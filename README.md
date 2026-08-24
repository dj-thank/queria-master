# Queria Master — 全法人・Queria公開データを DuckDB へ

## 0.10: 大分類G情報DB

大分類G（情報通信業、中分類37〜41）専用版は、FUMA行と全国法人マスタに加え、v0.9.0完全版へ収録済みの厚生労働省公開事業所連絡先を法人番号で結合します。事業所電話は`phone_type=establishment`として本社代表電話から分離し、事業所HPは企業公式HPへ自動昇格せず`enrichment.website_candidates`へ根拠付きで保持します。

ビルド、GitHub Releaseへの配置、コスト制御は次を参照してください。

- [`docs/ADR_G_INFORMATION_DATABASE_JA.md`](docs/ADR_G_INFORMATION_DATABASE_JA.md)
- [`docs/GITHUB_DISTRIBUTION_JA.md`](docs/GITHUB_DISTRIBUTION_JA.md)
- [`docs/GITHUB_BRANCH_AUDIT_20260824.md`](docs/GITHUB_BRANCH_AUDIT_20260824.md)

## 0.9: 設定と実装の生存性

0.9ではcanonical DB、enrichment DB、runtime DB、検索索引を別の役割として解決します。検索系はruntime、更新系はcanonicalを既定にし、runtime/indexは`generation_id`が一致する場合だけ開きます。

```powershell
# 保存設定を表示
.\.venv\Scripts\python.exe -m queria_master configure

# portableホームを保存
.\.venv\Scripts\python.exe -m queria_master configure --home D:\Queria --default-limit 500

# 現在のDB・索引・機能可否
.\.venv\Scripts\python.exe -m queria_master app-health

# 同梱済み公開データから法人番号付き事業所連絡先を別スコープで同期
.\.venv\Scripts\python.exe -m queria_master sync-embedded-public
```

Desktop版には［設定・診断］を追加しました。DB/index不整合時も設定画面を開け、検証後に保存・即時反映できます。詳細は `docs/V090_OPERATIONAL_ARCHITECTURE_JA.md` を参照してください。

Queria が公開する **国税庁法人番号・gBizINFO・EDINET・厚生労働省・政府電子調達・東京都ODS** の
24テーブルを法人番号で結合し、全法人の統合マスタと、補助金・調達・特許・財務ファクト・提出書類・
介護/障害福祉事業所などの明細をローカル DuckDB に取り込む実動プロジェクトです。法人番号のない
ファンドや地域統計もrawテーブルとして保持し、結合できるものだけを法人ビューへ出します。情報源ごとの
収録境界は `meta.coverage_boundary` と `meta.public_table_catalog` に明示します。

ブラウザスクレイピングや FUMA の非公開 API 解析は行いません。Queria 公式 CLI で公開 DuckLake を安全に読み、
結果を Parquet に書き出した後、DuckDB 純正エンジンで `data/queria_master.duckdb` を構築します。

> 配布 ZIP には大容量の固定データスナップショットを同梱せず、初回セットアップ時に Queria の公開最新版を自動投入します。展開しただけでは `data/queria_master.duckdb` はまだ存在しません。

## 最初に実行するもの

CompanyMaster Windowsアプリの統合ソースと、既存の全量 Queria runtime DB への接続仕様は [`docs/COMPANYMASTER_INTEGRATION_JA.md`](docs/COMPANYMASTER_INTEGRATION_JA.md) を参照してください。

### Windows

ZIP を展開し、`01_初回セットアップ.bat` をダブルクリックします。

PowerShell から実行する場合:

```powershell
powershell -ExecutionPolicy Bypass -File .\bootstrap.ps1
```

### Linux / macOS

```bash
chmod +x bootstrap.sh refresh.sh
./bootstrap.sh
```

初回処理は次を自動実行します。

1. `.venv` を作成
2. Queria CLI と DuckDB をインストール
3. Queria 上の全法人と、現行スナップショット対象24テーブルを読み取り
4. 全法人・活動・財務・施設明細を Parquet に抽出
5. `data/queria_master.duckdb` を原子的に構築
6. 法人番号・所在地・業種コード・活動明細用のローカルテーブルと検索ビューを作成

