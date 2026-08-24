# 公式サイト発見・検証・抽出の境界

## 4つの検索・調査機能

同じ「検索」という語でも、次は別機能です。

| 機能 | 入力 | 出力 | 禁止事項 |
|---|---|---|---|
| 法人ローカル検索 | runtime / `search.sqlite` | 法人一覧 | Webアクセス |
| 一般Web調査 | 会社と調査質問 | `ResearchReport` | enrichmentへの自動確定 |
| 公式サイト発見 | 法人IDと検索結果metadata | `official_candidate / needs_review` | 候補サイト取得、連絡先抽出、自動昇格 |
| 既知サイト抽出 | 検証済み`official_homepage` | 証拠付きcontact facts | Web検索、URL推測、別host探索 |

処理順は固定です。

```text
法人ID
  → Web検索adapter
  → 未検証candidate
  → 法人同一性・公式性の明示検証
  → verified official_homepage
  → 1サイト1取得の抽出
  → enrichment単一writer
  → runtime/index同一generation公開
```

## 発見adapterのJSONL契約

1行は1法人の検索結果です。adapterは利用規約・レート制限を満たす任意の検索providerとして実装し、Queria coreへHTTP clientを持ち込みません。

```json
{"corporate_number":"1234567890123","company_name":"例株式会社","prefecture_name":"東京都","city_name":"港区","provider":"licensed_search","hits":[{"url":"https://example.jp/","rank":1,"query":"例株式会社 公式","title":"例株式会社","snippet":"会社概要","confidence":0.82,"observed_at":"2026-08-24T00:00:00Z"}]}
```

```powershell
queria-master import-website-discovery --file work\discovery.jsonl
queria-master verify-website 1234567890123 https://example.jp/ `
  --method manual_identity_review --reviewer operator-01 `
  --evidence "会社概要の法人名・本店所在地がcanonical記録と一致"
queria-master collect-enrichment --worker-id extractor-01
```

発見結果はruntimeの公式URLになりません。`verify-website` は既に保存されたcandidateだけを、定義済みmethod・reviewer・法人同一性根拠とcandidate evidence IDを伴う明示レビューで昇格します。URL fingerprintが変わった場合だけ下流抽出を再queueします。昇格時は認証情報付きURL、非標準port、localhost系名、公開範囲外のIP literalを拒否します。接続時には全DNS応答を検査して公開IPへ固定し、redirectごとに同一hostとHTTPS維持を再検証します。

## 公開と読み取り

`company_scout/public_enrichment` のSQLiteはstagingです。`corporate_matches.status='accepted'` の行だけをbridgeが読み、公開マスタのURLはcandidate、公開Web URLかつ同一公式hostの根拠を持つサイト連絡先はverified factとしてenrichmentへ保存します。レビュー行は正本へ入りません。

```powershell
queria-master --db data\queria_master.duckdb integrate-public-enrichment `
  --staging-db company_scout\public_enrichment\output\company_public_data.sqlite3 `
  --enrichment-db data\queria_enrichment.duckdb `
  --runtime-db data\queria_runtime.duckdb `
  --search-index data\search.sqlite
```

runtimeと索引はstaging名で完成・相互検証してから正式名へ置換します。置換途中や障害で世代が異なる間は、索引検証が失敗しDuckDBへフォールバックするため、混成索引を検索しません。

## 値の解決

公式URLは`verified + official_homepage`だけ、電話・メール・フォームは`allowed + found/verified`かつ抑止対象外だけを採用します。複数値は次の順で決めます。

1. `verified`を`found`より優先
2. confidence降順
3. 観測日時降順
4. 正規化値とIDによる安定順

同じresolver viewをCLIの営業出力とruntimeが使い、検索索引はruntimeからだけ生成します。

抽出直後のcontactは `sales_eligibility=review` です。`review-contact --decision allowed|not_allowed --reviewer ... --reason ...` だけが利用可否を変更し、変更前後と担当者・理由は `enrichment.contact_reviews` へ追記します。汎用 `import-enrichment` は `allowed` contactと `verified official_homepage` の直接投入を拒否するため、JSONLからレビュー境界を迂回できません。
