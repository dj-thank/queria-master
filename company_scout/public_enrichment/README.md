# Public Company Enrichment

企業名・所在地などを含む任意の企業CSVを、公開情報だけで補完するためのローカル実行ツールです。

入力元データはGitへコミットせず、国税庁法人番号公表サイト、Gビズインフォ、EDINET、各社公式サイトから取得・ダウンロードした公開情報を法人番号で結合します。元データの列は上書きせず、公開値と出典・一致品質・更新日を別列として保持します。

## 補完できる項目

- 法人番号
- 法人名、郵便番号、登記住所、法人状態
- 代表者、資本金、従業員数、設立年月日
- 公式Webサイト、事業概要、事業種目
- 最新売上、最新純利益、年度別財務
- 平均年齢、平均年収
- 公式サイト上の代表電話候補
- 公開情報から派生したコアキーワード
- JSIC単位の売上・純利益ランキング

## データソース

- 国税庁 法人番号公表サイト
- Gビズインフォの法人番号付与結果、基本情報、財務情報、職場情報
- EDINET API v2 / 有価証券報告書XBRL
- 法人の公式Webサイト

各データソースの利用条件、再配布条件、API制限、robots.txtを遵守してください。

## 入力

最低限、会社名と所在地が必要です。`SOURCE_ID` がなければ行番号からローカルIDを自動生成します。

```csv
SOURCE_ID,企業名,本店所在地,証券コード
local-001,サンプル株式会社,東京都千代田区丸の内1-1-1,
```

任意列として `証券コード`、JSICコード・名称などを持たせられます。`SOURCE_ID` はローカル入力内で一意な任意IDです。

## セットアップ

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

EDINETを使う場合は環境変数または `.env` にAPIキーを設定します。

```text
EDINET_API_KEY=your_key_here
```

## 基本フロー

### 1. 入力企業をSQLiteへ準備

```bash
python public_data_enricher.py prepare sample_input.csv --replace
```

### 2. 法人番号付与用CSVを生成

```bash
python public_data_enricher.py make-assignment --output input/法人番号付与用.csv
```

Gビズインフォの法人番号付与ツールを使う場合は、`SOURCE_ID` を返却結果にも残してください。

### 3. 公開データを `input/` に配置

対応するCSV/ZIPを `input/` に置きます。ファイル名ではなくヘッダー構造から種類を判定します。

- 法人番号付与結果
- Gビズインフォ 基本情報
- Gビズインフォ 財務情報
- Gビズインフォ 職場情報
- EDINET抽出結果
- 公式サイト電話番号抽出結果

### 4. 取込・派生・出力

```bash
python public_data_enricher.py run-all --input-dir input --output-dir output/csv
```

## EDINET平均年齢・平均年収

```bash
python edinet_salary_enricher.py \
  --db output/company_public_data.sqlite3 \
  --env .env \
  --output input/EDINET_平均年齢・平均年収.csv
```

対象は入力企業のうち証券コードがある企業です。最新の有価証券報告書を探索し、XBRLから平均年齢と平均年間給与を抽出します。

## 公式サイト代表電話

```bash
python official_site_phone_enricher.py \
  --db output/company_public_data.sqlite3 \
  --output input/公式サイト_電話番号.csv
```

公開企業マスタで確認できた公式サイトだけを対象に、同一ホスト内・robots.txt準拠・低速アクセスで代表電話候補を探索します。FAXらしい番号は減点し、根拠URLと根拠テキストを保存します。

## 照合ルール

自動採用は高確度一致に限定します。

- Gビズインフォ: `M00` かつヒット1件 → 自動採用
- `M01` / `M02`、複数候補、法人番号競合 → 原則レビュー
- 企業名だけの一致 → 自動採用しない

`--accept-prefix` を明示した場合のみ、`M01` / `M02` の単一候補を採用できます。

## 主な出力

`output/csv/` に以下を生成します。

- `companies_enriched.csv`
- `public_company_details.csv`
- `financial_history.csv`
- `review_required.csv`
- `source_audit.csv`

公開情報が存在しない項目は空欄のまま保持します。推測値では埋めません。

## Provenance / セキュリティ方針

- 入力元データそのものを公開リポジトリへ含めない
- 元データ固有のID体系、件数、URL、APIパス、ファイル名をコードやドキュメントへ埋め込まない
- APIキー、アクセストークン、Cookieをコミットしない
- 生成SQLite、CSV、ZIP、APIキャッシュをコミットしない
- 公開値には取得元・更新日・一致コード・信頼度を保持する

このディレクトリは、特定の民間データベースや特定の入力データセットに依存しない設計です。
