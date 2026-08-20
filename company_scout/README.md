# CompanyMaster

社内向けの高速企業探索 Windows アプリです。全国法人マスターをローカル DuckDB に保持し、自然文または詳細条件で検索し、必要な企業だけ GPT-5.6 Luna + Codex App Server で公開 Web を深掘りし、そのまま CSV / Salesforce に出力します。

## MVPでできること

- 全国法人を業種限定なしで検索
- 都道府県、市区町村、従業員数、資本金、設立年、Webサイト有無、キーワードで絞り込み
- 日本標準産業分類（大分類 / 中分類 / 小分類 / 細分類）のコード前方一致検索
- 「東京のSaaSで従業員30〜300名」のような自然文を GPT-5.6 Luna が `SearchPlan` に変換
- 検索結果は 100 行ずつ表示し、数万〜数百万件をブラウザ側へ一括描画しない
- 検索条件をローカルメモリーに保存し、次回起動後に再利用
- 企業別の公開 Web 深掘り、根拠 URL、AI 推定業種、調査メモ、根拠トランスクリプトを保存
- 検索結果全体を名前付きリストにし、CSV 出力
- 電話番号は `search.company_documents.phone`（公式サイト等の証拠付き拡張層）を法人番号で結合して表示・CSV/Salesforceへ出力
- Salesforce Account へ法人番号を外部 ID とした Bulk API 2.0 Upsert
- OpenAI API キーを共有せず、利用者ごとに自分の ChatGPT アカウントで Codex にログイン
- LLM は `gpt-5.6-luna` にハードロック。モデル選択 UI / 自動フォールバックなし

> 「トランスクリプト」は公開情報を何を確認したかという根拠ログです。モデルの非公開の推論過程は保存・表示しません。

## アーキテクチャ

```text
CompanyMaster.exe (Tauri + React)
        |
        +-- Embedded DuckDB 1.5.5 -------------------+
        |   companies / industry_taxonomy            |
        |   saved_searches / company_lists            |
        |   research_reports                           |
        |   +-- DuckLake READ_ONLY direct ATTACH -----+
        |       houjin_bangou + gbizinfo              |
        |
        +-- Codex App Server (pinned child process)
        |      +-- per-user ChatGPT login
        |      +-- GPT-5.6 Luna only
        |      +-- live web research
        |
        +-- Salesforce OAuth PKCE --> Bulk API 2.0
```

LLM は DB に生 SQL を直接流しません。自然文は構造化 `SearchPlan` に変換し、Rust 側で検証して検索 SQL にコンパイルします。Web 調査も構造化 `ResearchReport` に変換し、アプリ側が保存します。

## Queria-master全量DBとの統合

既存の `queria-master` 全量版を同じPCに置いている場合、CompanyMasterは
`QUERIA_RUNTIME_DB`、`QUERIA_MASTER_HOME`、実行フォルダ周辺、カレントフォルダの順に
`data/queria_runtime.duckdb` を探します。見つかったランタイムDBはDuckDBの
`ATTACH ... (READ_ONLY)` で接続し、CompanyMasterの検索対象をビューとして公開します。
26GB超のDBをCompanyMaster側へコピーせず、検索・CSV・企業リストをそのまま利用できます。

```powershell
$env:QUERIA_MASTER_HOME = 'C:\Queria\release-root'
CompanyMaster.exe
```

検索・一覧は外部ランタイムDBを読み取り専用で使い、検索メモリー、企業リスト、調査レポートは
CompanyMaster専用のローカルDuckDBへ保存します。外部DB更新後はCompanyMasterを再起動してください。

電話番号は国税庁法人番号・gBizINFOの基礎マスター項目ではないため、runtime内の
`search.company_documents` に証拠付き拡張データがある場合だけ表示します。現在の同梱runtimeでは
電話番号件数が0件なので、電話を埋めるには既存の公式サイト調査・enrichment処理を実行してruntimeを
更新してください。電話がない会社を推測で補完することはありません。

## データの考え方

### 1. 最大母集団

組み込み DuckDB が Queria の国税庁法人番号 DuckLake を `READ_ONLY` で直接 ATTACH し、企業マスターの母集団にします。Queria CLI や Python の中継プロセスは使いません。会社名、法人番号、所在地などの基礎情報を保持します。

### 2. 企業属性

gBizINFO を法人番号で LEFT JOIN し、存在する場合だけ従業員数、資本金、設立、会社 URL、代表者、事業概要、補助金・調達・財務系属性を付与します。

### 3. 細かい業種

正式な業種分類は e-Stat の「日本標準産業分類（2023年7月改定）」を別マスターにします。

- 大分類: `G`
- 中分類: `39`
- 小分類: `391`
- 細分類: `3911`

