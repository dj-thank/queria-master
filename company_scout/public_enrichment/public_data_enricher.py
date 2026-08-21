#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Enrich a local company list with public/official corporate data.

The input dataset stays local. Public values are kept separately with provenance,
match quality, and review status. No source-dataset-specific identifiers are assumed
except a caller-provided SOURCE_ID.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

DEFAULT_DB = Path("output/company_public_data.sqlite3")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in {"none", "null", "nan"} else s


def nh(v: Any) -> str:
    s = unicodedata.normalize("NFKC", clean(v)).lower()
    return re.sub(r"[\s\u3000_\-–—・:：()（）\[\]【】/\\]+", "", s)


def nname(v: Any) -> str:
    s = unicodedata.normalize("NFKC", clean(v)).lower()
    for token in ["株式会社", "有限会社", "合同会社", "一般社団法人", "一般財団法人", "公益社団法人", "公益財団法人", "(株)", "㈱", "(有)", "㈲", "inc.", "inc", "co.,ltd."]:
        s = s.replace(token, "")
    return re.sub(r"[\s\u3000・･\.．,，'’\"“”\-‐–—_()（）\[\]【】/\\]+", "", s)


def naddr(v: Any) -> str:
    s = unicodedata.normalize("NFKC", clean(v)).lower()
    for k, val in {"〇":"0","一":"1","二":"2","三":"3","四":"4","五":"5","六":"6","七":"7","八":"8","九":"9"}.items():
        s = s.replace(k, val)
    s = s.replace("丁目", "-").replace("番地", "-").replace("番", "-").replace("号", "")
    s = re.sub(r"[\s\u3000・･\.．,，'’\"“”()（）\[\]【】/\\]+", "", s)
    return re.sub(r"[-‐‑‒–—―ー]+", "-", s).strip("-")


def corpno(v: Any) -> str:
    d = re.sub(r"\D", "", clean(v))
    return d if len(d) == 13 else ""


def sec_code(v: Any) -> str:
    s = re.sub(r"[^0-9A-Z]", "", unicodedata.normalize("NFKC", clean(v)).upper())
    return s[:4] if len(s) >= 4 else ""


def number(v: Any) -> float | None:
    s = unicodedata.normalize("NFKC", clean(v))
    if not s or s in {"-", "―", "—"}:
        return None
    neg = s.startswith(("△", "▲", "-")) or (s.startswith("(") and s.endswith(")"))
    s = s.replace("△", "").replace("▲", "").strip("() ")
    s = re.sub(r"[^0-9.+-]", "", s)
    try:
        x = float(s)
    except Exception:
        return None
    return -abs(x) if neg else x


UNITS = {"円":1, "千円":1_000, "万円":10_000, "百万円":1_000_000, "千万円":10_000_000, "億円":100_000_000, "十億円":1_000_000_000, "百億円":10_000_000_000, "千億円":100_000_000_000, "兆円":1_000_000_000_000}


def yen(v: Any, unit: Any = "") -> int | None:
    raw, u = clean(v), clean(unit)
    if not raw:
        return None
    mult = None
    for key in sorted(UNITS, key=len, reverse=True):
        if key in raw or key in u:
            mult = UNITS[key]
            break
    if mult is None:
        mult = 1 if re.fullmatch(r"[△▲\-()0-9,，.\s]+", raw) else None
    x = number(raw)
    return None if mult is None or x is None else int(round(x * mult))


def field(fields: Sequence[str], aliases: Sequence[str], contains: bool = False) -> str | None:
    normalized = {nh(x): x for x in fields if x is not None}
    for alias in aliases:
        a = nh(alias)
        if a in normalized:
            return normalized[a]
    if contains:
        for alias in aliases:
            a = nh(alias)
            for k, original in normalized.items():
                if a and a in k:
                    return original
    return None


