from __future__ import annotations

import argparse
import csv
import os
import sys
import time
import webbrowser
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .app_config import (
    AppSettings,
    ResolvedArtifacts,
    SettingsError,
    default_settings_path,
    load_settings,
    resolve_artifacts,
    save_settings,
)
from .health import inspect_application
from .resident import ResidentSearchSession
from .resources import DEFAULT_DB, PROJECT_ROOT, discover_project_root
from .search_index import DEFAULT_SEARCH_INDEX


DISPLAY_COLUMNS = (
    ("corporate_number", "法人番号", 150),
    ("company_name", "法人名", 280),
    ("corporate_kind_code", "法人種別", 85),
    ("prefecture_name", "都道府県", 90),
    ("city_name", "市区町村", 130),
    ("employee_number", "従業員", 75),
    ("capital_stock", "資本金", 100),
    ("company_url", "公開URL候補", 250),
    ("phone", "代表連絡先", 130),
    ("email", "公開メール", 210),
    ("inquiry_form_url", "問い合わせフォーム", 240),
    ("business_summary", "事業概要", 320),
)


def _frozen_home() -> Path:
    return discover_project_root()


def _default_path(name: str) -> Path:
    return _frozen_home() / "data" / name


def _format_value(key: str, value: Any) -> str:
    if value is None:
        return ""
    if key in {"employee_number", "capital_stock"}:
        try:
            return f"{int(value):,}"
        except (TypeError, ValueError):
            pass
    return str(value)


