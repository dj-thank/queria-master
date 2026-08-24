# Public Company Enrichment v1.1.0

任意の企業CSV / XLSXを、政府公開データ・EDINET・企業公式Webサイトなどの公開・公式情報だけで補完するローカル実行パイプラインです。

入力企業リスト、生成SQLite、取得キャッシュ、APIキーはリポジトリへ保存しません。入力元の列は変更・削除せず、公開値、出典、照合品質、要確認理由を別列として追加します。公開情報が存在しない項目は推測で埋めず、空欄のまま保持します。

## 現在の機能

- CSV / XLSXから任意の企業リストを読み込み
- 入力元の全列を保持したままローカルSQLiteへ準備
- `SOURCE_ID` がない入力にはローカル専用IDを自動生成
- 法人番号候補を履歴として保存し、高確度一致だけを自動採用
- 法人番号が競合した場合は採用解除してレビューへ戻す
- 法人番号確定済み企業だけへ基本・財務・職場情報を結合
- EDINET XBRLから平均年齢・平均年間給与を抽出
- 企業公式サイトから電話番号候補と根拠URLを取得
- 公式サイトの限定抜粋から情シス子会社・SES/SI・受託・運用保守の営業優先根拠JSONを生成
- 公式根拠付きの検証済み連絡先リファレンスをローカルDBへ反映
- 年度別財務、最新財務、コアキーワード、JSIC単位ランキングを出力
- 取込元、SHA-256、照合状態、要確認理由を監査可能な形で保存

## 対応する公開情報

- Gビズインフォの法人番号付与結果、基本情報、財務情報、職場情報
- 国税庁法人番号公表サイトの全件CSV
- EDINET API v2 / 有価証券報告書XBRL
- 法人自身が管理する公式Webサイト
- `reference/verified_public_contacts.csv` に収録した公式根拠付き連絡先

各提供元の利用条件、再配布条件、API制限、robots.txtを確認して利用してください。

## 補完できる主な項目

- 法人番号、法人名、郵便番号、登記住所、法人状態
- 代表者、資本金、従業員数、設立年月日
- 公式Webサイト、事業概要、事業種目
- 最新売上、最新純利益、年度別財務
- 平均年齢、平均年収
- 公式サイト上の電話番号候補、電話種別、電話用途、根拠URL
- 公開情報から派生したコアキーワード
- JSIC単位の売上・純利益ランキング

## 必要環境

- Python 3.11以上
- Windows / macOS / Linux
- EDINETを使う場合のみEDINET APIキー

CIでは Python 3.11 / 3.12 / 3.13 で構文チェックとオフラインテストを実行します。

## Windowsで最短実行

`company_scout/public_enrichment/` で次の順に実行します。

```text
setup_windows.cmd
prepare_windows.cmd companies.csv
integrate_windows.cmd
status_windows.cmd
```

XLSXを入力する場合も `prepare_windows.cmd` へファイルを渡せます。

## 手動セットアップ

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 入力形式

最低限、企業名と所在地が必要です。日本語・英語の代表的な列名を自動判定します。

```csv
SOURCE_ID,企業名,本店所在地,証券コード,JSIC細分類コード,JSIC細分類名
local-001,例示株式会社,東京都千代田区1-1-1,1234,3911,受託開発ソフトウェア業
```

`SOURCE_ID` がなければ `row-00000001` 形式のローカルIDを生成します。外部サービスのIDではありません。

## 基本フロー

### 1. 入力企業を準備

CSV:

```bash
python public_data_enricher.py prepare companies.csv --replace
```

XLSX:

```bash
python public_data_enricher.py prepare companies.xlsx --sheet 企業DB --replace
```

既存DBを作り直す場合だけ `--replace` を使用してください。

### 2. 法人番号付与用CSVを作る

```bash
python public_data_enricher.py make-assignment \
  --output input/corporate-number-assignment.csv \
  --chunk-size 10000
```

Gビズインフォの法人番号付与ツールを使う場合は、返却CSVにも `SOURCE_ID` を残してください。

### 3. 公開CSV / ZIPを `input/` に置く

ファイル名ではなくヘッダー構造から次の種類を判定します。

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

## 法人番号の照合ルール

自動採用は高確度一致に限定します。

- Gビズインフォ: `M00` かつ一意候補
- 国税庁全件CSV: 法人名＋住所の正規化完全一致かつ入力側候補が一意

次は原則レビューです。

- `M01` / `M02`
- 複数候補
- 企業名だけの一致
- 異なる情報源から別の法人番号が高確度で返った場合

`--accept-prefix` を明示した場合だけ `M01` / `M02` の単一候補を採用できます。通常運用では推奨しません。

法人番号が1件も採用されていない状態では、基本情報・財務情報・職場情報の無制限取込を拒否します。対象外法人をローカルDBへ混入させないための安全措置です。

