# EXE高速検索のGitHub・一次情報調査

調査日: 2026-08-19  
対象: 5,823,039法人を保持するWindows版Queria  
要求: 1,000件の検索・一覧を約0.5秒級で返し、EXE上で滑らかに表示する

## ローカルで確認した現状

同一プロセス内のSQLite FTS検索は、5,823,039法人の `ソフトウェア` 1,000件でp50 9.035msだった。一方、PyInstaller onefile EXEを新規プロセスで起動して1,000件をCSVへ出力した従来の総時間は約1.7〜2.1秒だった。したがって、支配項はFTS検索ではなく、EXE起動、Python初期化、結果整形、出力処理の合算である。この測定は `work/bench/exe_baseline.json` に保存している。常駐経路の最新実測は下表と `work/bench` の検証記録に分離している。

## 採用判断

| 候補 | 判断 | 理由 |
|---|---|---|
| SQLite FTS5 | 採用継続 | 既存の大規模索引をそのまま使え、`MATCH`、trigram、prefix、external-contentを持つ。 |
| DuckDB | 分析・更新・大規模出力へ限定 | 検索ごとの整合性確認接続は外し、更新時に検証する。 |
| 常駐検索エンジン | 採用 | DB／索引を一度開き、検索ごとのプロセス起動とDuckDB接続をなくす。 |
| PySide6/Qt model-view | 参考採用 | 将来の高機能UI候補。ただし現環境では未導入のため、今回の最小EXEは標準Tkのresident modelを先に検証する。 |
| PyInstaller onedir | 採用 | 起動時の展開を避ける高速配布形態。 |
| PyInstaller onefile | 互換配布 | 単一ファイルは便利だが、毎回 `_MEI` へ展開するため速度計測の基準版にはしない。 |
| Tantivy | 将来候補 | Rustネイティブ検索として強力だが、Python連携・DLL・インデックス更新の複雑性が増える。 |
| Meilisearch / Typesense | 今回不採用 | 常駐サーバー・IPC・別ライセンス・メモリ管理が増え、既存SQLite索引の交換理由がない。 |
| Tauri | 将来のUI再構築候補 | 小型のRust/WebViewアプリは魅力的だが、今回のPython検索本体を書き直すほどの測定根拠はない。 |

## 実EXEの常駐ベンチマーク

同じ全量ランタイムDBと2.4GBのSQLite FTS5索引を使い、ウォームアップ2回後に5回測定した。`roundtrip` はEXEとのJSONL往復、`server` はEXE内部の検索処理で、いずれもプロセスを起動し直さない常駐状態の値である。

| 配布形態 | 返却件数 | roundtrip p50 | server p50 | 結果 |
|---|---:|---:|---:|---|
| onefile CLI daemon | 10 | 0.736 ms | 0.646 ms | 10件 |
| onefile CLI daemon | 100 | 2.634 ms | 2.229 ms | 100件 |
| onefile CLI daemon | 1,000 | 18.074 ms | 15.154 ms | 1,000件 |
| onedir CLI daemon | 10 | 0.475 ms | 0.393 ms | 10件 |
| onedir CLI daemon | 100 | 1.530 ms | 1.228 ms | 100件 |
| onedir CLI daemon | 1,000 | 13.023 ms | 10.315 ms | 1,000件 |

このため、検索要求そのものは0.5秒を大きく下回る。今回の基準ビルドの別プロセス測定では、onefileの初回pingまで約1.4秒、onedirは約0.24秒だった（ウイルス対策・OSキャッシュで変動する）。修正版EXEの再起動はこのホストのアプリケーション制御ポリシー WinError 4551 でブロックされたため、13.023msは基準ビルドの測定値として記録し、現行ソースdaemonの14.398msと分けて扱う。画面版は起動後に接続を保持するonedirを標準とする。初回起動、GUIのTreeview反映、ディスクへのCSV出力は検索処理と別の測定項目であり、この結果だけで「起動から初回描画まで0.5秒」とは主張しない。

