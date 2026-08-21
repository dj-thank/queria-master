# Queria Master

日本の法人公開データをローカルへ取り込み、DuckDB / SQLiteで高速に検索・分析し、必要に応じて公式情報で証拠付き補完を行うためのプロジェクトです。

国税庁法人番号、gBizINFO、EDINET、厚生労働省、政府電子調達、自治体オープンデータなど、Queriaで公開されるデータを中心に法人番号で統合します。公開ソースに存在しない値は推測で埋めず、補完データもcanonicalな法人マスタへ無根拠に上書きしません。

## 現在の構成

データの役割を4層に分離しています。

| 層 | 既定ファイル | 役割 |
| --- | --- | --- |
| canonical | `data/queria_master.duckdb` | 公開ソースから再構築できる正規データ |
| enrichment | `data/queria_enrichment.duckdb` | 電話・メール・フォーム等の証拠、状態、取得日時 |
| runtime | `data/queria_runtime.duckdb` | 検索・表示向けに物理統合した読み取り用DB |
| search index | `data/search.sqlite` | FTS5 trigramによる高速キーワード検索 |

検索系はruntime、更新系はcanonicalを既定にします。runtimeと検索索引は `generation_id` が整合する場合だけ利用します。

設定と状態確認:

```powershell
.\.venv\Scripts\python.exe -m queria_master configure
.\.venv\Scripts\python.exe -m queria_master app-health
```

portableなデータ配置を固定する場合:

```powershell
.\.venv\Scripts\python.exe -m queria_master configure --home D:\Queria --default-limit 500
```

詳細は [`docs/V090_OPERATIONAL_ARCHITECTURE_JA.md`](docs/V090_OPERATIONAL_ARCHITECTURE_JA.md) と [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) を参照してください。

## 最初に実行する

### Windows

```text
01_初回セットアップ.bat
```

またはPowerShellから:

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

### Linux / macOS

```bash
chmod +x bootstrap.sh refresh.sh
./bootstrap.sh
```

初回セットアップでは、仮想環境の作成、必要パッケージの導入、公開データの取得、Parquetへの抽出、DuckDBの構築、検索用ビューの作成までを行います。

Queriaの認証が必要になった場合だけ、標準CLIのログインを利用します。

```powershell
.\.venv\Scripts\queria.exe login
```

認証情報はQueriaの標準設定領域へ保存し、このリポジトリへ書き込みません。

## すぐ検索する

```powershell
.\.venv\Scripts\python.exe -m queria_master search `
  --keyword AI `
  --prefecture 東京都 `
  --has-web `
  --limit 100
```

検索索引を作成済みなら `--fast` を利用できます。

```powershell
.\.venv\Scripts\python.exe -m queria_master build-search-index
.\.venv\Scripts\python.exe -m queria_master search --keyword ソフトウェア --fast --limit 100
```

主な検索条件:

```text
--keyword              社名・事業概要・URLなど
--prefecture           都道府県
--city                 市区町村
--industry-major       JSIC大分類
--industry-middle      JSIC中分類
--min-employees        最小従業員数
--max-employees        最大従業員数
--min-capital          最小資本金
--max-capital          最大資本金
--has-web              公式Web URLがある法人
--limit                最大件数
--out                  .csv / .json / .jsonl / .parquet
--fast                 検索索引を優先する高速返却
```

性能値はDB世代、キャッシュ、検索語、ビルド方式で変わるため、READMEへ過去の固定値を基準として置きません。測定方法と実測記録は [`docs/SEARCH_PERFORMANCE.md`](docs/SEARCH_PERFORMANCE.md) を参照してください。

## 公開企業情報の補完 — Public Company Enrichment v1.1.0

`company_scout/public_enrichment/` には、任意の企業CSV / XLSXを政府公開データ・EDINET・企業公式Webサイトで補完する独立パイプラインがあります。

入力元の全列を保持し、法人番号候補、公開値、出典、照合品質、要確認理由をローカルSQLiteへ分離して保存します。