`GBIZINFO_API_TOKEN` は不要です。Queria は匿名でも読めますが、レート制限に達した場合は次を一度実行してください。

```powershell
.\.venv\Scripts\queria.exe login
```

トークンは Queria の標準設定領域へ保存され、このプロジェクト内には保存しません。

標準ではDBを展開先の `data/` に保存します。パッケージとして導入する場合など、保存先を固定したいときは `QUERIA_MASTER_HOME` を設定できます。

## すぐ検索する

```powershell
.\.venv\Scripts\python.exe -m queria_master search --keyword AI --prefecture 東京都 --has-web --limit 100
```

全量データ付きZIPには `data/search.sqlite`（5,823,039法人分のFTS5 trigram索引）が含まれます。通常のソースZIPや更新後は、次の1回を実行すると同じ高速検索が使えます。

```powershell
.\.venv\Scripts\python.exe -m queria_master build-search-index
.\.venv\Scripts\python.exe -m queria_master search --keyword ソフトウェア --fast --limit 100
```

統合ランタイムDBを作り直した場合は、表示用の電話・メール・問い合わせフォームも検索索引へ含めるため、次のようにランタイムを入力にして索引を原子的に再構築します。

```powershell
.\.venv\Scripts\python.exe -m queria_master --db data\queria_runtime.duckdb build-search-index `
  --out data\search.sqlite
```

`--fast` は結果の安定ソートを省略し、FTS・カテゴリ・都道府県の索引から早く返します。従業員数・資本金順などの安定ソートが必要な場合は `--fast` を付けません。全量更新スクリプトは、DB更新後に検索索引も自動再構築します。

`--fast` で3文字未満または特殊文字を含む検索語を使った場合は、5.8百万行の全列部分一致を避けるため法人名の前方一致へ切り替えます。短い語の完全な部分一致（所在地・事業概要・連絡先を含む）が必要な場合は `--fast` を外してください。

## 常駐EXE・1000件高速表示

CLIを毎回起動する方式は、検索自体が速くてもPyInstaller起動と原本DB検証が加算されます。速度を優先する場合は、検索索引を一度だけ開く常駐モードを使います。

```powershell
.\dist\queria-master.exe --db data\queria_runtime.duckdb daemon `
  --search-index data\search.sqlite
```

常駐プロセスはJSONLで検索要求を受け付けます。1,000件を辞書の配列ではなく列名1回＋値配列として返すため、IPCの転送量を抑えています。検索要求例:

```json
{"op":"search","keyword":"ソフトウェア","limit":1000}
```

画面付きの速度優先版は標準Tkの常駐検索EXEとしてビルドできます。検索はworker thread、表示はGUI threadへ分離し、100件ずつ反映します。

```powershell
.\.venv\Scripts\python.exe scripts\build_exe.py --mode desktop --bundle onedir
.\dist\queria-master-desktop\queria-master-desktop.exe
```

速度優先のCLI常駐版をonedirで作る場合は、次を実行します。

```powershell
.\.venv\Scripts\python.exe scripts\build_exe.py --mode cli --bundle onedir --out dist\cli-onedir
.\dist\cli-onedir\queria-master\queria-master.exe --db data\queria_runtime.duckdb daemon --search-index data\search.sqlite
```

1000件の検索取得・常駐IPCを再現計測するには次を実行します。

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_exe_resident.py `
  --exe dist\queria-master.exe `
  --db data\queria_runtime.duckdb `
  --search-index data\search.sqlite
```