のような 4 階層を保持します。gBizINFO の `business_items` と正式 JSIC は同一扱いにしません。Web から Luna が推定した業種も `inferred_*` として正式値とは分離します。

**重要:** DuckDB で全国法人を同期しても全法人に4桁の正式 JSIC が付くわけではありません。`industry_code` が入っている外部データを追加インポートした会社、または根拠付きで Luna が推定した会社は細分類コードで直接絞れます。コードがない大量法人については、gBizINFO の業種/事業項目・事業概要に対する `industry_terms` / キーワード検索を使います。将来、法人番号↔JSIC の信頼できる追加データソースを接続すれば同じ列へ載せられます。

公式ページ: https://www.e-stat.go.jp/classifications/terms/10

## 最初のWindowsセットアップ

PowerShell を開き、プロジェクトルートで実行します。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup-windows.ps1
```

このスクリプトは必要に応じて Node.js LTS、Rust、Visual Studio C++ Build Tools を準備し、Codex の固定版を取得します。DuckDB は Rust crate の `bundled` ビルドで EXE 内へ組み込まれるため、別途 DuckDB / Queria / Python をインストールする必要はありません。

DuckDB: https://duckdb.org/docs/stable/clients/rust
Queria DuckDB 接続: https://docs.queria.io/connection/duckdb-cli/
Codex: https://github.com/openai/codex

## EXE / MSI を作る

```powershell
.\scripts\build-windows.ps1
```

生成先:

```text
src-tauri\target\release\bundle\nsis\*.exe
src-tauri\target\release\bundle\msi\*.msi
```

GitHub Actions の `.github/workflows/windows-build.yml` でも Windows インストーラーを生成できます。

## Codex / ChatGPT ログイン

アプリの「接続」→「Codex App Server」→「ChatGPTでログイン」を押します。

- 共有 API キーは不要
- 利用者ごとにブラウザで自分の ChatGPT アカウントへログイン
- Codex の認証情報はアプリ専用 `CODEX_HOME` から OS の資格情報ストアを利用
- `forced_login_method = "chatgpt"`
- `gpt-5.6-luna` が `model/list` に存在しない場合は LLM 機能を停止
- `thread/start` と `turn/start` の両方で Luna を明示
- `model/rerouted` で Luna 以外に変わった場合は処理を失敗させる

Codex App Server はローカル子プロセスとして起動し、JSONL over stdio で通信します。

OpenAI App Server docs: https://developers.openai.com/codex/app-server/
GPT-5.6 Luna: https://developers.openai.com/api/docs/models

## DuckDB 純正で全国法人を同期

Windows セットアップ後、アプリの「接続」→「DuckDB Native / 公開法人データ」→「DuckDB同期」。

EXE に組み込んだ DuckDB が、Queria の公開 DuckLake を直接 READ_ONLY で ATTACH します。外部 CLI や Python サブプロセスはありません。

```sql
INSTALL ducklake;
LOAD ducklake;
INSTALL httpfs;
LOAD httpfs;

ATTACH 'ducklake:https://data.queria.io/houjin_bangou/ducklake.duckdb'
  AS houjin_native (READ_ONLY);
ATTACH 'ducklake:https://data.queria.io/gbizinfo/ducklake.duckdb'
  AS gbiz_native (READ_ONLY);

SELECT ...
FROM houjin_native.main.mart_houjin_bangou h
LEFT JOIN gbiz_native.main.mart_gbizinfo_company g
  ON h.corporate_number = g.corporate_number;
```

JOIN 結果は同じローカル `company-master.duckdb` に materialize するため、2回目以降の検索はネットワークを使わず高速です。同期失敗時は既存 `companies` テーブルを残し、完成した staging table のみを最後に入れ替えます。

任意のローカル `.duckdb` / `.db` / `.parquet` / `.csv` / `.json` も「DuckDB/Parquet読込」から DuckDB 自身で直接読み込めます。

## 日本標準産業分類を入れる

1. e-Stat の日本標準産業分類ページから CSV を取得
2. 正規化

```powershell
python .\scripts\normalize-jsic.py .\downloaded-jsic.csv .\jsic-normalized.csv
```

3. アプリの「接続」→「産業分類」で `jsic-normalized.csv` を選択

正規化後の形式:

```csv
code,name,level,parent_code,revision,source_url
G,情報通信業,1,,2023-07,https://www.e-stat.go.jp/classifications/terms/10
39,...,2,G,2023-07,...
391,...,3,39,2023-07,...
3911,...,4,391,2023-07,...
```

## 検索例

自然文:

```text
関東の食品メーカー。従業員100名以上で、自社ECや通販に力を入れていそうな会社を5万件以内。
```

Luna が返すのは SQL ではなく、次のような構造です。

```json
{
  "prefectures": ["東京都", "神奈川県", "千葉県", "埼玉県"],
  "industry_codes": [],
  "industry_terms": ["食品製造", "通販", "EC"],
  "min_employees": 100,
  "keyword_any": ["EC", "通販", "オンラインショップ"],
  "limit": 50000
}
```

細分類コードを手入力する場合は前方一致です。`industry_code` / AI推定コードが存在する行では、`39` を指定すれば `3911` なども検索対象になります。

## メモリー

DuckDB に次を永続化します。

- `saved_searches`: Luna が作った検索条件と元プロンプト
- `company_lists`: 名前付き企業リスト
- `research_reports`: 企業別 Web 調査メモ、根拠 URL、調査ログ
- `companies.inferred_*`: Luna が Web 根拠から推定した業種

企業マスターと LLM 会話メモリーを分離しているため、企業数が増えても検索 UI の描画負荷は増えにくい構成です。

## Salesforce 接続

### Salesforce側

External Client App を作成し、Authorization Code + PKCE を利用できるようにします。

Callback URL は固定です。

```text
http://127.0.0.1:53682/callback
```

Account に以下のカスタム項目を作る想定です。

```text
API Name: CorporateNumber__c
Type: Text(13)
External ID: ON
Unique: ON
```

### CompanyMaster側

「接続」→「Salesforce」で次を入力します。

- Login URL: `https://login.salesforce.com` または My Domain
- External Client App の Client ID