### Windowsで最短実行

`company_scout/public_enrichment/` で次の順に実行します。

```text
setup_windows.cmd
prepare_windows.cmd companies.csv
integrate_windows.cmd
status_windows.cmd
```

主な機能:

- CSV / XLSX入力と入力元列の完全保持
- `SOURCE_ID` がない場合のローカルID自動生成
- 高確度の法人番号だけを自動採用
- 高確度候補が競合した場合は採用解除してレビューへ戻す
- 法人番号確定済み企業だけへ基本・財務・職場情報を結合
- EDINET XBRLから平均年齢・平均年間給与を抽出
- 公式サイトの電話番号候補を根拠URL付きで取得
- SSRF対策、同一ホスト制約、robots.txt、受信サイズ・リダイレクト上限
- 年度別財務、最新財務、コアキーワード、業種内ランキングの出力
- 取込元SHA-256、照合状態、要確認理由の監査

詳細は [`company_scout/public_enrichment/README.md`](company_scout/public_enrichment/README.md) と [`company_scout/public_enrichment/SECURITY.md`](company_scout/public_enrichment/SECURITY.md) を参照してください。

## 検証済み公式連絡先

`company_scout/public_enrichment/reference/verified_public_contacts.csv` には、企業自身が管理する公式ページを根拠として確認した連絡先を、次のメタデータとともに保存しています。

- 電話番号
- 電話種別
- 電話用途
- 代表電話フラグ
- 公式サイトURL
- 根拠URL
- 根拠ページ
- 信頼度
- 確認日

用途限定番号を代表電話として扱いません。企業名だけの一致も自動採用しません。

任意のローカル企業DBへ反映する場合:

```bash
cd company_scout/public_enrichment
python import_verified_contacts.py \
  --db output/company_public_data.sqlite3 \
  --contacts reference/verified_public_contacts.csv \
  --replace-source \
  --output output/csv/verified_contacts_reflected.csv
```

照合優先順位は、明示されたローカル `SOURCE_ID`、証券コード＋企業名の一意一致、企業名＋所在地の一意一致です。曖昧なレコードは監査テーブルへ回します。

詳細は [`company_scout/public_enrichment/reference/README.md`](company_scout/public_enrichment/reference/README.md) を参照してください。

## 公開データを更新する

Windows:

```powershell
.\02_データ更新.bat
```

Linux / macOS:

```bash
./refresh.sh
```

更新は一時DBへ構築し、検証を通過した後だけ完成済みDBを置き換えます。

代表的なスコープ:

```powershell
# 最大収録の公開データ
.\.venv\Scripts\python.exe -m queria_master refresh --scope all-public

# 情報通信業を先に確認
.\.venv\Scripts\python.exe -m queria_master refresh --scope info-communications

# gBizINFO基本情報のある法人
.\.venv\Scripts\python.exe -m queria_master refresh --scope gbizinfo-companies

# 国税庁法人番号を母集団にした法人
.\.venv\Scripts\python.exe -m queria_master refresh --scope all-corporations
```

大容量スコープでは、十分なストレージ、通信量、処理時間を確保してください。収録境界はDBの `meta.coverage_boundary` と `meta.public_table_catalog` で確認できます。

## 主なDuckDBデータ

代表的なテーブル・ビュー:

```text
core.companies
core.company_industries
core.v_category_summary
core.v_company_activity
core.v_company_source_records
core.v_company_source_counts
core.v_data_quality

gbizinfo.company_summary
gbizinfo.subsidies
gbizinfo.procurements
gbizinfo.patents
gbizinfo.certifications
gbizinfo.commendations

edinet.companies
edinet.documents
edinet.business_results
edinet.financial_facts
edinet.funds

mhlw.josei_katsuyaku_company
mhlw.kaigo_establishment
mhlw.shougai_establishment
mhlw.ndb_health_checkup

p_portal.procurement_award
metro_tokyo.*
raw.houjin_bangou

meta.source_registry
meta.source_metadata
meta.public_table_catalog
meta.dataset_row_counts
meta.refresh_log
meta.coverage_boundary
```