onefile版は配布しやすい互換版、onedir版は起動速度を優先する基準版です。全量5,823,039法人で常駐検索の1,000件取得は、0.8.0基準onedir CLIビルドの実測p50 13.023msでした。今回の修正版EXEはこのホストのアプリケーション制御ポリシー（WinError 4551）で直接起動できないため、最新ビルドのEXE合格とは混同せず、現行Python常駐経路はp50 14.398msとして別記録しています。0.5秒という数値は、検索・EXE起動・IPC・1000行のGUI反映・初回描画・CSV出力を分けて測定し、実測値として報告します。DBと索引の配置を自動検出できない場合は `QUERIA_MASTER_HOME` にデータの親ディレクトリを指定してください。

## 速度優先の大容量全量データ版

全量スナップショットを同梱する配布物では、原本 `data/queria_master.duckdb`、証拠付き拡張層
`data/queria_enrichment.duckdb`、1法人1行へ集約した検索用 `data/queria_runtime.duckdb`、
FTS5 trigram索引 `data/search.sqlite` を同梱できます。検索・一覧表示は通常ランタイムDBとSQLite索引を
使うため、毎回複数DBを結合する必要がありません。

```powershell
.\.venv\Scripts\python.exe scripts\build_full_release.py `
  --out ..\outputs\queria-master-all-public.zip
```

ParquetキャッシュはランタイムDBへ統合済みなので既定では重複収録しません。元Parquetも必要な場合だけ

EXEと全量DBを一つのアプリZIPへ同梱する場合は、空き容量の大きいドライブへ直接出力します。runtime DB、検索索引、元法人マスタ、拡張DB、GUI windowed版、console fallback、CLI onedir版を収録します。

```powershell
.\.venv\Scripts\python.exe scripts\build_full_app_bundle.py `
  --out F:\QueriaReleases\queria-master-0.8.0-full-app.zip
```

この完全版は約68GBになるため、Cドライブの `outputs` へコピーせず、出力先ドライブの空き容量を確認してください。

GitHubで公開配布する場合は、完全版ZIPを1,800MiB単位へ分割します。分割・結合時に全体SHA-256を検証するスクリプトは `scripts/split_full_app_bundle.py` と `scripts/join_full_app_bundle.py` です。詳細は `docs/GITHUB_DISTRIBUTION_JA.md` を参照してください。
`--include-parquet` を追加してください。この版は数十GBになるため、配布先の空き容量を先に確認してください。

ランタイムDBを再構築する場合は次を実行します。

```powershell
.\.venv\Scripts\python.exe -m queria_master --db data\queria_master.duckdb build-runtime
.\.venv\Scripts\python.exe -m queria_master --db data\queria_runtime.duckdb build-search-index `
  --out data\search.sqlite
```

`data/queria_runtime.duckdb` は高速な読み取り用に最適化した外部データファイルであり、EXEへ埋め込まず
隣接配置します。これにより、巨大DBを再配布せずにデータだけ更新できます。

任意の埋め込みモデルによる意味検索は、説明文などのある法人だけを `float16` のメモリマップ索引へ格納し、先にFTSで候補を絞る二段構成です。5.8百万件すべてへ密ベクトルを無条件に作る方式は、384次元でもfloat32約8.9GBとなり、検索時メモリと更新時間を不必要に増やすため既定にしていません。

```powershell
pip install -e ".[semantic]"
.\.venv\Scripts\python.exe -m queria_master build-semantic-index --model <モデル名>
.\.venv\Scripts\python.exe -m queria_master semantic-search "クラウド基盤を提供する企業" --candidate-keyword クラウド --limit 50
```

```powershell
.\.venv\Scripts\python.exe -m queria_master search --industry-middle 39 --min-employees 10 --out exports\tokyo_it.parquet
```

主な検索条件:

```text
--keyword              社名・事業概要・URLの部分一致
--prefecture           都道府県名
--city                 市区町村名
--industry-middle      37 / 38 / 39 / 40 / 41（複数指定可）
--industry-major       JSIC大分類 A〜T（複数指定可）
--industry-middle      JSIC中分類 2桁（複数指定可。37〜41以外も可）
--min-employees        最小従業員数
--max-employees        最大従業員数
--min-capital          最小資本金（円）
--max-capital          最大資本金（円）
--has-web              WebサイトURLがある法人だけ
--limit                最大件数
--out                  .csv / .json / .jsonl / .parquet
--fast                 FTS・カテゴリ索引を優先する高速返却
```

## データを更新する

Windows:

```powershell
.\02_データ更新.bat
```

Linux / macOS:

```bash
./refresh.sh
```

更新中に既存 DB は触らず、一時 DB が完成して検証を通過した後だけ置き換えます。

既定の更新対象は `all-public` です。2026-08-19の実データではParquet約6.64GB、DuckDB約28.5GB、
EDINET財務ファクトだけで約3,906万行あります。初回は空き容量・通信量・処理時間に十分な余裕を持たせてください。
情報通信業だけを先に確認したい場合は、更新時に `--scope info-communications` を指定します。

## 収録スコープ

既定は `all-public` です。

```powershell
# 全法人＋24公開テーブル（最大収録・超大容量）
.\.venv\Scripts\python.exe -m queria_master refresh --scope all-public

