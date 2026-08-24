from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .app_config import AppSettings, SettingsError, default_settings_path, load_settings, resolve_artifacts, save_settings
from .audit import AuditError, DEFAULT_AUDIT_OUTPUT, audit_database
from .enrichment import (
    DEFAULT_ENRICHMENT_DB,
    EnrichmentError,
    claim_enrichment_tasks,
    complete_enrichment_task,
    export_establishment_contacts,
    export_sales_ready_accounts,
    import_enrichment_jsonl,
    initialize_database as initialize_enrichment_database,
    seed_enrichment,
    sync_embedded_public_enrichment,
)
from .enrichment_worker import run_enrichment_worker
from .health import inspect_application
from .pipeline import PipelineError, online_probe, refresh, version_report
from .query import run_local_sql, search_companies, semantic_search_companies, show_summary
from .resident import run_jsonl_protocol
from .resources import ALL_PUBLIC_SCOPE, DEFAULT_CACHE, DEFAULT_DB, PROJECT_ROOT, public_scope_choices
from .runtime import (
    DEFAULT_RUNTIME_DB,
    RuntimeBuildError,
    build_runtime_database,
    runtime_summary,
)
from .search_index import DEFAULT_SEARCH_INDEX, build_search_index
from .semantic_index import DEFAULT_SEMANTIC_INDEX, SemanticIndexError, build_semantic_index


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="queria-master",
        description="Queria の公開法人データをローカル DuckDB へ取り込む",
    )
    parser.add_argument(
        "--db",
        type=_path,
        default=None,
        help="DuckDB ファイル（省略時は検索系がruntime DB、更新系がcanonical DB）",
    )
    parser.add_argument("--settings", type=_path, help="保存設定JSON（省略時はアプリホーム/config配下）")
    sub = parser.add_subparsers(dest="command", required=True)

    refresh_parser = sub.add_parser("refresh", help="Queria から抽出して DuckDB を再構築")
    refresh_parser.add_argument(
        "--scope",
        choices=public_scope_choices(),
        default=ALL_PUBLIC_SCOPE,
        help="all-public/all は全法人＋gBizINFO活動情報（大容量）",
    )
    refresh_parser.add_argument("--cache-dir", type=_path, default=DEFAULT_CACHE)
    refresh_parser.add_argument("--no-cache", action="store_true", help="抽出 Parquet を保持しない")

    doctor_parser = sub.add_parser("doctor", help="ローカル環境と DB を検証")
    doctor_parser.add_argument("--online", action="store_true", help="Queria への接続も確認")

    search_parser = sub.add_parser("search", help="法人マスタを条件検索")
    search_parser.add_argument("--keyword")
    search_parser.add_argument("--prefecture")
    search_parser.add_argument("--city")
    search_parser.add_argument("--industry-major", action="append", default=[], help="JSIC大分類 A〜T（複数指定可）")
    search_parser.add_argument("--industry-middle", action="append", default=[])
    search_parser.add_argument("--corporate-kind", action="append", default=[], help="法人種別コード（複数指定可）")
    search_parser.add_argument("--min-employees", type=int)
    search_parser.add_argument("--max-employees", type=int)
    search_parser.add_argument("--min-capital", type=int)
    search_parser.add_argument("--max-capital", type=int)
    search_parser.add_argument("--has-web", action="store_true")
    search_parser.add_argument("--limit", type=int, default=100)
    search_parser.add_argument("--out", type=_path)
    search_parser.add_argument("--search-index", type=_path)
    search_parser.add_argument("--no-search-index", action="store_true", help="SQLite FTS高速索引を使わない")
    search_parser.add_argument("--fast", action="store_true", help="安定ソートを省略し、0.1秒級の応答を優先")

    daemon_parser = sub.add_parser(
        "daemon",
        help="検索索引を常駐させるJSONLプロトコル（プロセス起動を繰り返さない高速経路）",
    )
    daemon_parser.add_argument("--search-index", type=_path)
    daemon_parser.add_argument(
        "--no-index-validation",
        action="store_true",
        help="索引とDBの統計検証を省略する（同一リリース内のベンチマーク専用）",
    )

    index_parser = sub.add_parser("build-search-index", help="法人キーワード検索用のSQLite FTS索引を構築")
    index_parser.add_argument("--out", type=_path)
    index_parser.add_argument("--batch-size", type=int, default=20_000)

    runtime_parser = sub.add_parser(
        "build-runtime",
        help="法人マスタと証拠付き拡張DBを一つの高速読み取り用DuckDBへ統合",
    )
    runtime_parser.add_argument("--enrichment-db", type=_path)
    runtime_parser.add_argument("--out", type=_path)
    runtime_parser.add_argument("--threads", type=int, default=4)
    runtime_parser.add_argument("--memory-limit", default="8GB")

    runtime_summary_parser = sub.add_parser("runtime-summary", help="統合ランタイムDBの件数と収録状態を表示")
    runtime_summary_parser.add_argument("--runtime-db", type=_path)

    audit_parser = sub.add_parser("audit", help="法人DB・拡張DB・検索索引・統合DBを読み取り専用で監査")
    audit_parser.add_argument("--search-index", type=_path)
    audit_parser.add_argument("--enrichment-db", type=_path)
    audit_parser.add_argument("--runtime-db", type=_path)
    audit_parser.add_argument("--out", type=_path, default=DEFAULT_AUDIT_OUTPUT)
    audit_parser.add_argument("--no-runtime", action="store_true", help="統合ランタイムDBを監査しない")
    audit_parser.add_argument("--strict", action="store_true", help="ゲート失敗時に終了コード1を返す")

    semantic_build_parser = sub.add_parser(
        "build-semantic-index", help="任意の埋め込みモデルで text-rich 法人のベクトル索引を構築"
    )
    semantic_build_parser.add_argument("--model", required=True, help="SentenceTransformersモデル名またはローカルパス")
    semantic_build_parser.add_argument("--search-index", type=_path)
    semantic_build_parser.add_argument("--out", type=_path, default=DEFAULT_SEMANTIC_INDEX)
    semantic_build_parser.add_argument("--batch-size", type=int, default=256)
    semantic_build_parser.add_argument("--dtype", choices=("float16", "float32"), default="float16")
    semantic_build_parser.add_argument("--min-text-chars", type=int, default=16)
    semantic_build_parser.add_argument("--limit", type=int)

    semantic_search_parser = sub.add_parser("semantic-search", help="任意の埋め込みモデルで意味検索")
    semantic_search_parser.add_argument("query", help="自然文の検索文")
    semantic_search_parser.add_argument("--model", help="SentenceTransformersモデル名。省略時は索引の記録値")
    semantic_search_parser.add_argument("--search-index", type=_path)
    semantic_search_parser.add_argument("--semantic-index", type=_path, default=DEFAULT_SEMANTIC_INDEX)
    semantic_search_parser.add_argument("--candidate-keyword", help="先にFTSで候補を絞る語")
    semantic_search_parser.add_argument("--prefecture")
    semantic_search_parser.add_argument("--city")
    semantic_search_parser.add_argument("--industry-major", action="append", default=[])
    semantic_search_parser.add_argument("--industry-middle", action="append", default=[])
    semantic_search_parser.add_argument("--has-web", action="store_true")
    semantic_search_parser.add_argument("--candidate-limit", type=int, default=20_000)
    semantic_search_parser.add_argument("--limit", type=int, default=100)
    semantic_search_parser.add_argument("--out", type=_path)
    semantic_search_parser.add_argument("--device", help="モデルの実行デバイス（例: cpu, cuda）")

    sub.add_parser("summary", help="収録件数・欠損・地域・業種を集計")

    sql_parser = sub.add_parser("sql", help="ローカル DuckDB へ読み取り専用 SQL を実行")
    source = sql_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query")
    source.add_argument("--file", type=_path)
    sql_parser.add_argument("--max-rows", type=int, default=200)
    sql_parser.add_argument("--out", type=_path)

    sub.add_parser("sources", help="DB に格納した出典情報を表示")

    init_enrichment_parser = sub.add_parser(
        "init-enrichment", help="正規法人DBを変更せず、証拠付き拡張DBを初期化"
    )
    init_enrichment_parser.add_argument("--enrichment-db", type=_path)

    seed_parser = sub.add_parser("seed-enrichment", help="全法人分の再開可能な拡張タスクを作成")
    seed_parser.add_argument("--enrichment-db", type=_path)
    seed_parser.add_argument("--limit", type=int)
    seed_parser.add_argument("--source-key", default="official_site")
    seed_parser.add_argument("--industry-major", help="JSIC大分類1文字で対象法人を限定（例: G=情報通信業）")

    import_enrichment_parser = sub.add_parser("import-enrichment", help="抽出器のJSONLを証拠付きで取り込む")
    import_enrichment_parser.add_argument("--enrichment-db", type=_path)
    import_enrichment_parser.add_argument("--file", type=_path, required=True)
    import_enrichment_parser.add_argument("--batch-size", type=int, default=1000)

    claim_parser = sub.add_parser("claim-enrichment", help="拡張タスクをワーカーへリース")
    claim_parser.add_argument("--enrichment-db", type=_path)
    claim_parser.add_argument("--worker-id", required=True)
    claim_parser.add_argument("--field")
    claim_parser.add_argument("--source-key")
    claim_parser.add_argument("--batch-size", type=int, default=100)
    claim_parser.add_argument("--lease-seconds", type=int, default=900)

    complete_parser = sub.add_parser("complete-enrichment", help="拡張タスクを完了状態へ更新")
    complete_parser.add_argument("corporate_number")
    complete_parser.add_argument("field_name")
    complete_parser.add_argument("source_key")
    complete_parser.add_argument("--enrichment-db", type=_path)
    complete_parser.add_argument(
        "--state",
        choices=("found", "not_found_after_policy", "not_applicable", "needs_review", "blocked_by_policy", "failed"),
        required=True,
    )
    complete_parser.add_argument("--worker-id")
    complete_parser.add_argument("--evidence-id")
    complete_parser.add_argument("--error")

    parse_contact_parser = sub.add_parser("parse-contact-page", help="保存済み公式HTMLから連絡先候補を抽出")
    parse_contact_parser.add_argument("--corporate-number", required=True)
    parse_contact_parser.add_argument("--url", required=True)
    parse_contact_parser.add_argument("--html-file", type=_path, required=True)
    parse_contact_parser.add_argument("--out", type=_path, required=True)

    sales_ready_parser = sub.add_parser("sales-ready", help="抑止・営業利用可否を反映したリストを出力")
    sales_ready_parser.add_argument("--enrichment-db", type=_path)
    sales_ready_parser.add_argument("--max-rows", type=int, default=100_000)
    sales_ready_parser.add_argument("--out", type=_path)

    worker_parser = sub.add_parser("collect-enrichment", help="公式URLを1ページずつ取得し、連絡先を証拠付きで追加")
    worker_parser.add_argument("--enrichment-db", type=_path)
    worker_parser.add_argument("--worker-id", required=True)
    worker_parser.add_argument("--field", choices=("website", "phone", "email", "form_url", "location"))
    worker_parser.add_argument("--source-key")
    worker_parser.add_argument("--batch-size", type=int, default=20)
    worker_parser.add_argument("--max-tasks", type=int, default=100)
    worker_parser.add_argument("--lease-seconds", type=int, default=900)
    worker_parser.add_argument("--timeout", type=float, default=15.0)
    worker_parser.add_argument("--max-bytes", type=int, default=2_000_000)
    worker_parser.add_argument("--interval-seconds", type=float, default=0.25)
    worker_parser.add_argument("--user-agent", default=None)
    worker_parser.add_argument("--only-with-url", action="store_true", help="公式URLがある法人だけを取得対象にする")

    embedded_parser = sub.add_parser(
        "sync-embedded-public",
        help="同梱済み公開データから法人番号付き事業所連絡先を別スコープで同期",
    )
    embedded_parser.add_argument("--enrichment-db", type=_path)

    establishment_parser = sub.add_parser(
        "establishment-list",
        help="本社代表連絡先と分離した公開事業所リストを出力",
    )
    establishment_parser.add_argument("--enrichment-db", type=_path)
    establishment_parser.add_argument("--prefecture")
    establishment_parser.add_argument("--service-type")
    establishment_parser.add_argument("--limit", type=int, default=100_000)
    establishment_parser.add_argument("--out", type=_path)

    configure_parser = sub.add_parser("configure", help="保存設定を表示・更新")
    configure_parser.add_argument("--home", dest="config_home")
    configure_parser.add_argument("--canonical-db", dest="config_canonical_database")
    configure_parser.add_argument("--enrichment-db", dest="config_enrichment_database")
    configure_parser.add_argument("--runtime-db", dest="config_runtime_database")
    configure_parser.add_argument("--search-index", dest="config_search_index")
    configure_parser.add_argument("--default-limit", dest="config_default_limit", type=int)
    configure_parser.add_argument(
        "--validate-index",
        dest="config_validate_index",
        action=argparse.BooleanOptionalAction,
        default=None,
    )

    health_parser = sub.add_parser("app-health", help="現在の設定・DB・索引・機能可否を読み取り専用で表示")
    health_parser.add_argument("--out", type=_path)
    return parser