def value(row: dict[str, str], aliases: Sequence[str], contains: bool = False) -> str:
    k = field(list(row), aliases, contains)
    return clean(row.get(k, "")) if k else ""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS companies(
      source_id TEXT PRIMARY KEY, source_row INTEGER, company_name TEXT, address TEXT,
      security_code TEXT, jsic_code TEXT, jsic_name TEXT, source_json TEXT);
    CREATE TABLE IF NOT EXISTS corporate_matches(
      source_id TEXT PRIMARY KEY REFERENCES companies(source_id) ON DELETE CASCADE,
      corporate_number TEXT, matched_name TEXT, matched_address TEXT, match_code TEXT,
      hit_count INTEGER, source_name TEXT, confidence REAL, status TEXT, reason TEXT, matched_at TEXT);
    CREATE TABLE IF NOT EXISTS public_master(
      corporate_number TEXT PRIMARY KEY, company_name TEXT, postal_code TEXT, address TEXT,
      corporate_status TEXT, close_date TEXT, representative_name TEXT, representative_position TEXT,
      capital_yen INTEGER, employees INTEGER, established_date TEXT, website_url TEXT,
      business_summary TEXT, business_categories TEXT, source_quality TEXT, source_org TEXT,
      acquired_at TEXT, updated_at TEXT, source_file TEXT, raw_json TEXT);
    CREATE TABLE IF NOT EXISTS financial_history(
      corporate_number TEXT, fiscal_period TEXT, fiscal_sort_key TEXT, revenue_yen INTEGER,
      revenue_label TEXT, net_income_yen INTEGER, source_quality TEXT, source_org TEXT,
      acquired_at TEXT, updated_at TEXT, source_file TEXT, raw_json TEXT,
      PRIMARY KEY(corporate_number,fiscal_period));
    CREATE TABLE IF NOT EXISTS workplace_info(
      corporate_number TEXT PRIMARY KEY, average_age REAL, source_org TEXT, updated_at TEXT, source_file TEXT, raw_json TEXT);
    CREATE TABLE IF NOT EXISTS edinet_metrics(
      source_id TEXT PRIMARY KEY REFERENCES companies(source_id) ON DELETE CASCADE,
      average_age REAL, average_salary_yen INTEGER, source_url TEXT, fetched_at TEXT, raw_json TEXT);
    CREATE TABLE IF NOT EXISTS site_contacts(
      source_id TEXT PRIMARY KEY REFERENCES companies(source_id) ON DELETE CASCADE,
      phone TEXT, evidence_url TEXT, evidence_text TEXT, confidence REAL, fetched_at TEXT, raw_json TEXT);
    CREATE TABLE IF NOT EXISTS source_audit(
      id INTEGER PRIMARY KEY AUTOINCREMENT, source_file TEXT, source_type TEXT, sha256 TEXT,
      rows_read INTEGER, rows_accepted INTEGER, rows_review INTEGER, errors INTEGER, imported_at TEXT, notes TEXT);
    """)
    con.commit()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def read_csv_bytes(blob: bytes) -> tuple[list[str], list[dict[str, str]]]:
    last = None
    for enc in ("utf-8-sig", "cp932", "shift_jis", "utf-8"):
        try:
            text = blob.decode(enc)
            r = csv.DictReader(io.StringIO(text))
            rows = list(r)
            if r.fieldnames:
                return list(r.fieldnames), rows
        except Exception as exc:
            last = exc
    raise ValueError(f"CSV decode failed: {last}")


def prepare(con: sqlite3.Connection, path: Path, replace: bool) -> dict[str, Any]:
    if replace:
        for t in ["site_contacts","edinet_metrics","workplace_info","financial_history","public_master","corporate_matches","companies","source_audit"]:
            con.execute(f"DELETE FROM {t}")
    elif con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]:
        raise RuntimeError("companies already contains data; pass --replace to rebuild")
    fields, rows = read_csv_bytes(path.read_bytes())
    idk = field(fields, ["SOURCE_ID", "source_id", "ID", "id"])
    nk = field(fields, ["企業名", "会社名", "法人名", "company_name", "name"])
    ak = field(fields, ["本店所在地", "所在地", "住所", "address"])
    sk = field(fields, ["証券コード", "security_code", "symbol"])
    jck = field(fields, ["jsicDetailedClass", "JSIC細分類コード", "業種コード"])
    jnk = field(fields, ["saibunruiName", "JSIC細分類名", "業種名"])
    if not nk or not ak:
        raise RuntimeError("company name and address columns are required")
    inserted = duplicate = invalid = 0
    for i, row in enumerate(rows, start=2):
        sid = clean(row.get(idk, "")) if idk else f"row-{i-1:08d}"
        name, address = clean(row.get(nk, "")), clean(row.get(ak, ""))
        if not sid or not name or not address:
            invalid += 1
            continue
        try:
            con.execute("INSERT INTO companies VALUES(?,?,?,?,?,?,?,?)", (
                sid, i, name, address, sec_code(row.get(sk, "")) if sk else "",
                clean(row.get(jck, "")) if jck else "", clean(row.get(jnk, "")) if jnk else "",
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))))
            inserted += 1
        except sqlite3.IntegrityError:
            duplicate += 1
    con.commit()
    return {"inserted": inserted, "duplicate": duplicate, "invalid": invalid}


def make_assignment(con: sqlite3.Connection, output: Path) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = con.execute("SELECT source_id,company_name,address FROM companies ORDER BY source_row").fetchall()
    with output.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["SOURCE_ID","企業名","本店所在地","法人番号"])
        for r in rows:
            w.writerow([r["source_id"],r["company_name"],r["address"],""])
    return {"rows": len(rows), "output": str(output)}


def upsert_match(con: sqlite3.Connection, sid: str, corp: str, name: str, address: str, code: str, hit: int, source: str, confidence: float, status: str, reason: str) -> None:
    old = con.execute("SELECT * FROM corporate_matches WHERE source_id=?", (sid,)).fetchone()
    if old and old["status"] == "accepted" and status != "accepted":
        return
    if old and old["status"] == "accepted" and status == "accepted" and old["corporate_number"] != corp:
        status, confidence = "review", min(confidence, .4)
        reason = f"accepted corporate number conflict: {old['corporate_number']} vs {corp}; {reason}"
    con.execute("""INSERT INTO corporate_matches VALUES(?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(source_id) DO UPDATE SET corporate_number=excluded.corporate_number,
      matched_name=excluded.matched_name,matched_address=excluded.matched_address,match_code=excluded.match_code,
      hit_count=excluded.hit_count,source_name=excluded.source_name,confidence=excluded.confidence,
      status=excluded.status,reason=excluded.reason,matched_at=excluded.matched_at""",
      (sid,corp,name,address,code,hit,source,confidence,status,reason,now_iso()))


def classify(fields: Sequence[str]) -> str:
    hs = {nh(x) for x in fields}
    joined = "|".join(hs)
    if "sourceid" in hs and any(x in joined for x in ["結果コード","一致コード","マッチコード"]):
        return "numbering"
    if "sourceid" in hs and any(x in joined for x in ["平均年収","平均年間給与","edinetコード"]):
        return "edinet"
    if "sourceid" in hs and any(x in joined for x in ["根拠url","電話番号","公式サイトurl"]):
        return "site_phone"
    if "法人番号" in joined and any(x in joined for x in ["平均年齢","平均継続勤務年数","所定外労働"]):
        return "workplace"
    if "法人番号" in joined and any(x in joined for x in ["事業年度","売上高","営業収益","純利益","総資産"]):
        return "financial"
    if "法人番号" in joined and any(x in joined for x in ["事業概要","法人代表者","企業ホームページ","資本金"]):
        return "basic"
    return "unknown"


def import_numbering(con: sqlite3.Connection, source: str, fields: list[str], rows: list[dict[str,str]], accept_prefix: bool) -> tuple[int,int,int,int]:
    idk=field(fields,["SOURCE_ID","source_id"])
    nk=field(fields,["企業名","法人名","商号または名称"])
    ak=field(fields,["本店所在地","所在地","住所"])
    ck=field(fields,["法人番号"])
    codek=field(fields,["結果コード","一致コード","マッチコード","付与結果コード"],True)
    hitk=field(fields,["ヒット件数","候補件数","該当件数"],True)
    if not ck:
        raise ValueError("corporate number column not found")
    by_key: dict[tuple[str,str],list[str]] = defaultdict(list)
    for r in con.execute("SELECT source_id,company_name,address FROM companies"):
        by_key[(nname(r["company_name"]),naddr(r["address"]))].append(r["source_id"])
    accepted=review=errors=0
    for row in rows:
        sid=clean(row.get(idk,"")) if idk else ""
        if not sid and nk and ak:
            ids=by_key.get((nname(row.get(nk,"")),naddr(row.get(ak,""))),[])
            sid=ids[0] if len(ids)==1 else ""
        if not sid:
            continue
        corp=corpno(row.get(ck,""))
        code=clean(row.get(codek,"")) if codek else ""
        hit=int(number(row.get(hitk,"")) or (1 if corp else 0)) if hitk else (1 if corp else 0)
        if code=="M00" and hit==1 and corp:
            status,conf,reason="accepted",1.0,"M00 and single hit"
        elif accept_prefix and code in {"M01","M02"} and hit==1 and corp:
            status,conf,reason="accepted",.85,"prefix match explicitly allowed"
        else:
            status,conf,reason="review",.6 if code in {"M01","M02"} and hit==1 else .4,f"outside auto-accept rule: code={code or 'unknown'}, hit={hit}"
        try:
            upsert_match(con,sid,corp,clean(row.get(nk,"")) if nk else "",clean(row.get(ak,"")) if ak else "",code or "NUMBERING",hit,source,conf,status,reason)
            accepted += int(status=="accepted")
            review += int(status=="review")
        except Exception:
            errors += 1
    con.commit()
    return len(rows),accepted,review,errors


def import_basic(con: sqlite3.Connection, source: str, fields: list[str], rows: list[dict[str,str]]) -> tuple[int,int,int,int]:
    ck=field(fields,["法人番号"])
    if not ck:
        raise ValueError("corporate number column not found")
    targets={r[0] for r in con.execute("SELECT corporate_number FROM corporate_matches WHERE status='accepted' AND corporate_number<>''")}
    accepted=errors=0
    for row in rows:
        corp=corpno(row.get(ck,""))
        if not corp:
            errors+=1
            continue
        if targets and corp not in targets:
            continue
        name=value(row,["商号または名称","商号又は名称","法人名","名称"])
        postal=value(row,["郵便番号"])
        address=value(row,["登記住所","本社所在地","所在地"])
        close_date=value(row,["登記記録の閉鎖等年月日","閉鎖年月日"])
        rep=value(row,["法人代表者名","代表者名","代表者氏名"])
        pos=value(row,["法人代表者役職","代表者役職"])
        capital=yen(value(row,["資本金","資本金額"]),value(row,["資本金（単位）","資本金単位"]))
        emp=number(value(row,["従業員数"]))
        established=value(row,["設立年月日","設立日"])
        website=value(row,["企業ホームページ","WebサイトURL","法人ホームページ","URL"],True)
        summary=value(row,["事業概要","企業概要","事業内容"])
        cats=[]
        for k,v in row.items():
            if any(x in nh(k) for x in [nh("事業種目"),nh("業種"),nh("事業分野")]) and clean(v):
                cats.append(clean(v))
        quality=value(row,["データ品質"])
        org=value(row,["出典元","出典"])
        acquired=value(row,["最終取得日","取得日"])
        updated=value(row,["最終更新日","更新日"])
        con.execute("""INSERT INTO public_master VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(corporate_number) DO UPDATE SET company_name=COALESCE(NULLIF(excluded.company_name,''),public_master.company_name),postal_code=COALESCE(NULLIF(excluded.postal_code,''),public_master.postal_code),address=COALESCE(NULLIF(excluded.address,''),public_master.address),corporate_status=excluded.corporate_status,close_date=COALESCE(NULLIF(excluded.close_date,''),public_master.close_date),representative_name=COALESCE(NULLIF(excluded.representative_name,''),public_master.representative_name),representative_position=COALESCE(NULLIF(excluded.representative_position,''),public_master.representative_position),capital_yen=COALESCE(excluded.capital_yen,public_master.capital_yen),employees=COALESCE(excluded.employees,public_master.employees),established_date=COALESCE(NULLIF(excluded.established_date,''),public_master.established_date),website_url=COALESCE(NULLIF(excluded.website_url,''),public_master.website_url),business_summary=COALESCE(NULLIF(excluded.business_summary,''),public_master.business_summary),business_categories=COALESCE(NULLIF(excluded.business_categories,''),public_master.business_categories),source_quality=excluded.source_quality,source_org=excluded.source_org,acquired_at=excluded.acquired_at,updated_at=excluded.updated_at,source_file=excluded.source_file,raw_json=excluded.raw_json""",
        (corp,name,postal,address,"closed" if close_date else "active",close_date,rep,pos,capital,int(emp) if emp is not None else None,established,website,summary," | ".join(dict.fromkeys(cats)),quality,org,acquired,updated,source,json.dumps(row,ensure_ascii=False,separators=(",",":"))))
        accepted+=1
    con.commit()
    return len(rows),accepted,0,errors


def fiscal_key(v: str) -> str:
    s=unicodedata.normalize("NFKC",clean(v))
    found=[]
    for y,m,d in re.findall(r"(\d{4})[年/\-.](\d{1,2})[月/\-.](\d{1,2})日?",s):
        found.append(f"{int(y):04d}-{int(m):02d}-{int(d):02d}")
    for y,m in re.findall(r"(\d{4})[年/\-.](\d{1,2})月?",s):
        found.append(f"{int(y):04d}-{int(m):02d}-01")
    if not found:
        found=[f"{y}-01-01" for y in re.findall(r"(?:19|20)\d{2}",s)]
    return max(found) if found else ""


def import_financial(con: sqlite3.Connection, source: str, fields: list[str], rows: list[dict[str,str]]) -> tuple[int,int,int,int]:
    ck=field(fields,["法人番号"])
    pk=field(fields,["事業年度","決算期","会計期間","年度"])
    if not ck or not pk:
        raise ValueError("corporate number/fiscal period column not found")
    targets={r[0] for r in con.execute("SELECT corporate_number FROM corporate_matches WHERE status='accepted' AND corporate_number<>''")}
    accepted=errors=0
    rev_alias=["売上高","営業収益","営業収入","営業総収入","経常収益","正味収入保険料"]
    for row in rows:
        corp=corpno(row.get(ck,""))
        period=clean(row.get(pk,""))
        if not corp or not period:
            errors+=1
            continue
        if targets and corp not in targets:
            continue
        rk=next((field(fields,[a],True) for a in rev_alias if field(fields,[a],True)),None)
        label=clean(rk) if rk else ""
        revenue=yen(row.get(rk,""),value(row,["単位","金額単位"])) if rk else None
        nk=field(fields,["純利益","当期純利益","親会社株主に帰属する当期純利益"],True)
        net=yen(row.get(nk,""),value(row,["単位","金額単位"])) if nk else None
        quality=value(row,["データ品質"])
        org=value(row,["出典元","出典"])
        acquired=value(row,["最終取得日","取得日"])
        updated=value(row,["最終更新日","更新日"])
        con.execute("""INSERT INTO financial_history VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(corporate_number,fiscal_period) DO UPDATE SET fiscal_sort_key=excluded.fiscal_sort_key,revenue_yen=COALESCE(excluded.revenue_yen,financial_history.revenue_yen),revenue_label=COALESCE(NULLIF(excluded.revenue_label,''),financial_history.revenue_label),net_income_yen=COALESCE(excluded.net_income_yen,financial_history.net_income_yen),source_quality=excluded.source_quality,source_org=excluded.source_org,acquired_at=excluded.acquired_at,updated_at=excluded.updated_at,source_file=excluded.source_file,raw_json=excluded.raw_json""",
        (corp,period,fiscal_key(period),revenue,label,net,quality,org,acquired,updated,source,json.dumps(row,ensure_ascii=False,separators=(",",":"))))
        accepted+=1
    con.commit()
    return len(rows),accepted,0,errors


def import_workplace(con: sqlite3.Connection, source: str, fields: list[str], rows: list[dict[str,str]]) -> tuple[int,int,int,int]:
    ck=field(fields,["法人番号"])
    accepted=errors=0
    if not ck:
        raise ValueError("corporate number column not found")
    for row in rows:
        corp=corpno(row.get(ck,""))
        age=number(value(row,["従業員の平均年齢","平均年齢"],True))
        if not corp:
            errors+=1
            continue
        con.execute("""INSERT INTO workplace_info VALUES(?,?,?,?,?,?) ON CONFLICT(corporate_number) DO UPDATE SET average_age=COALESCE(excluded.average_age,workplace_info.average_age),source_org=excluded.source_org,updated_at=excluded.updated_at,source_file=excluded.source_file,raw_json=excluded.raw_json""",
        (corp,float(age) if age is not None and 0<=age<=100 else None,value(row,["出典元","出典"]),value(row,["最終更新日","更新日"]),source,json.dumps(row,ensure_ascii=False,separators=(",",":"))))
        accepted+=1
    con.commit()
    return len(rows),accepted,0,errors


def import_edinet(con: sqlite3.Connection, source: str, fields: list[str], rows: list[dict[str,str]]) -> tuple[int,int,int,int]:
    idk=field(fields,["SOURCE_ID","source_id"])
    accepted=errors=0
    if not idk:
        raise ValueError("SOURCE_ID column not found")
    for row in rows:
        sid=clean(row.get(idk,""))
        age=number(value(row,["平均年齢"]))
        salary=yen(value(row,["平均年収円","平均年間給与","平均年収"]))
        if not sid:
            errors+=1
            continue
        con.execute("""INSERT INTO edinet_metrics VALUES(?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET average_age=COALESCE(excluded.average_age,edinet_metrics.average_age),average_salary_yen=COALESCE(excluded.average_salary_yen,edinet_metrics.average_salary_yen),source_url=excluded.source_url,fetched_at=excluded.fetched_at,raw_json=excluded.raw_json""",
        (sid,float(age) if age is not None else None,salary,value(row,["出典URL"]),value(row,["取得日時"]),json.dumps(row,ensure_ascii=False,separators=(",",":"))))
        accepted+=1
    con.commit()
    return len(rows),accepted,0,errors


def import_site(con: sqlite3.Connection, source: str, fields: list[str], rows: list[dict[str,str]]) -> tuple[int,int,int,int]:
    idk=field(fields,["SOURCE_ID","source_id"])
    accepted=errors=0
    if not idk:
        raise ValueError("SOURCE_ID column not found")
    for row in rows:
        sid=clean(row.get(idk,""))
        phone=value(row,["電話番号"])
        if not sid:
            errors+=1
            continue
        conf=number(value(row,["信頼度"]))
        con.execute("""INSERT INTO site_contacts VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET phone=excluded.phone,evidence_url=excluded.evidence_url,evidence_text=excluded.evidence_text,confidence=excluded.confidence,fetched_at=excluded.fetched_at,raw_json=excluded.raw_json""",
        (sid,phone,value(row,["根拠URL"]),value(row,["根拠テキスト"]),float(conf) if conf is not None else None,value(row,["取得日時"]),json.dumps(row,ensure_ascii=False,separators=(",",":"))))
        accepted+=1
    con.commit()
    return len(rows),accepted,0,errors


IMPORTERS={"numbering":import_numbering,"basic":import_basic,"financial":import_financial,"workplace":import_workplace,"edinet":import_edinet,"site_phone":import_site}


def iter_inputs(path: Path) -> Iterator[tuple[str,bytes,str]]:
    for p in sorted(path.glob("*")):
        if p.is_dir():
            continue
        if p.suffix.lower()==".csv":
            yield p.name,p.read_bytes(),sha256(p)
        elif p.suffix.lower()==".zip":
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():
                    if n.lower().endswith(".csv"):
                        yield f"{p.name}!{n}",z.read(n),sha256(p)


def import_dir(con: sqlite3.Connection, path: Path, accept_prefix: bool) -> dict[str,Any]:
    out=[]
    for name,blob,h in iter_inputs(path):
        try:
            fields,rows=read_csv_bytes(blob)
            typ=classify(fields)
            if typ=="unknown":
                continue
            if typ=="numbering":
                stats=import_numbering(con,name,fields,rows,accept_prefix)
            else:
                stats=IMPORTERS[typ](con,name,fields,rows)
            con.execute("INSERT INTO source_audit(source_file,source_type,sha256,rows_read,rows_accepted,rows_review,errors,imported_at,notes) VALUES(?,?,?,?,?,?,?,?,?)",(name,typ,h,*stats,now_iso(),""))
            con.commit()
            out.append({"file":name,"type":typ,"read":stats[0],"accepted":stats[1],"review":stats[2],"errors":stats[3]})
        except Exception as exc:
            out.append({"file":name,"type":"error","error":str(exc)})
    return {"files":out}


def latest_financial(con: sqlite3.Connection) -> dict[str,sqlite3.Row]:
    result={}
    for r in con.execute("SELECT * FROM financial_history ORDER BY corporate_number,fiscal_sort_key"):
        result[r["corporate_number"]]=r
    return result


def rank_maps(rows: list[dict[str,Any]], metric: str) -> dict[str,tuple[int,int]]:
    groups: dict[str,list[tuple[str,int]]] = defaultdict(list)
    for r in rows:
        v=r.get(metric)
        code=r.get("jsic_code") or ""
        if code and isinstance(v,int):
            groups[code].append((r["source_id"],v))
    out={}
    for code,items in groups.items():
        items.sort(key=lambda x:x[1],reverse=True)
        rank=0
        prev=None
        for i,(sid,v) in enumerate(items,1):
            if v!=prev:
                rank=i
                prev=v
            out[f"{code}:{sid}"]=(rank,len(items))
    return out


def keywords(summary: str, categories: str, jsic_name: str) -> str:
    text=" | ".join(x for x in [categories,jsic_name,summary] if x)
    parts=[]
    for x in re.split(r"[、,，。・/|\n;；]+",text):
        x=clean(x)
        if 2<=len(x)<=40 and x not in parts:
            parts.append(x)
        if len(parts)>=8:
            break
    return " / ".join(parts)


def export_all(con: sqlite3.Connection, outdir: Path) -> dict[str,Any]:
    outdir.mkdir(parents=True,exist_ok=True)
    latest=latest_financial(con)
    records=[]
    sql="""SELECT c.*,m.corporate_number,m.match_code,m.confidence,m.status,m.reason,
    p.company_name public_name,p.postal_code,p.address public_address,p.corporate_status,p.close_date,
    p.representative_name,p.representative_position,p.capital_yen,p.employees,p.established_date,p.website_url,
    p.business_summary,p.business_categories,p.source_quality,p.source_org,p.updated_at public_updated,
    w.average_age workplace_age,e.average_age edinet_age,e.average_salary_yen,e.source_url edinet_url,
    s.phone,s.evidence_url,s.confidence phone_conf
    FROM companies c LEFT JOIN corporate_matches m ON m.source_id=c.source_id
    LEFT JOIN public_master p ON p.corporate_number=m.corporate_number
    LEFT JOIN workplace_info w ON w.corporate_number=m.corporate_number
    LEFT JOIN edinet_metrics e ON e.source_id=c.source_id
    LEFT JOIN site_contacts s ON s.source_id=c.source_id ORDER BY c.source_row"""
    for r in con.execute(sql):
        f=latest.get(r["corporate_number"])
        records.append({**dict(r),"latest_period":f["fiscal_period"] if f else "","latest_revenue_yen":f["revenue_yen"] if f else None,"latest_revenue_label":f["revenue_label"] if f else "","latest_net_income_yen":f["net_income_yen"] if f else None})
    rr=rank_maps(records,"latest_revenue_yen")
    nr=rank_maps(records,"latest_net_income_yen")
    integrated=outdir/"companies_enriched.csv"
    details=outdir/"public_company_details.csv"
    review=outdir/"review_required.csv"
    history=outdir/"financial_history.csv"
    auditp=outdir/"source_audit.csv"
    public_headers=["公開_法人番号","公開_法人番号一致コード","公開_法人番号一致信頼度","公開_法人番号採用状態","公開_法人名","公開_郵便番号","公開_登記住所","公開_法人状態","公開_代表者名称","公開_代表者役職","公開_資本金円","公開_従業員数","公開_設立年月日","公開_WebサイトURL","公開_電話番号","公開_電話番号根拠URL","公開_事業概要","公開_事業種目","公開_最新決算期","公開_最新売上円","公開_最新売上種別","公開_最新純利益円","公開_平均年齢","公開_平均年収円","公開_コアキーワード","公開_売上業種内順位","公開_売上業種内母数","公開_純利益業種内順位","公開_純利益業種内母数","公開_主要出典","公開_データ品質","公開_最終更新日","公開_要確認理由"]
    source_headers=[]
    if records:
        try:
            source_headers=list(json.loads(records[0]["source_json"]).keys())
        except Exception:
            source_headers=["SOURCE_ID","企業名","本店所在地"]
    def pub(r: dict[str,Any]) -> list[Any]:
        key=f"{r.get('jsic_code') or ''}:{r['source_id']}"
        rrank=rr.get(key,(None,None))
        nrank=nr.get(key,(None,None))
        age=r.get("edinet_age") if r.get("edinet_age") is not None else r.get("workplace_age")
        return [r.get("corporate_number") or "",r.get("match_code") or "",r.get("confidence") if r.get("confidence") is not None else "",r.get("status") or "",r.get("public_name") or "",r.get("postal_code") or "",r.get("public_address") or "",r.get("corporate_status") or "",r.get("representative_name") or "",r.get("representative_position") or "",r.get("capital_yen") if r.get("capital_yen") is not None else "",r.get("employees") if r.get("employees") is not None else "",r.get("established_date") or "",r.get("website_url") or "",r.get("phone") or "",r.get("evidence_url") or "",r.get("business_summary") or "",r.get("business_categories") or "",r.get("latest_period") or "",r.get("latest_revenue_yen") if r.get("latest_revenue_yen") is not None else "",r.get("latest_revenue_label") or "",r.get("latest_net_income_yen") if r.get("latest_net_income_yen") is not None else "",age if age is not None else "",r.get("average_salary_yen") if r.get("average_salary_yen") is not None else "",keywords(r.get("business_summary") or "",r.get("business_categories") or "",r.get("jsic_name") or ""),rrank[0] or "",rrank[1] or "",nrank[0] or "",nrank[1] or ""," / ".join(x for x in [r.get("source_org"),"EDINET" if r.get("average_salary_yen") is not None else "","公式サイト" if r.get("phone") else ""] if x),r.get("source_quality") or "",r.get("public_updated") or "",r.get("reason") or ""]
    with integrated.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.writer(f)
        w.writerow(source_headers+public_headers)
        for r in records:
            try:
                src=json.loads(r["source_json"])
                base=[src.get(h,"") for h in source_headers]
            except Exception:
                base=[r["source_id"],r["company_name"],r["address"]]
            w.writerow(base+pub(r))
    with details.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.writer(f)
        w.writerow(["SOURCE_ID","企業名"]+public_headers)
        for r in records:
            w.writerow([r["source_id"],r["company_name"]]+pub(r))
    with review.open("w",encoding="utf-8-sig",newline="") as f:
        w=csv.writer(f)
        w.writerow(["SOURCE_ID","企業名","本店所在地","法人番号候補","一致コード","一致信頼度","理由"])
        for r in records:
            if r.get("status")!="accepted":
                w.writerow([r["source_id"],r["company_name"],r["address"],r.get("corporate_number") or "",r.get("match_code") or "",r.get("confidence") if r.get("confidence") is not None else "",r.get("reason") or "法人番号未確定"])
    with history.open("w",encoding="utf-8-sig",newline="") as f:
        rows=con.execute("SELECT * FROM financial_history ORDER BY corporate_number,fiscal_sort_key").fetchall()
        headers=list(rows[0].keys()) if rows else ["corporate_number","fiscal_period","fiscal_sort_key","revenue_yen","revenue_label","net_income_yen","source_quality","source_org","acquired_at","updated_at","source_file","raw_json"]
        w=csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow([r[h] for h in headers])
    with auditp.open("w",encoding="utf-8-sig",newline="") as f:
        rows=con.execute("SELECT * FROM source_audit ORDER BY id").fetchall()
        headers=list(rows[0].keys()) if rows else ["id","source_file","source_type","sha256","rows_read","rows_accepted","rows_review","errors","imported_at","notes"]
        w=csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow([r[h] for h in headers])
    return {"companies":len(records),"integrated":str(integrated),"details":str(details),"review":str(review),"history":str(history),"audit":str(auditp)}


def status(con: sqlite3.Connection) -> dict[str,Any]:
    tables=["companies","corporate_matches","public_master","financial_history","workplace_info","edinet_metrics","site_contacts","source_audit"]
    out={t:con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    out["accepted_matches"]=con.execute("SELECT COUNT(*) FROM corporate_matches WHERE status='accepted'").fetchone()[0]
    out["review_matches"]=con.execute("SELECT COUNT(*) FROM corporate_matches WHERE status='review'").fetchone()[0]
    out["integrity"]=con.execute("PRAGMA integrity_check").fetchone()[0]
    return out


def parser() -> argparse.ArgumentParser:
    p=argparse.ArgumentParser(description="Integrate public company data into a local company list")
    p.add_argument("--db",type=Path,default=DEFAULT_DB)
    sub=p.add_subparsers(dest="command",required=True)
    x=sub.add_parser("prepare")
    x.add_argument("csv",type=Path)
    x.add_argument("--replace",action="store_true")
    x=sub.add_parser("make-assignment")
    x.add_argument("--output",type=Path,default=Path("input/法人番号付与用.csv"))
    x=sub.add_parser("import")
    x.add_argument("--input-dir",type=Path,default=Path("input"))
    x.add_argument("--accept-prefix",action="store_true")
    x=sub.add_parser("export")
    x.add_argument("--output-dir",type=Path,default=Path("output/csv"))
    x=sub.add_parser("run-all")
    x.add_argument("--input-dir",type=Path,default=Path("input"))
    x.add_argument("--output-dir",type=Path,default=Path("output/csv"))
    x.add_argument("--accept-prefix",action="store_true")
    sub.add_parser("status")
    return p


def main() -> int:
    a=parser().parse_args()
    con=connect(a.db)
    init(con)
    try:
        if a.command=="prepare":
            result=prepare(con,a.csv,a.replace)
        elif a.command=="make-assignment":
            result=make_assignment(con,a.output)
        elif a.command=="import":
            result=import_dir(con,a.input_dir,a.accept_prefix)
        elif a.command=="export":
            result=export_all(con,a.output_dir)
        elif a.command=="run-all":
            result={"imports":import_dir(con,a.input_dir,a.accept_prefix),"export":export_all(con,a.output_dir)}
        else:
            result=status(con)
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        return 0
    finally:
        con.close()

if __name__=="__main__":
    raise SystemExit(main())
