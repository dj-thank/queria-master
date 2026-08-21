#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""EDINET API v2から最新有価証券報告書の平均年齢・平均年間給与を抽出する。"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import requests

BASE = "https://api.edinet-fsa.go.jp/api/v2"
AGE_NAMES = {
    "averageageyearsinformationaboutreportingcompanyinformationaboutemployees",
    "averageageyearsinformationaboutreportingcompany",
    "averageageyearsinformationaboutemployees",
}
SALARY_NAMES = {
    "averageannualsalaryinformationaboutreportingcompanyinformationaboutemployees",
    "averageannualsalaryinformationaboutreportingcompany",
    "averageannualsalaryinformationaboutemployees",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip().strip('"').strip("'")
    return values


def normalize_sec(value: Any) -> str:
    code = re.sub(r"[^0-9A-Z]", "", str(value or "").upper())
    return code[:4] if len(code) >= 4 else ""


def load_targets(db: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    by_sec: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for r in con.execute("SELECT source_id,company_name,security_code FROM companies WHERE TRIM(COALESCE(security_code,''))<>'' ORDER BY source_row"):
        sec = normalize_sec(r["security_code"])
        if not sec:
            continue
        by_sec.setdefault(sec, []).append(r["source_id"])
        names[r["source_id"]] = r["company_name"]
    con.close()
    return by_sec, names


def request_json(session: requests.Session, url: str, params: dict[str, Any], timeout: float, retries: int, sleep_s: float) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", sleep_s * (2 ** attempt)))
                time.sleep(min(wait, 60))
                continue
            if r.status_code >= 500:
                time.sleep(min(sleep_s * (2 ** attempt), 30))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(min(sleep_s * (2 ** attempt), 30))
    raise RuntimeError(f"EDINET API失敗: {last}")


def request_bytes(session: requests.Session, url: str, params: dict[str, Any], timeout: float, retries: int, sleep_s: float) -> bytes:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", sleep_s * (2 ** attempt)))
                time.sleep(min(wait, 60))
                continue
            if r.status_code >= 500:
                time.sleep(min(sleep_s * (2 ** attempt), 30))
                continue
            r.raise_for_status()
            return r.content
        except Exception as exc:
            last = exc
            if attempt < retries:
                time.sleep(min(sleep_s * (2 ** attempt), 30))
    raise RuntimeError(f"EDINET文書取得失敗: {last}")


def date_range(start: date, end: date):
    cur = start
    while cur <= end:
        yield cur
        cur += timedelta(days=1)


def scan_latest_documents(session: requests.Session, api_key: str, by_sec: dict[str, list[str]], cache_dir: Path,
                          start: date, end: date, timeout: float, retries: int, sleep_s: float) -> dict[str, dict[str, Any]]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    latest: dict[str, dict[str, Any]] = {}
    for d in date_range(start, end):
        cache = cache_dir / f"{d.isoformat()}.json"
        if cache.exists():
            try:
                payload = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                cache.unlink(missing_ok=True)
                payload = None
        else:
            payload = None
        if payload is None:
            payload = request_json(
                session,
                f"{BASE}/documents.json",
                {"date": d.isoformat(), "type": 2, "Subscription-Key": api_key},
                timeout, retries, sleep_s,
            )
            cache.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            time.sleep(sleep_s)
        for doc in payload.get("results", []) or []:
            if str(doc.get("docTypeCode") or "") != "120":
                continue
            sec = normalize_sec(doc.get("secCode"))
            if sec not in by_sec:
                continue
            if str(doc.get("withdrawalStatus") or "0") not in {"0", ""}:
                continue
            if str(doc.get("docInfoEditStatus") or "0") not in {"0", ""}:
                continue
            key = str(doc.get("submitDateTime") or doc.get("filingDate") or d.isoformat())
            if sec not in latest or key > str(latest[sec].get("_sort") or ""):
                doc = dict(doc)
                doc["_sort"] = key
                latest[sec] = doc
    return latest


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def score_context(context_ref: str) -> int:
    s = (context_ref or "").lower()
    score = 0
    if "current" in s: score += 30
    if "year" in s or "annual" in s: score += 10
    if "consolidated" not in s and "nonconsolidated" in s: score += 4
    if "prior" in s or "previous" in s: score -= 40
    if "member" in s or "segment" in s: score -= 10
    return score


def extract_xbrl_metrics(blob: bytes) -> tuple[float | None, int | None, dict[str, Any]]:
    candidates_age: list[tuple[int, float, str, str]] = []
    candidates_salary: list[tuple[int, int, str, str]] = []
    files_seen: list[str] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith((".xbrl", ".xml")) and "xbrl" in n.lower()]
        for name in names:
            files_seen.append(name)
            try:
                with zf.open(name) as f:
                    for _event, elem in ET.iterparse(f, events=("end",)):
                        lname = local_name(elem.tag).lower()
                        text = (elem.text or "").strip().replace(",", "")
                        if not text or elem.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}nil") == "true":
                            elem.clear(); continue
                        context = elem.attrib.get("contextRef", "")
                        score = score_context(context)
                        if lname in AGE_NAMES or ("averageageyears" in lname and "employees" in lname):
                            try:
                                value = float(text)
                                if 0 <= value <= 100:
                                    candidates_age.append((score, value, context, lname))
                            except ValueError:
                                pass
                        if lname in SALARY_NAMES or ("averageannualsalary" in lname and "employees" in lname):
                            try:
                                value = int(round(float(text)))
                                if 100_000 <= value <= 1_000_000_000:
                                    candidates_salary.append((score, value, context, lname))
                            except ValueError:
                                pass
                        elem.clear()
            except ET.ParseError:
                continue
    age = max(candidates_age, default=(0, None, "", ""), key=lambda x: (x[0], x[1] or 0))[1]
    salary = max(candidates_salary, default=(0, None, "", ""), key=lambda x: (x[0], x[1] or 0))[1]
    debug = {
        "files_seen": files_seen,
        "age_candidates": candidates_age[:20],
        "salary_candidates": candidates_salary[:20],
    }
    return age, salary, debug


