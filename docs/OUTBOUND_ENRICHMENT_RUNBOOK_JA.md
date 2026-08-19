# 営業向け法人データ拡張ランブック

## 目的

`data/queria_master.duckdb` は正規の法人マスタです。営業向けのホームページ、電話、公開メール、問い合わせフォーム、所在地の補足は、正規DBへ列追加・上書きせず、同じ法人番号をキーにした `data/queria_enrichment.duckdb` へ保存します。

拡張DBの各事実は、原則として次の組で追跡できます。

```text
法人番号 × 項目 × 出典URL × 取得日時 × 内容ハッシュ × ポリシー状態
```

公式ページに明記された値だけを取り込み、AIによるメールアドレス生成、SMTP受信箱探索、robots.txtの迂回は行いません。

## 初回セットアップ

正規DBが既にある場合:

```powershell
.\.venv\Scripts\python.exe -m queria_master init-enrichment
.\.venv\Scripts\python.exe -m queria_master seed-enrichment
```

`seed-enrichment` は全法人を5項目（website / phone / email / form_url / location）の再開可能なタスクへ分解します。正規DBにある `company_url` と `full_address` は、それぞれ `gbizinfo.company_summary` / `canonical.company_summary` を出典キーとして拡張DBへ冪等にシードします。再実行しても同じ法人・URL・住所の現在値は重複しません。

別の保存先を使う場合:

```powershell
.\.venv\Scripts\python.exe -m queria_master --db data\queria_master.duckdb init-enrichment --enrichment-db data\queria_enrichment.duckdb
.\.venv\Scripts\python.exe -m queria_master --db data\queria_master.duckdb seed-enrichment --enrichment-db data\queria_enrichment.duckdb
```

## 公式ページ収集

取得処理は、タスクをリースするワーカーと、DuckDBへ反映する単一writerの境界を持ちます。

```powershell
.\.venv\Scripts\python.exe -m queria_master collect-enrichment `
  --worker-id worker-01 `
  --field phone `
  --batch-size 20 `
  --max-tasks 100
```

既定では robots.txt を確認し、1ページあたり2MB、タイムアウト15秒、リクエスト間隔0.25秒で動きます。利用条件が確認できない場合は取得せず `needs_review`、robots.txtで拒否された場合は `blocked_by_policy` として残します。大量実行では、サイトごとのレート制限・利用規約・停止要求を別途設定してください。

HTMLから抽出する値は次のとおりです。

- JSON-LDの `email` / `telephone` / `url`
- `mailto:` / `tel:` リンク
- ページに表示されたメールアドレス・電話番号
- `お問い合わせ`、`contact` 等を示すリンク・フォームURL

メールとフォームは初期状態で `sales_eligibility='review'` です。出典と法務運用を確認したレコードだけを、明示的な取り込みJSONLで `allowed` に昇格してください。

保存済みHTMLをネットワークなしで処理する場合:

```powershell
.\.venv\Scripts\python.exe -m queria_master parse-contact-page `
  --corporate-number 1234567890123 `
  --url https://example.jp/contact `
  --html-file work\example-contact.html `
  --out work\example-enrichment.jsonl
.\.venv\Scripts\python.exe -m queria_master import-enrichment --file work\example-enrichment.jsonl
```

## 状態管理と再開

外部ワーカーは直接テーブルを書かず、次のコマンド/APIを使います。

```powershell
.\.venv\Scripts\python.exe -m queria_master claim-enrichment `
  --worker-id worker-01 --field email --batch-size 100

.\.venv\Scripts\python.exe -m queria_master complete-enrichment `
  1234567890123 email official_site `
  --state not_found_after_policy --worker-id worker-01
```

公開状態として扱う状態は以下です。

```text
found
not_found_after_policy
not_applicable
needs_review
blocked_by_policy
```

内部運用では `pending` / `leased` / `failed` も使用します。リース期限切れの `leased` は次回claimで再取得できます。`attempt_count`、`input_fingerprint`、`policy_code`、`worker_run_id`、`last_error`を保持するため、全件を何度も無条件に取り直す必要はありません。

## 営業リスト生成

```powershell
.\.venv\Scripts\python.exe -m queria_master sales-ready `
  --max-rows 100000 `
  --out exports\sales_ready_accounts.csv
```

出力は `crm.v_sales_ready_accounts` から作られます。`allowed` かつ `found` の電話・メール・フォームだけを対象にし、法人・連絡先・ドメインの抑止を適用します。問い合わせフォームはメールアドレスの代替として保存されますが、初期抽出値は `review` のため、確認なしで営業送信対象にはなりません。

抑止レコードの例:

```json
{"kind":"suppression","corporate_number":"1234567890123","suppression_type":"email","value":"info@example.jp","reason":"user_request","source":"crm","source_url":"https://example.jp/contact"}
```

`suppression_type` は `email`、`phone`、`domain`、`corporate_number` を使用できます。抑止は削除せず、有効期間と出典を残します。

## 5百万件級での運用境界

この実装は5百万件級を「一括メモリ展開」せず、次の段階で処理します。

1. DuckDBから全法人の状態を作る（seed）
2. claimで小さなバッチをリースする
3. 公式URLの1ページだけを取得する
4. JSONL相当の証拠レコードを単一writerで反映する
5. 成功・不存在・ポリシー拒否を項目ごとに確定する

正規DBの更新は拡張DBを削除しないため、法人番号が継続する限り補足調査を再利用できます。新しい法人は `seed-enrichment` の再実行で追加されます。正規DBの再構築中に同じDuckDBへ拡張テーブルを作ることはありません。

## 速度優先の利用DBを作る

検索・詳細表示・リスト作成を毎回別DBへJOINせずに実行する場合は、更新完了後に統合ランタイムDBを再生成します。

```powershell
.\.venv\Scripts\python.exe -m queria_master build-runtime `
  --db data\queria_master.duckdb `
  --enrichment-db data\queria_enrichment.duckdb `
  --out data\queria_runtime.duckdb
```

このファイルは正規DBと拡張DBから再構築する読み取り用スナップショットです。既存ファイルを直接更新せず、検査済みの `.building` を原子的に差し替えます。更新処理は正規DB・拡張DBへ行い、利用者・EXEは `--db data\queria_runtime.duckdb` を指定してください。

なお、国税庁・gBizINFOに公式URLが存在しない法人へ、検索エンジン結果や会社名からURLを推測して全件補完することはしません。URL発見を追加する場合は、利用規約とレート制限を確認した独立アダプターを `source_key` 別に実装し、候補は `official_candidate` / `needs_review` として取り込みます。AI推定メールや個人宛アドレスは `allowed` へ自動昇格させません。

## 検証

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

合成試験では、正規DBの不変性、seedの冪等性、出典履歴、連絡先の正規化、抑止の優先、claim/completeのリース復旧、公式HTMLの抽出を検証します。