ブラウザで本人が Salesforce にログインします。アクセストークン / リフレッシュトークンは Windows 資格情報ストアへ保存します。

検索画面でリスト名を決めて「Salesforce」を押すと、現在の検索条件全体をリスト化して Account へ Bulk API 2.0 Upsert します。画面の 100 行だけを送る処理ではありません。

現在の Account 標準マッピング:

```text
CompanyMaster                Salesforce Account
name                         Name
corporate_number             CorporateNumber__c
website                      Website
phone                        Phone
prefecture                   BillingState
city                         BillingCity
address                      BillingStreet
industry_name / inferred     Industry
employees                    NumberOfEmployees
business_summary             Description
```

Salesforce REST API version は `v67.0` に固定しています。

## API / コネクター拡張

LLM に任意 URL や任意 SQL を無制限に実行させるのではなく、接続先ごとにアダプターを追加します。詳細は `docs/CONNECTOR_SPEC.md`。

原則:

1. 認証はコネクター側
2. LLM は構造化パラメータだけ生成
3. アプリが schema 検証
4. コネクターが API を実行
5. 結果を共通 Company / ResearchReport 形式へ正規化
6. その結果だけ LLM が相談・分類・要約に利用

この方式なら、将来 EDINET、独自社内 API、CRM、外部企業 DB を同じ UI に足せます。

## 速度設計

- 数百万行: DuckDB
- 公開データ同期: DuckDB DuckLake READ_ONLY direct attach → local materialization
- UI: 100 行ページング
- 検索: DB 側で count/filter/order
- LLM: 検索全件には使わず、条件解釈と必要企業の深掘りだけ
- Salesforce: UI 行ではなく DB リストから一括送信
- 企業調査: 1 社単位で保存し、再検索時に再利用可能

## 現MVPの境界

- Windows インストーラーは Windows toolchain でビルドする必要があります。
- Salesforce の Bulk Job 作成・アップロードまでは実装済みですが、完了結果の定期ポーリング/失敗行再送 UI は次段階です。
- Salesforce の任意カスタム項目マッピング UI はまだ固定 Account マッピングです。
- 数十万〜数百万社を 1 回の Salesforce 送信にする用途は、今後ストリーミング/分割ジョブ化した方が安全です。現 MVP は主に数万社単位を想定しています。
- gBizINFO の属性が存在しない法人は空欄のまま残ります。母集団からは削除しません。
- Web 推定業種は AI 推定であり、正式 JSIC と混同しません。

## セキュリティ

- OpenAI API key をアプリに埋め込まない
- Codex は利用者本人の ChatGPT ログイン
- Salesforce も利用者本人の OAuth ログイン
- Salesforce token は OS credential store
- LLM から DB 生 SQL を直接実行しない
- 企業調査は read-only sandbox を基本とする
- 外部書き込みは Salesforce 等の明示コネクター経由

## 主要ソース

- OpenAI Codex App Server: https://developers.openai.com/codex/app-server/
- OpenAI Codex: https://github.com/openai/codex
- GPT-5.6 Luna: https://developers.openai.com/api/docs/models
- DuckDB Rust client: https://duckdb.org/docs/stable/clients/rust
- Queria DuckDB direct connection: https://docs.queria.io/connection/duckdb-cli/
- e-Stat 日本標準産業分類: https://www.e-stat.go.jp/classifications/terms/10
- Salesforce Bulk API 2.0: https://developer.salesforce.com/docs/atlas.en-us.api_asynch.meta/api_asynch/bulk_api_2_0.htm