# 情報通信業 G（小容量の動作確認向け）
.\.venv\Scripts\python.exe -m queria_master refresh --scope info-communications

# gBizINFO に基本情報がある全法人（大容量）
.\.venv\Scripts\python.exe -m queria_master refresh --scope gbizinfo-companies

# 国税庁の現存法人を母集団にした全法人（約500万件規模・非常に大容量）
.\.venv\Scripts\python.exe -m queria_master refresh --scope all-corporations
```

大容量スコープでは数 GB 以上の空き容量と通信量を見込んでください。

## DuckDB の主要テーブル

```text
core.companies                 NTA と gBizINFO の和集合（法人 1 行の統合マスタ）
core.company_industries        法人 × 日本標準産業分類コード
core.v_category_summary        大分類・中分類・小分類の法人件数集計
gbizinfo.company_summary       gBizINFO 法人サマリーの全列
gbizinfo.subsidies             補助金明細（1 行 1 案件）
gbizinfo.procurements          調達明細（1 行 1 案件）
gbizinfo.patents               特許・意匠・商標明細
gbizinfo.certifications        届出・認定明細
gbizinfo.commendations         表彰明細
edinet.business_results        EDINET主要財務指標
edinet.companies               EDINET提出会社マスター
edinet.documents               EDINET提出書類メタデータ
edinet.financial_facts         EDINET財務ファクト（約3,906万行）
edinet.funds                   EDINETファンドマスター
mhlw.josei_katsuyaku_company   女性活躍・職場情報
mhlw.kaigo_establishment       介護サービス事業所
mhlw.shougai_establishment     障害福祉サービス事業所
mhlw.ndb_health_checkup        NDB特定健診の地域統計
p_portal.procurement_award     政府電子調達の落札実績
metro_tokyo.*                  東京都ODSの選定テーブル7本
raw.houjin_bangou              国税庁法人番号の全列
core.v_info_communications_strict     公式 business_items に基づく厳密分類
core.v_info_communications_candidates  業種コードがないキーワード候補
core.v_company_activity        法人別の活動明細件数
core.v_company_source_records  全結合可能ソースの法人番号明細（source_key付き）
core.v_company_source_counts   ソース別の法人番号・明細件数・結合可否
core.v_data_quality             欠損率・出典別収録率の集計
meta.jsic_info_communications  37〜41 のコードマスタ
meta.source_registry           出典・ライセンス・テーブル名
meta.source_metadata           Queria が返したメタデータ原文
meta.public_table_catalog      24テーブルの出典・結合キー・ローカル名
meta.dataset_row_counts        24公開 Parquet の件数・サイズ・SHA-256
meta.refresh_log               更新履歴・行数・Parquet SHA-256
meta.coverage_boundary         収録範囲・要約のみ・対象外の境界
```

全公開スコープでは活動・財務・施設明細を法人サマリーへ横持ちせず、元の粒度を保持します。`core.company_industries` は
`business_items` の全大分類・中分類・小分類を階層行へ正規化し、`core.v_category_summary` で高速に集計できます。

```sql
SELECT corporate_number, title, amount, issuer
FROM gbizinfo.subsidies
WHERE corporate_number = '法人番号13桁'
ORDER BY certification_date DESC NULLS LAST;
```

DuckDB を直接使う例:

```sql
SELECT company_name, prefecture_name, employee_number, company_url
FROM core.v_info_communications
WHERE prefecture_name = '東京都'
  AND employee_number >= 10