_RUNTIME_DEFAULT_COMMANDS = frozenset(
    {"search", "daemon", "build-search-index", "summary", "sql", "sources"}
)


def _default_database_for_command(command: str, *, canonical_database: Path, runtime_database: Path) -> Path:
    """Return the safe implicit database for one CLI command.

    Search indexes shipped with the full application are built from the
    integrated runtime database.  Mutation/build commands must keep using the
    canonical database as their input, so changing ``resources.DEFAULT_DB``
    globally would be unsafe.
    """

    if command in _RUNTIME_DEFAULT_COMMANDS:
        return runtime_database
    return canonical_database


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    args = _parser().parse_args(argv)
    configured_settings_path = args.settings or os.environ.get("QUERIA_SETTINGS")
    settings_path = (
        Path(configured_settings_path).expanduser().resolve()
        if configured_settings_path
        else default_settings_path(PROJECT_ROOT)
    )
    settings = load_settings(settings_path)
    artifacts = resolve_artifacts(settings, fallback_home=PROJECT_ROOT)
    if args.db is None:
        args.db = _default_database_for_command(
            args.command,
            canonical_database=artifacts.canonical_database,
            runtime_database=artifacts.runtime_database,
        )
    if hasattr(args, "search_index") and args.search_index is None:
        args.search_index = artifacts.search_index
    if hasattr(args, "enrichment_db") and args.enrichment_db is None:
        args.enrichment_db = artifacts.enrichment_database
    if hasattr(args, "runtime_db") and args.runtime_db is None:
        args.runtime_db = artifacts.runtime_database
    if args.command == "build-search-index" and args.out is None:
        args.out = artifacts.search_index
    if args.command == "build-runtime" and args.out is None:
        args.out = artifacts.runtime_database
    args.settings_path = settings_path
    args.app_settings = settings
    args.resolved_artifacts = artifacts
    return args


