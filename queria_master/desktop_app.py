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

from .resident import ResidentSearchSession
from .resources import DEFAULT_DB, PROJECT_ROOT
from .search_index import DEFAULT_SEARCH_INDEX


DISPLAY_COLUMNS = (
    ("corporate_number", "法人番号", 150),
    ("company_name", "法人名", 280),
    ("prefecture_name", "都道府県", 90),
    ("city_name", "市区町村", 130),
    ("employee_number", "従業員", 75),
    ("capital_stock", "資本金", 100),
    ("company_url", "公式URL", 250),
)


def _frozen_home() -> Path:
    override = os.environ.get("QUERIA_MASTER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        # Support both layouts used by the release bundles:
        #   <bundle>\data\...
        #   <release-root>\data\...\<bundle>\<app>.exe
        candidates = (executable_dir, executable_dir.parent, Path.cwd())
        for candidate in candidates:
            if (candidate / "data" / "queria_runtime.duckdb").is_file() and (
                candidate / "data" / "search.sqlite"
            ).is_file():
                return candidate
        return executable_dir
    return PROJECT_ROOT


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

    def __init__(self, *, database_path: Path, search_index: Path, initial_keyword: str = "", initial_limit: int = 1000):
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

        self.session = ResidentSearchSession(database_path=database_path, search_index=search_index)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="queria-search")
        self.generation = 0
        self.rows: list[dict[str, Any]] = []
        self.last_elapsed_ms = 0.0

        self.keyword = tk.StringVar(value=initial_keyword)
        self.prefecture = tk.StringVar()
        self.city = tk.StringVar()
        self.middle = tk.StringVar()
        self.limit = tk.IntVar(value=max(1, min(initial_limit, 1000)))
        self.has_web = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="索引を開きました。検索語を入力してください。")
        self._build_widgets()
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
        try:
            limit = max(1, min(int(self.limit.get()), 1000))
        except (TypeError, ValueError):
            limit = 1000
        middle = self.middle.get().strip()
        return {
            "keyword": self.keyword.get().strip() or None,
            "prefecture": self.prefecture.get().strip() or None,
            "city": self.city.get().strip() or None,
            "industry_middles": (middle,) if middle else (),
            "has_web": bool(self.has_web.get()),
            "limit": limit,
        }

    def submit(self) -> None:
        self.generation += 1
        generation = self.generation
        request = self._request()
        started = time.perf_counter()
        self.search_button.configure(state="disabled")
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

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.session.close()
        self.root.destroy()

    def run(self) -> int:
        self.keyword_entry.focus_set()
        self.root.mainloop()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Queria resident desktop search application")
    parser.add_argument("--db", type=Path, default=_default_path("queria_runtime.duckdb"))
    parser.add_argument("--search-index", type=Path, default=_default_path("search.sqlite"))
    parser.add_argument("--keyword", default="")
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args(argv)
    app = DesktopSearchApp(
        database_path=args.db,
        search_index=args.search_index,
        initial_keyword=args.keyword,
        initial_limit=args.limit,
    )
    return app.run()


__all__ = ["DesktopSearchApp", "main"]
