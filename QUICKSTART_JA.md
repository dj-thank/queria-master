# 最短クイックスタート

## 0.9 完全版を使う場合

完全版ZIPは`data`を自動検出します。まず状態を確認します。

```powershell
.\queria-master-cli\queria-master.exe app-health
```

設定変更:

```powershell
.\queria-master-cli\queria-master.exe configure --home D:\Queria
```

GUIは右上の［設定・診断］から同じ設定を保存できます。検索用runtime DBとindexの`generation_id`が一致しない場合は検索を開始しません。

本社代表連絡先と分離した公開事業所リスト:

```powershell
.\queria-master-cli\queria-master.exe establishment-list `
  --prefecture 東京都 --limit 10000 --out .\tokyo-establishments.csv
```

## Windows

1. ZIP を展開
2. `01_初回セットアップ.bat` をダブルクリック
3. 完了後、`03_検索サンプル.bat` をダブルクリック

既定では24テーブルの全公開スコープを処理するため、生成物:

```text
data/queria_master.duckdb
data/queria_enrichment.duckdb
data/queria_runtime.duckdb
data/search.sqlite
cache/all-public-latest/*.parquet
exports/
```

全量は大容量です（現行スナップショットでParquet約6.64GB、DuckDB約28.5GB）。情報通信業だけを先に試す場合は、更新時に `--scope info-communications` を指定してください。

検索例:

```powershell
.\.venv\Scripts\python.exe -m queria_master search --keyword ソフトウェア --prefecture 東京都 --limit 50

# 全量索引を使った高速検索（初回構築後）
.\.venv\Scripts\python.exe -m queria_master search --keyword ソフトウェア --fast --limit 50

# 全業種のカテゴリ検索（JSIC大分類E、製造業の中分類09など）
.\.venv\Scripts\python.exe -m queria_master search --industry-major E --industry-middle 09 --limit 50
```

集計:

```powershell
.\.venv\Scripts\python.exe -m queria_master summary
```

収録範囲の確認:

```powershell
.\.venv\Scripts\python.exe -m queria_master sql --query "SELECT * FROM meta.coverage_boundary" --max-rows 20
```

ソース別のテーブル・結合キー・件数:

```powershell
.\.venv\Scripts\python.exe -m queria_master sql --query "SELECT * FROM meta.public_table_catalog" --max-rows 50
.\.venv\Scripts\python.exe -m queria_master sql --query "SELECT * FROM meta.dataset_row_counts" --max-rows 50
```

法人番号で結合された追加情報の一覧:

```powershell
.\.venv\Scripts\python.exe -m queria_master sql --query "SELECT * FROM core.v_company_source_counts WHERE corporate_number='法人番号13桁' ORDER BY source_key" --max-rows 50
```

全量DBを更新した後は、runtimeと検索索引を同じgenerationで公開します。

```powershell
.\.venv\Scripts\python.exe -m queria_master init-enrichment
.\.venv\Scripts\python.exe -m queria_master --db data\queria_master.duckdb publish-runtime `
  --enrichment-db data\queria_enrichment.duckdb `
  --runtime-db data\queria_runtime.duckdb `
  --search-index data\search.sqlite
.\.venv\Scripts\python.exe -m queria_master audit --strict
```

以降の検索・詳細SQL・リスト作成はruntimeを既定で使います。更新時は正規DBと拡張DBを更新してから `publish-runtime` を実行してください。

履歴Hojinjoho活動情報ZIPを正本と分離して監査する任意手順:

```powershell
.\.venv\Scripts\python.exe -m queria_master import-gbiz-archive `
  --archive work\Hojinjoho.zip `
  --staging-db work\hojinjoho-history.duckdb `
  --target-industry G
```

このstagingは既存検索へ自動反映されず、既存ファイルも上書きしません。Basic CSVや全法人母集団の取込ではなく、`G` は大分類だけを意味します。自動テストは合成ZIPで実施済みです。別途復元できた companion 監査記録には379,025,154 bytesなどの値がありますが、元ZIP本体は取得できず、現 importer の完走検証は未実施です。詳細は [`docs/GBIZ_ARCHIVE_IMPORT_JA.md`](docs/GBIZ_ARCHIVE_IMPORT_JA.md) を参照してください。

意味検索を追加する場合は、任意依存を入れてから text-rich 法人だけのベクトル索引を作ります。`--candidate-keyword` を併用すると、FTS候補を先に絞るため大規模データでもメモリ効率よく検索できます。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[semantic]"
.\.venv\Scripts\python.exe -m queria_master build-semantic-index --model <モデル名>
.\.venv\Scripts\python.exe -m queria_master semantic-search "ソフトウェア開発を支援する企業" --candidate-keyword ソフトウェア --limit 50
```

任意 SQL:

```powershell
.\.venv\Scripts\python.exe -m queria_master sql --file sql\examples.sql --max-rows 100
```

Queria の匿名レート制限に当たった場合だけ、次を実行してブラウザ承認します。

```powershell
.\.venv\Scripts\queria.exe login
```