def _doctor(db_path: Path, online: bool) -> int:
    report = version_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if sys.version_info < (3, 10):
        raise PipelineError("Python 3.10 以上が必要です。")
    if not db_path.is_file():
        raise PipelineError(f"DB がありません: {db_path}")
    run_local_sql(
        "SELECT scope, row_count, completed_at, parquet_sha256 FROM meta.refresh_log ORDER BY completed_at DESC LIMIT 1",
        db_path=db_path,
        max_rows=10,
    )
    if online:
        print("\n[Queria online probe]")
        print(json.dumps(online_probe(), ensure_ascii=False, indent=2))
    print("\n検証 OK")
    return 0


def _show_sources(db_path: Path) -> int:
    run_local_sql(
        "SELECT source_name, queria_dataset, table_name, role, license_name, attribution FROM meta.source_registry ORDER BY source_name",
        db_path=db_path,
        max_rows=100,
    )
    return 0


def _write_enrichment_rows(path: Path, columns: list[str], rows: list[tuple[object, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.casefold()
    records = [dict(zip(columns, row)) for row in rows]
    if suffix == ".csv":
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(records)
    elif suffix == ".json":
        path.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    else:
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _configure(args: argparse.Namespace) -> int:
    current: AppSettings = args.app_settings
    updates: dict[str, object] = {}
    mapping = {
        "home": "config_home",
        "canonical_database": "config_canonical_database",
        "enrichment_database": "config_enrichment_database",
        "runtime_database": "config_runtime_database",
        "search_index": "config_search_index",
        "default_limit": "config_default_limit",
        "validate_index": "config_validate_index",
    }
    for field_name, argument_name in mapping.items():
        value = getattr(args, argument_name)
        if value is not None:
            updates[field_name] = value
    configured = replace(current, **updates) if updates else current
    if updates:
        save_settings(args.settings_path, configured)
    resolved = resolve_artifacts(configured, fallback_home=PROJECT_ROOT)
    print(
        json.dumps(
            {
                "saved": bool(updates),
                "settings_path": str(args.settings_path),
                "settings": asdict(configured),
                "resolved": {
                    "home": str(resolved.home),
                    "canonical_database": str(resolved.canonical_database),
                    "enrichment_database": str(resolved.enrichment_database),
                    "runtime_database": str(resolved.runtime_database),
                    "search_index": str(resolved.search_index),
                    "origins": dict(resolved.origins),
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    # PyInstaller-launched Windows consoles can inherit the legacy cp932
    # stream even when the project files and JSON are UTF-8.  Reconfigure the
    # process boundary so Japanese results never abort after the query itself
    # has already completed.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    try:
        args = _parse_args(argv)
    except SettingsError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2
    try:
        if args.command == "configure":
            return _configure(args)
        if args.command == "app-health":
            report = inspect_application(args.resolved_artifacts)
            payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
            if args.out is not None:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(payload + "\n", encoding="utf-8")
            print(payload)
            return 0 if report["overall_status"] == "passed" else 1
        if args.command == "refresh":
            result = refresh(
                scope=args.scope,
                database_path=args.db,
                cache_dir=args.cache_dir,
                keep_cache=not args.no_cache,
            )
            print(
                f"完成: {result.database_path}\n"
                f"scope={result.scope}, companies={result.row_count:,}, "
                f"bytes={result.parquet_bytes:,}, manifest_sha256={result.parquet_sha256}"
            )
            if result.artifact_paths:
                print(f"ローカル原本Parquet: {result.parquet_path} ({len(result.artifact_paths)} tables)")
            return 0
        if args.command == "doctor":
            return _doctor(args.db, args.online)
        if args.command == "search":
            search_companies(
                db_path=args.db,
                keyword=args.keyword,
                prefecture=args.prefecture,
                city=args.city,
                industry_majors=args.industry_major,
                industry_middles=args.industry_middle,
                corporate_kinds=args.corporate_kind,
                min_employees=args.min_employees,
                max_employees=args.max_employees,
                min_capital=args.min_capital,
                max_capital=args.max_capital,
                has_web=args.has_web,
                limit=args.limit,
                out=args.out,
                search_index=None if args.no_search_index else args.search_index,
                fast=args.fast,
            )
            return 0
        if args.command == "daemon":
            return run_jsonl_protocol(
                database_path=args.db,
                search_index=args.search_index,
                validate_database=not args.no_index_validation,
            )
        if args.command == "build-search-index":
            stats = build_search_index(args.db, args.out, batch_size=args.batch_size)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        if args.command == "build-runtime":
            stats = build_runtime_database(
                args.db,
                enrichment_path=args.enrichment_db,
                output_path=args.out,
                threads=args.threads,
                memory_limit=args.memory_limit,
            )
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        if args.command == "runtime-summary":
            print(json.dumps(runtime_summary(args.runtime_db), ensure_ascii=False, indent=2))
            return 0
        if args.command == "audit":
            report = audit_database(
                args.db,
                search_index_path=args.search_index,
                enrichment_path=args.enrichment_db,
                runtime_path=None if args.no_runtime else args.runtime_db,
            )
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            print(json.dumps({"status": report["overall_status"], "out": str(args.out), "gates": report["gates"]}, ensure_ascii=False, indent=2))
            if args.strict and report["overall_status"] != "passed":
                return 1
            return 0
        if args.command == "build-semantic-index":
            from .semantic_index import SentenceTransformerProvider

            provider = SentenceTransformerProvider(args.model)
            stats = build_semantic_index(
                search_index_path=args.search_index,
                output_prefix=args.out,
                model=provider,
                batch_size=args.batch_size,
                dtype=args.dtype,
                min_text_chars=args.min_text_chars,
                limit=args.limit,
            )
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        if args.command == "semantic-search":
            semantic_search_companies(
                query_text=args.query,
                model_name=args.model,
                search_index=args.search_index,
                semantic_index=args.semantic_index,
                candidate_keyword=args.candidate_keyword,
                prefecture=args.prefecture,
                city=args.city,
                industry_majors=args.industry_major,
                industry_middles=args.industry_middle,
                has_web=args.has_web,
                candidate_limit=args.candidate_limit,
                limit=args.limit,
                out=args.out,
                device=args.device,
            )
            return 0
        if args.command == "summary":
            show_summary(args.db)
            return 0
        if args.command == "sql":
            sql = args.query if args.query is not None else args.file.read_text(encoding="utf-8")
            run_local_sql(sql, db_path=args.db, max_rows=args.max_rows, out=args.out)
            return 0
        if args.command == "sources":
            return _show_sources(args.db)
        if args.command == "init-enrichment":
            print(json.dumps(initialize_enrichment_database(args.db, args.enrichment_db), ensure_ascii=False, indent=2))
            return 0
        if args.command == "sync-embedded-public":
            print(
                json.dumps(
                    sync_embedded_public_enrichment(args.db, args.enrichment_db),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "seed-enrichment":
            print(
                json.dumps(
                    seed_enrichment(
                        args.db,
                        enrichment_path=args.enrichment_db,
                        limit=args.limit,
                        source_key=args.source_key,
                        industry_major=args.industry_major,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "import-enrichment":
            print(
                json.dumps(
                    import_enrichment_jsonl(
                        args.db,
                        args.file,
                        enrichment_path=args.enrichment_db,
                        batch_size=args.batch_size,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "claim-enrichment":
            print(
                json.dumps(
                    claim_enrichment_tasks(
                        args.db,
                        enrichment_path=args.enrichment_db,
                        worker_id=args.worker_id,
                        field_name=args.field,
                        source_key=args.source_key,
                        batch_size=args.batch_size,
                        lease_seconds=args.lease_seconds,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "complete-enrichment":
            complete_enrichment_task(
                args.db,
                enrichment_path=args.enrichment_db,
                corporate_number=args.corporate_number,
                field_name=args.field_name,
                source_key=args.source_key,
                state=args.state,
                worker_id=args.worker_id,
                evidence_id=args.evidence_id,
                error=args.error,
            )
            print("完了状態へ更新しました。")
            return 0
        if args.command == "parse-contact-page":
            from .enrichment_extract import extract_contact_records

            records = extract_contact_records(
                args.html_file.read_text(encoding="utf-8"),
                args.corporate_number,
                args.url,
            )
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(json.dumps({"records": len(records), "out": str(args.out)}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "sales-ready":
            columns, rows = export_sales_ready_accounts(
                args.db,
                enrichment_path=args.enrichment_db,
                max_rows=args.max_rows,
            )
            if args.out:
                _write_enrichment_rows(args.out, columns, rows)
                print(json.dumps({"rows": len(rows), "out": str(args.out)}, ensure_ascii=False, indent=2))
            else:
                print(json.dumps([dict(zip(columns, row)) for row in rows], ensure_ascii=False, default=str, indent=2))
            return 0
        if args.command == "establishment-list":
            columns, rows = export_establishment_contacts(
                args.db,
                enrichment_path=args.enrichment_db,
                prefecture=args.prefecture,
                service_type=args.service_type,
                max_rows=args.limit,
            )
            if args.out:
                _write_enrichment_rows(args.out, columns, rows)
                print(json.dumps({"rows": len(rows), "out": str(args.out)}, ensure_ascii=False, indent=2))
            else:
                print(json.dumps([dict(zip(columns, row)) for row in rows], ensure_ascii=False, default=str, indent=2))
            return 0
        if args.command == "collect-enrichment":
            print(
                json.dumps(
                    run_enrichment_worker(
                        args.db,
                        enrichment_path=args.enrichment_db,
                        worker_id=args.worker_id,
                        field_name=args.field,
                        source_key=args.source_key,
                        batch_size=args.batch_size,
                        max_tasks=args.max_tasks,
                        lease_seconds=args.lease_seconds,
                        timeout=args.timeout,
                        max_bytes=args.max_bytes,
                        interval_seconds=args.interval_seconds,
                        respect_robots=True,
                        user_agent=args.user_agent or "queria-master-enrichment/0.9 (+public-data-contact-research)",
                        only_with_url=args.only_with_url,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        raise AssertionError(f"Unhandled command: {args.command}")
    except (
        PipelineError,
        EnrichmentError,
        RuntimeBuildError,
        AuditError,
        SettingsError,
        OSError,
        ValueError,
        SemanticIndexError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