ORDER BY employee_number DESC NULLS LAST
LIMIT 100;
```

サンプル SQL は `sql/examples.sql` にまとめています。

## 重要なデータ上の限界

### 大分類Gの情報DB

日本標準産業分類の大分類`G`は、情報通信業の中分類37〜41とその全小分類・細分類を含みます。G専用ビルドはFUMA行を母集団とし、法人番号が空の行を全国法人マスタへ照合します。法人格を保持した正規化社名と正規化本店住所が双方で一意に完全一致した場合だけ、法人番号を回復します。

```powershell
.\.venv\Scripts\python.exe scripts\build_g37_41_fuma.py
```

ファイル名の`G37-41`は既存配布との互換名です。対象範囲の正本は常に大分類`G`です。法人番号の根拠は`corporate_number_match_method`、HP・電話の根拠は出典URLと取得日時を含む拡張レイヤーで追跡します。全社へLLMを実行せず、公的データ照合→既知公式HPの通常抽出→低信頼候補のみLLM確認、の順で費用を抑えます。

この DB は「公開ソースに存在する法人・明細」を、法人番号を共通キーに統合したものです。
国税庁法人番号には業種がなく、gBizINFO の事業種目も全法人で必ず埋まるわけではありません。
したがって、FUMA の独自収集・推定による件数と完全一致することは保証できません。

「全法人」は現在のQueria公開スナップショットにある国税庁法人番号とgBizINFO法人サマリーの和集合です。
「全公開データ」はこの版で選定した24テーブルのスナップショットであり、インターネット上の全情報、
日本中のモデル・個人プロフィール、全自治体の全カタログを意味しません。gBizINFOの公式ダウンロードには
財務・職場の生明細もありますが、Queriaが公開していない範囲は取得していません。
EDINET財務ファクトはQueriaが公開する現行・段階充填分で、訂正報告書や全期間を保証しません。
財務の `latest_*`、職場の `avg_age` / `avg_monthly_overtime` / `female_ratio` など、法人サマリーに実際に存在する指標は保持します。
この境界は `SELECT * FROM meta.coverage_boundary` で確認できます。

FUMA/FDS の正規 CSV を取得した場合は法人番号をキーに追加結合できますが、このプロジェクトは FUMA の画面や非公開 API を取得しません。

また、Queria/gBizINFO の公開項目には電話番号がありません。電話番号を必要とする場合は、利用条件を満たす別の正規データソースが必要です。

## 業種判定

gBizINFO の現行 `business_items` は、`G:情報通信業-40:インターネット附随サービス業-401:` のようなラベル付き文字列です。抽出時にこの形式を判定し、`jsic_codes_raw` へ検索用の正規化コードを保存します。元の文字列は `business_items_raw` に残します。

```text
G
G / G37 / G38 / G39 / G40 / G41
```

複数業種を持つ法人は `core.company_industries` に複数行として正規化されます。中分類が明示されない `G:情報通信業` は `G` として保持し、厳密ビューには含めます。
検索CLIは `--industry-major E --industry-middle 09` のように全業種で使えます。

`core.v_info_communications_strict` は公式の `business_items` に `G:` がある法人だけです。
事業概要や社名から推測した法人は `core.v_info_communications_candidates` に分離し、厳密ビューへ混ぜません。

## 営業向け証拠付き拡張層

法人マスタへ電話・メール・ホームページを直接上書きせず、`data/queria_enrichment.duckdb` へ出典・取得日時・内容ハッシュ付きで保存できます。初回作成と全法人タスクのシード:

```powershell
.\.venv\Scripts\python.exe -m queria_master init-enrichment
.\.venv\Scripts\python.exe -m queria_master seed-enrichment
```

公式ページをrobots.txtと取得上限に従って段階収集し、問い合わせフォームや明記された電話・メールを取り込む例:

```powershell
.\.venv\Scripts\python.exe -m queria_master collect-enrichment --worker-id worker-01 --field email --max-tasks 100
.\.venv\Scripts\python.exe -m queria_master sales-ready --out exports\sales_ready_accounts.csv
```

抽出値は初期状態で `review` です。抑止・利用可否を確認した値だけを営業リストへ出す設計、状態の意味、再開手順は `docs/OUTBOUND_ENRICHMENT_RUNBOOK_JA.md` を参照してください。

## 速度優先の統合ランタイムDB

更新用の正規DBと証拠付き拡張DBを壊さずに保持しつつ、利用時のJOINをなくすため、次のコマンドで一つの読み取り用DuckDBへ物理統合できます。

```powershell
.\.venv\Scripts\python.exe -m queria_master build-runtime `
  --db data\queria_master.duckdb `
  --enrichment-db data\queria_enrichment.duckdb `
  --out data\queria_runtime.duckdb

.\.venv\Scripts\python.exe -m queria_master runtime-summary `
  --runtime-db data\queria_runtime.duckdb