class DesktopSearchApp:
    """Small resident desktop shell around one immutable SearchIndex connection."""

    def __init__(
        self,
        *,
        database_path: Path,
        search_index: Path,
        initial_keyword: str = "",
        initial_limit: int = 1000,
        validate_index: bool = True,
        settings_path: Path | None = None,
        settings: AppSettings | None = None,
        artifacts: ResolvedArtifacts | None = None,
    ):
        import tkinter as tk
        from tkinter import messagebox, ttk

        self._tk = tk
        self._ttk = ttk
        self._messagebox = messagebox
        self.root = tk.Tk()
        self.root.title("Queria 高速法人検索")
        self.root.geometry("1280x760")
        self.root.minsize(980, 560)
        self.root.option_add("*Font", ("Yu Gothic UI", 10))

        self.database_path = Path(database_path).resolve()
        self.search_index_path = Path(search_index).resolve()
        self.validate_index = bool(validate_index)
        self.settings_path = Path(settings_path) if settings_path is not None else default_settings_path(_frozen_home())
        self.settings = settings or AppSettings(home=str(_frozen_home()))
        self.artifacts = artifacts
        self.session: ResidentSearchSession | None = None
        self.startup_error: Exception | None = None
        try:
            self.session = ResidentSearchSession(
                database_path=self.database_path,
                search_index=self.search_index_path,
                validate_database=self.validate_index,
            )
        except Exception as exc:
            self.startup_error = exc
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="queria-search")
        self.generation = 0
        self.rows: list[dict[str, Any]] = []
        self.last_elapsed_ms = 0.0

        self.keyword = tk.StringVar(value=initial_keyword)
        self.prefecture = tk.StringVar()
        self.city = tk.StringVar()
        self.major = tk.StringVar()
        self.middle = tk.StringVar()
        self.corporate_kind = tk.StringVar()
        self.min_employees = tk.StringVar()
        self.max_employees = tk.StringVar()
        self.min_capital = tk.StringVar()
        self.max_capital = tk.StringVar()
        self.limit = tk.IntVar(value=max(1, min(initial_limit, 1000)))
        self.has_web = tk.BooleanVar(value=False)
        if self.session is None:
            initial_status = f"設定エラー: {self.startup_error}（設定・診断を開いて修正してください）"
        else:
            count = int(self.session.metadata.get("row_count", "0") or 0)
            initial_status = f"索引を開きました: {count:,}法人 | {self.database_path.name}"
        self.status = tk.StringVar(value=initial_status)
        self._build_widgets()
        if self.session is None:
            self.search_button.configure(state="disabled")
            self.root.after(
                150,
                lambda: self._messagebox.showerror(
                    "設定エラー",
                    f"検索DBと索引を開けませんでした。\n\n{self.startup_error}\n\n"
                    "［設定・診断］から正しいファイルを選択してください。",
                ),
            )
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Return>", lambda _event: self.submit())
        self.root.bind("<Control-f>", lambda _event: self.keyword_entry.focus_set())

    def _build_widgets(self) -> None:
        tk = self._tk
        ttk = self._ttk
        filters = ttk.Frame(self.root, padding=(10, 10, 10, 4))
        filters.pack(fill="x")

        ttk.Label(filters, text="キーワード").grid(row=0, column=0, sticky="w")
        self.keyword_entry = ttk.Entry(filters, textvariable=self.keyword, width=34)
        self.keyword_entry.grid(row=0, column=1, sticky="ew", padx=(5, 12))
        ttk.Label(filters, text="都道府県").grid(row=0, column=2, sticky="w")
        ttk.Entry(filters, textvariable=self.prefecture, width=12).grid(row=0, column=3, padx=(5, 12))
        ttk.Label(filters, text="市区町村").grid(row=0, column=4, sticky="w")
        ttk.Entry(filters, textvariable=self.city, width=14).grid(row=0, column=5, padx=(5, 12))
        ttk.Label(filters, text="JSIC中分類").grid(row=0, column=6, sticky="w")
        ttk.Entry(filters, textvariable=self.middle, width=6).grid(row=0, column=7, padx=(5, 12))
        ttk.Checkbutton(filters, text="URLあり", variable=self.has_web).grid(row=0, column=8, padx=(0, 12))
        ttk.Label(filters, text="件数").grid(row=0, column=9, sticky="w")
        ttk.Spinbox(filters, from_=1, to=1000, textvariable=self.limit, width=7).grid(row=0, column=10, padx=(5, 12))
        self.search_button = ttk.Button(filters, text="検索 (Enter)", command=self.submit)
        self.search_button.grid(row=0, column=11, padx=(0, 6))
        self.export_button = ttk.Button(filters, text="CSV出力", command=self.export_csv, state="disabled")
        self.export_button.grid(row=0, column=12)
        self.settings_button = ttk.Button(filters, text="設定・診断", command=self.open_settings)
        self.settings_button.grid(row=0, column=13, padx=(6, 0))

        ttk.Label(filters, text="JSIC大分類").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(filters, textvariable=self.major, width=8).grid(
            row=1, column=1, sticky="w", padx=(5, 12), pady=(8, 0)
        )
        ttk.Label(filters, text="従業員 最小").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(filters, textvariable=self.min_employees, width=10).grid(
            row=1, column=3, padx=(5, 12), pady=(8, 0)
        )
        ttk.Label(filters, text="最大").grid(row=1, column=4, sticky="w", pady=(8, 0))
        ttk.Entry(filters, textvariable=self.max_employees, width=10).grid(
            row=1, column=5, padx=(5, 12), pady=(8, 0)
        )
        ttk.Label(filters, text="資本金 最小").grid(row=1, column=6, sticky="w", pady=(8, 0))
        ttk.Entry(filters, textvariable=self.min_capital, width=14).grid(
            row=1, column=7, padx=(5, 12), pady=(8, 0)
        )
        ttk.Label(filters, text="最大").grid(row=1, column=8, sticky="w", pady=(8, 0))
        ttk.Entry(filters, textvariable=self.max_capital, width=14).grid(
            row=1, column=9, padx=(5, 12), pady=(8, 0)
        )
        ttk.Label(filters, text="法人種別").grid(row=1, column=10, sticky="w", pady=(8, 0))
        ttk.Entry(filters, textvariable=self.corporate_kind, width=10).grid(
            row=1, column=11, padx=(5, 12), pady=(8, 0)
        )
        filters.columnconfigure(1, weight=1)

        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=10, pady=(0, 5))

        table_frame = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        table_frame.pack(fill="both", expand=True)
        keys = [item[0] for item in DISPLAY_COLUMNS]
        self.tree = ttk.Treeview(table_frame, columns=keys, show="headings", selectmode="browse")
        for key, heading, width in DISPLAY_COLUMNS:
            self.tree.heading(key, text=heading)
            self.tree.column(key, width=width, minwidth=50, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.bind("<Double-1>", self.open_selected_url)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        ttk.Label(self.root, textvariable=self.status, anchor="w", padding=(10, 6)).pack(fill="x")

    def _request(self) -> dict[str, Any]:
        def optional_int(value: str) -> int | None:
            value = value.strip().replace(",", "")
            return int(value) if value else None

        try:
            limit = max(1, min(int(self.limit.get()), 1000))
        except (TypeError, ValueError):
            limit = 1000
        middle = self.middle.get().strip()
        major = self.major.get().strip().upper()
        corporate_kind = self.corporate_kind.get().strip()
        return {
            "keyword": self.keyword.get().strip() or None,
            "prefecture": self.prefecture.get().strip() or None,
            "city": self.city.get().strip() or None,
            "industry_majors": (major,) if major else (),
            "industry_middles": (middle,) if middle else (),
            "corporate_kinds": (corporate_kind,) if corporate_kind else (),
            "min_employees": optional_int(self.min_employees.get()),
            "max_employees": optional_int(self.max_employees.get()),
            "min_capital": optional_int(self.min_capital.get()),
            "max_capital": optional_int(self.max_capital.get()),
            "has_web": bool(self.has_web.get()),
            "limit": limit,
        }

    def submit(self) -> None:
        if self.session is None:
            self._messagebox.showerror("設定エラー", "検索DBと索引が有効ではありません。設定・診断を確認してください。")
            return
        self.generation += 1
        generation = self.generation
        try:
            request = self._request()
        except ValueError:
            self._messagebox.showerror("入力エラー", "従業員数と資本金は整数で入力してください。")
            return
        started = time.perf_counter()
        self.search_button.configure(state="disabled")
        self.settings_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.progress.start(8)
        self.status.set("検索中…")
        future = self.executor.submit(self.session.search, request)
        future.add_done_callback(
            lambda completed: self.root.after(0, self._search_finished, generation, started, completed)
        )

    def _search_finished(self, generation: int, started: float, future: Future[list[dict[str, Any]]]) -> None:
        if generation != self.generation:
            return
        self.progress.stop()
        self.search_button.configure(state="normal")
        self.settings_button.configure(state="normal")
        try:
            rows = future.result()
        except Exception as exc:
            self.status.set(f"検索エラー: {exc}")
            self._messagebox.showerror("検索エラー", str(exc))
            return
        self.rows = rows
        self.last_elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.tree.delete(*self.tree.get_children())
        self._render_batch(rows, 0, generation)

    def _render_batch(self, rows: list[dict[str, Any]], offset: int, generation: int) -> None:
        if generation != self.generation:
            return
        batch_end = min(offset + 100, len(rows))
        keys = [item[0] for item in DISPLAY_COLUMNS]
        for row in rows[offset:batch_end]:
            self.tree.insert("", "end", values=tuple(_format_value(key, row.get(key)) for key in keys))
        if batch_end < len(rows):
            self.root.after_idle(self._render_batch, rows, batch_end, generation)
            self.status.set(f"{batch_end:,}/{len(rows):,}件を表示中…")
        else:
            self.export_button.configure(state="normal" if rows else "disabled")
            self.status.set(f"{len(rows):,}件 | 検索・取得 {self.last_elapsed_ms:.1f}ms")

    def export_csv(self) -> None:
        if not self.rows:
            return
        from tkinter import filedialog

        path = filedialog.asksaveasfilename(
            title="検索結果をCSVへ保存",
            defaultextension=".csv",
            filetypes=(("CSV", "*.csv"), ("すべてのファイル", "*.*")),
        )
        if not path:
            return
        keys = [item[0] for item in DISPLAY_COLUMNS]
        with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.rows)
        self.status.set(f"CSVを保存しました: {path}")

    def open_selected_url(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        values = self.tree.item(selection[0], "values")
        url_index = [item[0] for item in DISPLAY_COLUMNS].index("company_url")
        url = values[url_index] if len(values) > url_index else ""
        if url:
            webbrowser.open(url)

    def open_settings(self) -> None:
        tk = self._tk
        ttk = self._ttk
        window = tk.Toplevel(self.root)
        window.title("Queria 設定・診断")
        window.geometry("920x500")
        window.transient(self.root)

        current_home = self.artifacts.home if self.artifacts is not None else _frozen_home()
        values = {
            "home": tk.StringVar(value=str(current_home)),
            "canonical_database": tk.StringVar(
                value=str(self.artifacts.canonical_database if self.artifacts else _default_path("queria_master.duckdb"))
            ),
            "enrichment_database": tk.StringVar(
                value=str(self.artifacts.enrichment_database if self.artifacts else _default_path("queria_enrichment.duckdb"))
            ),
            "runtime_database": tk.StringVar(value=str(self.database_path)),
            "search_index": tk.StringVar(value=str(self.search_index_path)),
            "default_limit": tk.StringVar(value=str(self.limit.get())),
            "validate_index": tk.BooleanVar(value=self.validate_index),
        }
        labels = (
            ("home", "アプリホーム"),
            ("canonical_database", "Canonical DB（更新元）"),
            ("enrichment_database", "Enrichment DB（証拠付き拡張）"),
            ("runtime_database", "Runtime DB（検索用）"),
            ("search_index", "検索インデックス"),
            ("default_limit", "既定表示件数"),
        )
        body = ttk.Frame(window, padding=12)
        body.pack(fill="both", expand=True)
        for row, (key, label) in enumerate(labels):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=4)
            width = 12 if key == "default_limit" else 90
            ttk.Entry(body, textvariable=values[key], width=width).grid(
                row=row, column=1, sticky="ew", padx=(10, 0), pady=4
            )
        ttk.Checkbutton(body, text="起動時にDBと索引の対応を検証", variable=values["validate_index"]).grid(
            row=len(labels), column=1, sticky="w", padx=(10, 0), pady=4
        )
        result = tk.StringVar(
            value=f"設定ファイル: {self.settings_path}\n"
            f"現在: DB={self.database_path} / INDEX={self.search_index_path}"
        )
        ttk.Label(body, textvariable=result, anchor="w", justify="left", wraplength=850).grid(
            row=len(labels) + 1, column=0, columnspan=2, sticky="ew", pady=(12, 8)
        )
        body.columnconfigure(1, weight=1)

        def candidate() -> tuple[AppSettings, ResolvedArtifacts]:
            try:
                limit = int(values["default_limit"].get())
            except ValueError as exc:
                raise SettingsError("既定表示件数は整数で指定してください。") from exc
            app_settings = AppSettings(
                home=values["home"].get().strip() or None,
                canonical_database=values["canonical_database"].get().strip(),
                enrichment_database=values["enrichment_database"].get().strip(),
                runtime_database=values["runtime_database"].get().strip(),
                search_index=values["search_index"].get().strip(),
                default_limit=limit,
                validate_index=bool(values["validate_index"].get()),
            )
            resolved = resolve_artifacts(
                app_settings,
                fallback_home=current_home,
                environment={},
            )
            return app_settings, resolved

        def check_only() -> None:
            try:
                _settings, resolved = candidate()
                report = inspect_application(resolved)
                if report["overall_status"] != "passed":
                    raise SettingsError(" / ".join(report["errors"]))
                metadata = report["search_index_metadata"] or {}
                count = int(metadata.get("row_count", "0") or 0)
                refresh_id = metadata.get("refresh_id", "不明")
                enabled = [
                    key for key, value in report["capabilities"].items() if value.get("enabled")
                ]
                result.set(
                    f"検証OK: {count:,}法人 / refresh={refresh_id}\n"
                    f"Runtime DB: {resolved.runtime_database}\nIndex: {resolved.search_index}\n"
                    f"有効機能: {', '.join(enabled)}"
                )
            except Exception as exc:
                result.set(f"検証失敗: {exc}")
                self._messagebox.showerror("設定検証エラー", str(exc), parent=window)

        def save_and_apply() -> None:
            try:
                app_settings, resolved = candidate()
                new_session = ResidentSearchSession(
                    database_path=resolved.runtime_database,
                    search_index=resolved.search_index,
                    validate_database=resolved.validate_index,
                )
                save_settings(self.settings_path, app_settings)
            except Exception as exc:
                self._messagebox.showerror("設定保存エラー", str(exc), parent=window)
                return
            old_session = self.session
            self.session = new_session
            if old_session is not None:
                old_session.close()
            self.settings = app_settings
            self.artifacts = resolved
            self.database_path = resolved.runtime_database
            self.search_index_path = resolved.search_index
            self.validate_index = resolved.validate_index
            self.limit.set(max(1, min(resolved.default_limit, 1000)))
            self.search_button.configure(state="normal")
            count = int(new_session.metadata.get("row_count", "0") or 0)
            self.status.set(f"設定を保存・反映しました: {count:,}法人 | {self.database_path.name}")
            window.destroy()

        actions = ttk.Frame(body)
        actions.grid(row=len(labels) + 2, column=0, columnspan=2, sticky="e")
        ttk.Button(actions, text="検証", command=check_only).pack(side="left", padx=4)
        ttk.Button(actions, text="保存して反映", command=save_and_apply).pack(side="left", padx=4)
        ttk.Button(actions, text="閉じる", command=window.destroy).pack(side="left", padx=4)

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
        if self.session is not None:
            self.session.close()
        self.root.destroy()

    def run(self) -> int:
        self.keyword_entry.focus_set()
        self.root.mainloop()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Queria resident desktop search application")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--search-index", type=Path)
    parser.add_argument("--settings", type=Path)
    parser.add_argument("--keyword", default="")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    fallback_home = _frozen_home()
    configured_settings_path = args.settings or os.environ.get("QUERIA_SETTINGS")
    settings_path = (
        Path(configured_settings_path).expanduser().resolve()
        if configured_settings_path
        else default_settings_path(fallback_home)
    )
    try:
        settings = load_settings(settings_path)
    except SettingsError as exc:
        settings = AppSettings(home=str(fallback_home))
        settings_load_error: Exception | None = exc
    else:
        settings_load_error = None
    artifacts = resolve_artifacts(
        settings,
        fallback_home=fallback_home,
        explicit={
            "runtime_database": args.db,
            "search_index": args.search_index,
        },
    )
    app = DesktopSearchApp(
        database_path=artifacts.runtime_database,
        search_index=artifacts.search_index,
        initial_keyword=args.keyword,
        initial_limit=args.limit if args.limit is not None else artifacts.default_limit,
        validate_index=artifacts.validate_index,
        settings_path=settings_path,
        settings=settings,
        artifacts=artifacts,
    )
    if settings_load_error is not None:
        app.status.set(f"設定ファイルを読めません: {settings_load_error}（既定値で起動）")
    return app.run()


__all__ = ["DesktopSearchApp", "main"]