## EDINET平均年齢・平均年収

`.env` にAPIキーを設定します。

```text
EDINET_API_KEY=your_key_here
```

```bash
python edinet_salary_enricher.py \
  --db output/company_public_data.sqlite3 \
  --env .env \
  --output input/edinet-metrics.csv
```

対象は証券コードのある入力企業です。受信ZIPサイズ、展開後サイズ、メンバー数に上限を設けています。

## 公式サイトの電話番号候補

```bash
python official_site_phone_enricher.py \
  --db output/company_public_data.sqlite3 \
  --output input/official-site-phones.csv
```

法人番号で確認済みの公式URLだけを対象にします。

- 同一ホスト内だけを巡回
- robots.txtを確認
- 低速アクセス
- ページ数・受信サイズ・リダイレクト回数を制限
- localhost、プライベートIP、リンクローカル、予約アドレスを拒否
- 根拠URLが公式サイトと異なる結果は取込拒否

自動抽出した番号は候補です。FAX、支店、採用、広報など用途の最終判定には根拠URLを確認してください。

同じ取得処理は、HTML全文を保存せず、事業シグナルの最大240文字の抜粋、同一ホストURL、SHA-256だけをprogress JSONL schema v2へ保持します。`ses_priority_json.py` は次を提供します。

```bash
python ses_priority_json.py prioritize-targets \
  --input phone_targets_enriched.csv \
  --output phone_targets_prioritized.csv \
  --summary ses_priority_seed_summary.json

python ses_priority_json.py export \
  --targets phone_targets_prioritized.csv \
  --progress 'work/shard-*/progress.jsonl' \
  --jsonl output/ses_priority_profiles.jsonl \
  --csv output/ses_priority_profiles.csv \
  --summary output/ses_priority_summary.json
```

seed scoreはクロール順だけに使う弱い推定です。最終A/B/Cは公式サイト本文の根拠と連絡可能性から決定しますが、電話は全件 `candidate_needs_review`、親会社関係は明示文言があっても名称確認前はcandidateです。

## 検証済み公式連絡先リファレンス

`reference/verified_public_contacts.csv` には、企業自身の公式ページを根拠として確認した連絡先を、電話種別・電話用途・代表電話フラグ・公式URL・根拠URL・信頼度・確認日とともに保存しています。

用途限定番号を代表電話として扱いません。

ローカルDBへ反映する場合:

```bash
python import_verified_contacts.py \
  --db output/company_public_data.sqlite3 \
  --contacts reference/verified_public_contacts.csv \
  --replace-source \
  --output output/csv/verified_contacts_reflected.csv
```

照合優先順位は次のとおりです。

1. 呼び出し側が明示した `SOURCE_ID`
2. 証券コード＋正規化企業名が一意
3. 正規化企業名＋正規化所在地が一意

企業名だけの一致は自動採用しません。曖昧な行は `verified_contact_import_audit` にレビュー対象として保存します。

詳細は [`reference/README.md`](reference/README.md) を参照してください。

## 出力

`output/csv/` に主に次を生成します。

- `companies_enriched.csv` — 入力元列を保持した統合結果
- `public_company_details.csv` — 1企業1行の公開情報
- `financial_history.csv` — 法人番号×事業年度の財務履歴
- `industry_rankings.csv` — 公開財務から計算したランキング
- `review_required.csv` — 未確定・競合・低信頼度候補
- `source_audit.csv` — 取込元、SHA-256、件数、エラー
- `integration_summary.json` — 件数サマリー
- `verified_contacts_reflected.csv` — 検証済み連絡先のローカル反映結果

ランキングは入力企業群と公開財務値を対象に再計算した派生値で、外部サービスの既存順位を複製するものではありません。

## 状態確認

```bash
python public_data_enricher.py status
```

`integrity` が `ok` であることを確認してください。

## テスト

```bash
python -m unittest discover -s tests -v
```

テストはネットワークへ接続せず、合成データだけで実行します。恒久CIは `.github/workflows/public-enrichment-tests.yml` です。

## セキュリティとデータ保護

- 入力企業リストをコミットしない
- 生成SQLite、CSV、XLSX、ZIP、取得キャッシュをコミットしない
- APIキー、トークン、Cookieをコミットしない
- 特定の入力元に固有のID、件数、URL、APIパスをコードやドキュメントへ埋め込まない
- 公開値には取得元、更新日、一致コード、信頼度を保持する
- 競合する高確度法人番号は自動採用せず、候補履歴を残してレビューへ戻す
- 公式サイト取得ではSSRF対策と同一ホスト制約を適用する
- EDINET ZIPには受信・展開上限を適用する

詳細は [`SECURITY.md`](SECURITY.md) を参照してください。