```

`data/queria_runtime.duckdb` には、全法人の正規マスタ、公開データの明細、拡張層の証拠・状態・抑止情報、営業向けビュー、1法人1行の `search.company_documents` を同梱します。ビルドは `.building` へ行い、検査後に原子的に差し替えるため、検索中の完成済みDBは壊しません。高速キーワード検索は `data/search.sqlite` のSQLite FTS5 trigram索引を使い、集計・詳細SQL・営業リストは統合ランタイムDBを読みます。

統合DBを使う検索例:

```powershell
.\.venv\Scripts\python.exe -m queria_master --db data\queria_runtime.duckdb search `
  --keyword SaaS --prefecture 東京都 --fast --limit 100
```

更新時は `data/queria_master.duckdb` と `data/queria_enrichment.duckdb` を更新し、最後に `build-runtime` を再実行します。これにより、証拠を残す更新経路と、毎回一つのDBだけを読む高速な利用経路を分離できます。

出荷前・更新後の監査:

```powershell
.\.venv\Scripts\python.exe -m queria_master audit `
  --db data\queria_master.duckdb `
  --enrichment-db data\queria_enrichment.duckdb `
  --runtime-db data\queria_runtime.duckdb `
  --out ..\outputs\QUALITY_AUDIT.json `
  --strict
```

監査では法人番号重複、主要項目の収録件数、検索索引のrefresh_id・法人件数、実測キーワード検索時間、拡張DB件数、統合DBとの法人件数一致を確認します。0.1秒という目標は環境・キャッシュ・検索語で変動するため、監査JSONの実測値を基準にします。

### Windows EXE

新しいデスクトップUIの正本は `company_scout/` のReact + Tauri/Rust版です。`queria_master/desktop_app.py` からビルドするTkinter版は既存配布向けのレガシー・メンテナンスUIとして残し、新しい操作設計と機能追加はTauri版を優先します。UIと検索の契約は [`docs/UI_SEARCH_WORKBENCH_2026-08-21.md`](docs/UI_SEARCH_WORKBENCH_2026-08-21.md) を参照してください。

外部DBをEXEへ埋め込まず、実行ファイルは軽量な処理本体、DB・検索索引は同じフォルダの `data` に置く構成です。これにより5.8百万法人の再配布時にEXEを作り直す必要がありません。

レガシーDesktop版:

```powershell
.\scripts\build_exe.ps1
.\dist\queria-master.exe --db data\queria_runtime.duckdb runtime-summary `
  --runtime-db data\queria_runtime.duckdb
.\dist\queria-master.exe --db data\queria_runtime.duckdb search `
  --keyword ソフトウェア --fast --limit 100