公開データは元の粒度を保持します。活動、財務、施設明細を無理に法人1行へ横持ちしません。

DuckDBを直接使う例:

```sql
SELECT company_name, prefecture_name, employee_number, company_url
FROM core.v_info_communications
WHERE prefecture_name = '東京都'
  AND employee_number >= 10
ORDER BY employee_number DESC NULLS LAST
LIMIT 100;
```

SQL例は [`sql/examples.sql`](sql/examples.sql) にまとめています。

## 証拠付きenrichment

電話・メール・問い合わせフォームなどの補完値は、canonical DBへ直接上書きせず `data/queria_enrichment.duckdb` へ出典・取得日時・状態・内容ハッシュ付きで保存できます。

```powershell
.\.venv\Scripts\python.exe -m queria_master init-enrichment
.\.venv\Scripts\python.exe -m queria_master seed-enrichment
.\.venv\Scripts\python.exe -m queria_master collect-enrichment `
  --worker-id worker-01 `
  --field email `
  --max-tasks 100
```

抽出値は初期状態でレビュー対象として扱います。抑止・利用可否を確認した値だけを営業用途へ出す設計です。

```powershell
.\.venv\Scripts\python.exe -m queria_master sales-ready `
  --out exports\sales_ready_accounts.csv
```

詳細は [`docs/OUTBOUND_ENRICHMENT_RUNBOOK_JA.md`](docs/OUTBOUND_ENRICHMENT_RUNBOOK_JA.md) を参照してください。

## runtime DBと検索索引

canonical DBとenrichment DBを壊さずに保持しつつ、利用時のJOINを減らすため、読み取り用runtime DBを構築できます。

```powershell
.\.venv\Scripts\python.exe -m queria_master build-runtime `
  --db data\queria_master.duckdb `
  --enrichment-db data\queria_enrichment.duckdb `
  --out data\queria_runtime.duckdb

.\.venv\Scripts\python.exe -m queria_master --db data\queria_runtime.duckdb build-search-index `
  --out data\search.sqlite
```

状態確認:

```powershell
.\.venv\Scripts\python.exe -m queria_master runtime-summary `
  --runtime-db data\queria_runtime.duckdb
```

出荷前・更新後の監査:

```powershell
.\.venv\Scripts\python.exe -m queria_master audit `
  --db data\queria_master.duckdb `
  --enrichment-db data\queria_enrichment.duckdb `
  --runtime-db data\queria_runtime.duckdb `
  --out ..\outputs\QUALITY_AUDIT.json `
  --strict
```

## EXE / Desktop

CLI版・Desktop版はDBを実行ファイルへ埋め込まず、外部データとして隣接配置する設計です。データ更新だけで再利用でき、巨大DBの変更ごとにEXEを再ビルドする必要がありません。

Desktop版:

```powershell
.\.venv\Scripts\python.exe scripts\build_exe.py --mode desktop --bundle onedir
.\dist\queria-master-desktop\queria-master-desktop.exe
```

CLI常駐版:

```powershell
.\.venv\Scripts\python.exe scripts\build_exe.py --mode cli --bundle onedir --out dist\cli-onedir
.\dist\cli-onedir\queria-master\queria-master.exe `
  --db data\queria_runtime.duckdb `
  daemon --search-index data\search.sqlite
```

CompanyMaster Windowsアプリとの統合仕様は [`docs/COMPANYMASTER_INTEGRATION_JA.md`](docs/COMPANYMASTER_INTEGRATION_JA.md) を参照してください。

## 大容量配布

大容量の公開データ版やアプリ一式は、通常のソースリポジトリとは分けて生成できます。

```powershell
.\.venv\Scripts\python.exe scripts\build_full_release.py `
  --out ..\outputs\queria-master-all-public.zip