## 一次情報・GitHub

1. [SQLite FTS5公式](https://sqlite.org/fts5.html) — FTS5の `MATCH`、prefix index、trigram、external-content tableが仕様化されている。prefix indexはprefix queryのrange scanを減らすが、0.5秒を保証する資料ではない。
2. [DuckDB Python conversion公式](https://duckdb.org/docs/current/clients/python/conversion) — `to_arrow_table()` と `to_arrow_reader(chunk_size)` が提供され、分析・大規模出力ではPython辞書列挙より候補になる。インタラクティブ画面の直接測定は別途必要。
3. [Tantivy GitHub](https://github.com/quickwit-oss/tantivy) — MITライセンスのRust全文検索ライブラリ。immutable segmentとcommit/reloadのライフサイクルを持つ。将来のRust索引候補だが、現行EXEへ直ちに移す根拠は不足。
4. [Meilisearch GitHub](https://github.com/meilisearch/meilisearch) — ローカルで動かせる検索サーバーだが、別プロセス・HTTP/IPC・ライセンス管理が必要で、今回の組み込み検索には過剰。
5. [Typesense GitHub](https://github.com/typesense/typesense) — インメモリ型のサーバー検索を提供するが、GPL-3.0のサーバーと別デーモンを導入するため、現行Python EXEへの直接採用は見送る。
6. [PyInstaller公式: one-folder / one-file](https://pyinstaller.org/en/stable/operating-mode.html) — one-folderは依存ファイルを隣接配置し、one-fileは起動時に一時 `_MEI` へ展開する。速度優先の基準配布はone-folderとする。
7. [PySide6 QAbstractItemModel公式](https://doc.qt.io/qtforpython-6/PySide6/QtCore/QAbstractItemModel.html) — model/viewは遅延取得・`fetchMore()`が可能で、GUIスレッドとデータ取得を分離する前提がある。Qt移行時の設計根拠にする。
8. [Tauri Architecture公式](https://tauri.app/concept/architecture/) — Rust backendとOS WebViewをmessage passingで接続でき、小型の常駐アプリを作れる。将来のUI再構築候補として参照する。

## 実装方針

まずSQLite索引をresident processで一度だけ開く。索引のfreshnessは毎回DuckDBを開くのではなく、検索索引へ記録したrefresh_id・DBサイズ・mtime等の小さなmanifestで確認する。UIは検索workerからコンパクトなtupleを受け、画面スレッドで一定件数ずつ反映する。1,000件の取得時間、1,000件のモデル反映時間、初回paint、CSV出力時間を別々に測定する。

GPT Proのsource-onlyレビューも統合した。レビューで見つかったカテゴリ先頭検索の重複リスク（大分類行と中分類行が同じ法人へ複数行を作る問題）は、`company_categories` から `SELECT DISTINCT doc_id` で法人集合を作る経路へ修正し、カテゴリ検索の件数・一意性・LIMITを回帰テストで固定した。3文字未満または特殊文字を含む `--fast` 検索は、全列の先頭ワイルドカード走査を避けて社名前方一致へ切り替える仕様を維持する。完全な部分一致が必要な場合は `--fast` を外す。

## 未検証のまま残すもの

- Windows実機での1,000行初回paint p50/p95
- Windowsの低速ディスク・ウイルス対策ソフト下での初回起動差（今回の環境ではonefile約1.4秒、onedir約0.24秒。ただし同一ホストでも変動）
- 新しく生成したGUI EXEの起動は、このホストのアプリケーション制御ポリシー WinError 4551 によりブロックされた。GUIソースは5秒常駐を確認済みで、配布先の署名・Smart App Control環境で追加検証が必要。
- 低速ディスク・ウイルス対策ソフト下の起動差
- 画面描画を含む0.5秒達成
- PySide6またはTauriへ移行した場合の総メモリと保守コスト

本資料は調査とローカル測定を分離した設計判断であり、外部プロジェクトのベンチマーク値をQueriaの性能保証として扱わない。
