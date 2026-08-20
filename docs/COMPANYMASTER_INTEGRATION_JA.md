# CompanyMaster 統合メモ

添付された `CompanyScout_DuckDBNative_v0.2_Source.zip` のソースを `company_scout/` として統合し、アプリの表示名と配布識別子を `CompanyMaster` に統一しました。ZIP内の `AGENTS.md` や README は実装仕様の参考資料として扱い、リポジトリの既存データ・ライセンス・ユーザー要件を優先しています。

## 統合した経路

- `company_scout/src-tauri/` に Tauri + Rust + bundled DuckDB を配置
- `data/queria_runtime.duckdb` をアプリへコピーせず、DuckDB の `ATTACH ... (READ_ONLY)` で接続
- `QUERIA_RUNTIME_DB` または `QUERIA_MASTER_HOME` でランタイムDBを明示可能
- 約582万法人の検索対象は外部 runtime DB のビュー、検索メモリー・企業リスト・調査レポートは CompanyMaster の sidecar DB
- 自然文の条件解釈とWeb調査は、ユーザー本人のChatGPTログインを使う Codex App Server 経由
- `gpt-5.6-luna` を `model/list`、thread、turn、reroute の各段階で固定し、利用不能時はフォールバックしない
- Salesforce は Authorization Code + PKCE と Bulk API 2.0 Upsert。法人番号を外部IDにする
- 電話番号は `search.company_documents.phone` または選択企業の公式サイト抽出結果を法人番号で結合し、画面・CSV・SalesforceのPhoneへ流す

## Windowsビルド

```powershell
cd company_scout
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup-windows.ps1
.\scripts\build-windows.ps1
```

生成物は `company_scout/src-tauri/target/release/bundle/` に出力されます。全量DBを隣接配置した配布先では、必要に応じて次を設定して `CompanyMaster.exe` を起動します。

```powershell
$env:QUERIA_RUNTIME_DB = 'D:\Queria\data\queria_runtime.duckdb'
```

## この環境での検証範囲

現在の実行環境には Rust/Cargo と Node.js/npm がなく、Windows EXEのコンパイル・起動までは実施していません。代わりに、既存 `data/queria_runtime.duckdb` への DuckDB read-only attach、ビュー列変換、5,824,180件の件数取得、業種・従業員・Web有無の検索SQLを Python DuckDB で確認しています。Windows上ではセットアップスクリプトとGitHub ActionsのWindowsビルドでコンパイル検証してください。

## 未実装・今後の拡張

現統合版はCSV / XLSX出力、Salesforceジョブ状態ポーリング・失敗行再送、項目マッピングUIまで実装しています。XLSXはExcelの1シート上限のため、100万社超はCSVを使用してください。企業リスト単位の一括Web調査は次の拡張対象です。