.\.venv\Scripts\python.exe scripts\build_full_app_bundle.py `
  --out F:\QueriaReleases\queria-master-full-app.zip
```

GitHubで大容量ファイルを分割配布する手順は [`docs/GITHUB_DISTRIBUTION_JA.md`](docs/GITHUB_DISTRIBUTION_JA.md) を参照してください。

## データ上の限界

このプロジェクトは「公開ソースに存在する法人・明細」を法人番号を共通キーとして統合します。

- 国税庁法人番号には業種情報がありません。
- gBizINFOの事業種目や財務・職場情報は全法人で必ず埋まるわけではありません。
- EDINETの対象・期間は公開されている提出書類に依存します。
- 電話番号やメールなどは、公式根拠を確認できない場合は空欄またはレビュー対象のままです。
- 公開ソースの件数と、民間データベースや独自推定の件数が一致する保証はありません。

「全公開データ」は、このプロジェクトが選定している公開テーブルの収録範囲を意味し、インターネット上の全情報を意味しません。

## 安全性と再現性

- 公開DuckLakeはQueria CLIのread-only経路で読み取ります。
- リモートSQLは読み取りクエリに限定します。
- SQL・集計・検索結果は手元のDuckDB / SQLiteで処理します。
- APIキー、アクセストークン、Cookieをリポジトリ・ZIP・DB・ログへ保存しません。
- 入力企業リストや生成DB、取得キャッシュをGitへコミットしません。
- 特定の入力元に固有のID、URL、APIパス、件数フィンガープリントを公開企業補完コードへ埋め込みません。
- 公式サイト取得にはSSRF対策、同一ホスト制約、robots.txt、レスポンス上限を適用します。
- EDINET ZIPには受信サイズ、展開サイズ、ファイル数の上限を適用します。
- 競合する高確度の法人番号は自動採用せず、候補履歴を残してレビューへ戻します。

## テスト / CI

リポジトリ全体:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\verify_package.py
```

Public Company Enrichment:

```bash
cd company_scout/public_enrichment
python -m unittest discover -s tests -v
```

Public Company EnrichmentはGitHub Actionsで Python 3.11 / 3.12 / 3.13 の構文チェックとオフラインテストを実行します。

## ドキュメント

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 全体設計
- [`docs/V090_OPERATIONAL_ARCHITECTURE_JA.md`](docs/V090_OPERATIONAL_ARCHITECTURE_JA.md) — canonical / runtime / index運用
- [`docs/COMPANYMASTER_INTEGRATION_JA.md`](docs/COMPANYMASTER_INTEGRATION_JA.md) — Windowsアプリ統合
- [`docs/OUTBOUND_ENRICHMENT_RUNBOOK_JA.md`](docs/OUTBOUND_ENRICHMENT_RUNBOOK_JA.md) — 証拠付きenrichment運用
- [`docs/SEARCH_PERFORMANCE.md`](docs/SEARCH_PERFORMANCE.md) — 検索性能の測定
- [`docs/GITHUB_DISTRIBUTION_JA.md`](docs/GITHUB_DISTRIBUTION_JA.md) — 大容量配布
- [`company_scout/public_enrichment/README.md`](company_scout/public_enrichment/README.md) — 公開企業情報補完
- [`company_scout/public_enrichment/SECURITY.md`](company_scout/public_enrichment/SECURITY.md) — 補完処理のセキュリティ境界
- [`company_scout/public_enrichment/reference/README.md`](company_scout/public_enrichment/reference/README.md) — 検証済み公式連絡先

## 出典

主な出典は、gBizINFO、国税庁法人番号公表サイト、EDINET、厚生労働省公開データ、政府電子調達、自治体オープンデータ、日本標準産業分類です。

Queria側のテーブル、詳細URL、ライセンス、収録境界は `reference/sources.json` とDBの `meta.source_registry` / `meta.coverage_boundary` に保持します。

## ライセンス

このリポジトリのコードはMIT Licenseです。取得・生成したデータには各提供元の利用条件・ライセンスが適用されます。