def main() -> int:
    ap = argparse.ArgumentParser(description="EDINETから平均年齢・平均年収を取得")
    ap.add_argument("--db", type=Path, default=Path("output/company_public_data.sqlite3"))
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--output", type=Path, default=Path("input/EDINET_平均年齢・平均年収.csv"))
    ap.add_argument("--cache-dir", type=Path, default=Path("cache/edinet"))
    ap.add_argument("--from-date", type=date.fromisoformat)
    ap.add_argument("--to-date", type=date.fromisoformat, default=date.today())
    ap.add_argument("--days", type=int, default=500, help="from-date未指定時に遡る日数")
    ap.add_argument("--timeout", type=float, default=45)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--limit", type=int, default=0, help="検証用。対象証券コード数を制限")
    args = ap.parse_args()

    env = load_env(args.env)
    api_key = os.environ.get("EDINET_API_KEY") or env.get("EDINET_API_KEY") or ""
    if not api_key:
        raise SystemExit("EDINET_API_KEYがありません。.envへ設定してください。")
    by_sec, names = load_targets(args.db)
    if args.limit > 0:
        by_sec = dict(list(sorted(by_sec.items()))[:args.limit])
    if not by_sec:
        raise SystemExit("証券コードのある対象企業がありません。")
    start = args.from_date or (args.to_date - timedelta(days=args.days - 1))
    session = requests.Session()
    session.headers.update({"User-Agent": "Public-Company-Enricher/1.0 (+data integration; low rate)"})
    latest = scan_latest_documents(session, api_key, by_sec, args.cache_dir / "lists", start, args.to_date,
                                   args.timeout, args.retries, args.sleep)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    cache_docs = args.cache_dir / "documents"; cache_docs.mkdir(parents=True, exist_ok=True)
    fields = ["SOURCE_ID","企業名","証券コード","EDINETコード","書類管理番号","提出日時","事業年度末","平均年齢","平均年収円","出典URL","取得日時","抽出詳細JSON"]
    rows_written = docs_found = metrics_found = 0
    with args.output.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for sec, source_ids in sorted(by_sec.items()):
            doc = latest.get(sec)
            if not doc:
                continue
            docs_found += 1
            doc_id = str(doc.get("docID") or "")
            cache = cache_docs / f"{doc_id}.zip"
            if cache.exists():
                blob = cache.read_bytes()
            else:
                blob = request_bytes(session, f"{BASE}/documents/{doc_id}", {"type": 1, "Subscription-Key": api_key},
                                     args.timeout, args.retries, args.sleep)
                cache.write_bytes(blob)
                time.sleep(args.sleep)
            try:
                age, salary, debug = extract_xbrl_metrics(blob)
            except zipfile.BadZipFile:
                age, salary, debug = None, None, {"error": "BadZipFile"}
            if age is not None or salary is not None:
                metrics_found += 1
            for fid in source_ids:
                w.writerow({
                    "SOURCE_ID": fid,
                    "企業名": names.get(fid, ""),
                    "証券コード": sec,
                    "EDINETコード": doc.get("edinetCode") or "",
                    "書類管理番号": doc_id,
                    "提出日時": doc.get("submitDateTime") or "",
                    "事業年度末": doc.get("periodEnd") or "",
                    "平均年齢": age if age is not None else "",
                    "平均年収円": salary if salary is not None else "",
                    "出典URL": f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?S100={doc_id}",
                    "取得日時": now_iso(),
                    "抽出詳細JSON": json.dumps(debug, ensure_ascii=False, separators=(",", ":")),
                })
                rows_written += 1
    print(json.dumps({
        "target_security_codes": len(by_sec), "scan_from": start.isoformat(), "scan_to": args.to_date.isoformat(),
        "annual_reports_found": docs_found, "documents_with_metrics": metrics_found,
        "output_rows": rows_written, "output": str(args.output)
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
