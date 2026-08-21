# Public Company Enrichment

任意の企業CSVまたはXLSXを、公開・公式情報だけで補完するローカル実行パイプラインです。

入力企業リスト、生成SQLite、取得キャッシュ、APIキーはリポジトリへ保存しません。元列は変更せず、公開値・出典・照合品質・要確認理由を別列として追加します。公開情報が存在しない項目は、推測で埋めず空欄のまま保持します。

## 対応する公開情報

- Gビズインフォの法人番号付与結果、基本情報、財務情報、職場情報
- 国税庁法人番号公表サイトの全件CSV
- EDINET API v2と有価証券報告書XBRL
- 法人の公式Webサイトに掲載された代表電話候補

取得元ごとの利用条件、再配布条件、API制限、robots.txtを確認して利用してください。

## 補完できる主な項目

- 法人番号、法人名、郵便番号、登記住所、法人状態
- 代表者、資本金、従業員数、設立年月日
- 公式Webサイト、事業概要、事業種目
- 最新売上、最新純利益、年度別財務
- 平均年齢、平均年収
- 公式サイト上の代表電話候補と根拠URL
- 公開情報から派生したコアキーワード
- JSIC単位の売上・純利益ランキング

## 必要環境

- Python 3.11以上
- Windows、macOS、Linux
- EDINETを使う場合のみEDINET APIキー

## セットアップ

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Windowsでは `setup_windows.cmd` でもセットアップできます。

## 入力形式

最低限、企業名と所在地が必要です。列名は日本語・英語の代表的な表記を自動判定します。

```csv
SOURCE_ID,企業名,本店所在地,証券コード,JSIC細分類コード,JSIC細分類名
local-001,例示株式会社,東京都千代田区1-1-1,1234,3911,受託開発ソフトウェア業
```

`SOURCE_ID` がない場合は、入力行から `row-00000001` 形式のローカルIDを生成します。これは外部サービスのIDではありません。

## 基本フロー

### 1. 入力企業をSQLiteへ準備

CSVの場合：

```bash
python public_data_enricher.py prepare companies.csv --replace
```

XLSXの場合：

```bash
python public_data_enricher.py prepare companies.xlsx --replace
# 先頭以外のシートを使う場合だけ: --sheet "対象シート名"
```

既存データベースを作り直す場合だけ `--replace` を使用してください。

### 2. 法人番号付与用CSVを作る

```bash
python public_data_enricher.py make-assignment \
  --output input/corporate-number-assignment.csv \
  --chunk-size 10000
```

Gビズインフォの法人番号付与ツールを使う場合は、返却CSVにも `SOURCE_ID` を残してください。

### 3. 公開CSVまたはZIPを `input/` に置く

ファイル名ではなくヘッダー構造から、次の種類を自動判定します。

- 法人番号付与結果
- 国税庁全件CSV
- Gビズインフォ基本情報
- Gビズインフォ財務情報
- Gビズインフォ職場情報
- EDINET抽出結果
- 公式サイト電話番号抽出結果

### 4. 取込・派生・出力

```bash
python public_data_enricher.py run-all \
  --input-dir input \
  --output-dir output/csv
```

Windowsでは `integrate_windows.cmd` を利用できます。

## 照合ルール

自動採用は高確度一致に限定します。

- Gビズインフォの `M00` かつヒット1件
- 国税庁全件CSVで、法人名と住所の正規化完全一致かつ入力側候補1件

次は原則として要確認です。

- `M01`、`M02`
- 複数候補
- 企業名だけの一致
- 異なる情報源から別の法人番号が高確度で返った場合

`--accept-prefix` を明示した場合だけ、`M01` / `M02` の単一候補を採用できます。通常運用では推奨しません。

法人番号が1件も採用されていない状態では、基本情報・財務情報・職場情報の全件取込を拒否します。対象外法人をローカルDBへ取り込まないための安全措置です。

## EDINET平均年齢・平均年収

`.env` にAPIキーを設定します。

```text
EDINET_API_KEY=your_key_here
```

実行例：

```bash
python edinet_salary_enricher.py \
  --db output/company_public_data.sqlite3 \
  --env .env \
  --output input/edinet-metrics.csv
```

対象は証券コードのある入力企業です。受信ZIPサイズ、ZIP展開後サイズ、メンバー数に上限を設けています。

## 公式サイト代表電話

```bash
python official_site_phone_enricher.py \
  --db output/company_public_data.sqlite3 \
  --output input/official-site-phones.csv
```

法人番号で確認済みの公式URLだけを対象にします。次の制限があります。

- 同一ホスト内だけを巡回
- robots.txtを確認
- 低速アクセス
- ページ数と受信サイズを制限
- localhost、プライベートIP、リンクローカル、予約アドレスを拒否
- 根拠URLが公式サイトと異なる電話番号CSVは取込拒否

電話番号は候補です。FAX、支店番号、採用窓口などの最終確認には根拠URLを使用してください。

## 出力

`output/csv/` に次を生成します。

- `companies_enriched.csv` — 元列を保持した統合結果
- `public_company_details.csv` — 1企業1行の公開情報
- `financial_history.csv` — 法人番号×事業年度の財務履歴
- `industry_rankings.csv` — 公開財務から計算したランキング
- `review_required.csv` — 未確定・競合・低信頼度候補
- `source_audit.csv` — 取込元、SHA-256、件数、エラー
- `integration_summary.json` — 件数サマリー

ランキングは入力企業群と公開財務値を対象に再計算した派生値です。外部サービスの既存順位を複製するものではありません。

## 状態確認

```bash
python public_data_enricher.py status
```

`integrity` が `ok` であることを確認してください。

## テスト

```bash
python -m unittest discover -s tests -v
```

テストはネットワークへ接続せず、合成データだけで実行します。

## データ保護方針

- 入力企業リストをコミットしない
- 生成SQLite、CSV、XLSX、ZIP、取得キャッシュをコミットしない
- APIキー、トークン、Cookieをコミットしない
- 特定の入力元に固有のID、件数、URL、APIパスをコードへ埋め込まない
- 公開値には取得元、更新日、一致コード、信頼度を保持する
- 競合する高確度法人番号は自動採用せず、候補履歴を残してレビューへ戻す