```

EXEの再ビルド後は `dist\queria-master.exe.json` にサイズ、SHA-256、使用Python、外部データ方針を記録します。

## 安全性と再現性

- Queria 公開 DuckLake を素の DuckDB で直接 `ATTACH` しません。
- Queria CLI が互換バージョンで read-only 接続し、Parquet へ抽出します。
- SQL・結果は手元の DuckDB で処理されます。
- 認証情報を ZIP、SQL、ログ、DB に書き込みません。
- 既定で Queria の匿名テレメトリを環境変数により無効化します。
- リモート SQL は `SELECT / WITH` のみです。
- `scripts/verify_package.py` と標準ライブラリのテストを同梱しています。

検証:

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
- [`docs/UI_SEARCH_WORKBENCH_2026-08-21.md`](docs/UI_SEARCH_WORKBENCH_2026-08-21.md) — React/Tauri検索UIと索引フォールバックの契約
- [`docs/OUTBOUND_ENRICHMENT_RUNBOOK_JA.md`](docs/OUTBOUND_ENRICHMENT_RUNBOOK_JA.md) — 証拠付きenrichment運用
- [`docs/SEARCH_PERFORMANCE.md`](docs/SEARCH_PERFORMANCE.md) — 検索性能の測定
- [`docs/GITHUB_DISTRIBUTION_JA.md`](docs/GITHUB_DISTRIBUTION_JA.md) — 大容量配布
- [`docs/ADR_G_INFORMATION_DATABASE_JA.md`](docs/ADR_G_INFORMATION_DATABASE_JA.md) — 情報通信業版DBの設計判断
- [`docs/GITHUB_BRANCH_AUDIT_20260824.md`](docs/GITHUB_BRANCH_AUDIT_20260824.md) — GitHubブランチ全量監査
- [`docs/RELEASE_G_V0100_JA.md`](docs/RELEASE_G_V0100_JA.md) — 情報通信業版 v0.10.0 リリースノート
- [`company_scout/public_enrichment/README.md`](company_scout/public_enrichment/README.md) — 公開企業情報補完
- [`company_scout/public_enrichment/SECURITY.md`](company_scout/public_enrichment/SECURITY.md) — 補完処理のセキュリティ境界
- [`company_scout/public_enrichment/reference/README.md`](company_scout/public_enrichment/reference/README.md) — 検証済み公式連絡先

## 出典

- Queria `gbizinfo.main.mart_gbizinfo_company`
- Queria `gbizinfo.main.mart_gbizinfo_subsidy`
- Queria `gbizinfo.main.mart_gbizinfo_procurement`
- Queria `gbizinfo.main.mart_gbizinfo_patent`
- Queria `gbizinfo.main.mart_gbizinfo_certification`
- Queria `gbizinfo.main.mart_gbizinfo_commendation`
- Queria `houjin_bangou.main.mart_houjin_bangou`
- Queria `edinet.main.mart_companies` / `mart_documents` / `mart_business_results` / `stg_financial_facts` / `mart_funds`
- Queria `mhlw.josei_katsuyaku.company` / `mhlw.kaigo.establishment` / `mhlw.shougai.establishment` / `mhlw.ndb.health_checkup`
- Queria `p_portal.main.procurement_award`
- Queria `metro_tokyo.ods` の選定テーブル
- gBizINFO（経済産業省）
- 国税庁法人番号公表サイト（国税庁）
- EDINET（金融庁）
- 厚生労働省の公開データ
- 政府電子調達システム（調達ポータル）
- デジタル庁・自治体標準オープンデータセット
- 日本標準産業分類（総務省）

詳細 URL とライセンス表記は `reference/sources.json` と DB の `meta.source_registry` に収録しています。

設計詳細は `docs/ARCHITECTURE.md`、FUMA URL の正規化メモは `docs/FUMA_URL_JSON.md` にあります。

## ライセンス

このリポジトリのコードは MIT License です。取得したデータには各提供元の利用条件・ライセンスが適用されます。
