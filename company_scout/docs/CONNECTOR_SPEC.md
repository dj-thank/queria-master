# CompanyMaster Connector Contract

CompanyMaster の外部接続は「LLMが任意APIを自由実行」ではなく、明示的なアダプター境界を使います。

## 入力

LLMが生成してよいもの:

- 検索条件 (`SearchPlan`)
- 企業調査指示
- コネクターが公開した列挙型/フィルタ値
- 出力項目マッピングの提案

LLMが直接保持・生成しないもの:

- OAuth refresh token
- API secret
- Salesforce access token
- DB接続パスワード

## アダプターの責務

各コネクターは次を実装します。

```text
status() -> ConnectorStatus
login()/authorize() -> browser or local auth flow
schema() -> fields/capabilities
search(validated_params) -> normalized records
fetch_company(corporate_number) -> normalized company detail
export(records, mapping) -> job/result
```

## 共通企業キー

日本法人では `corporate_number` を最優先キーにします。

取得元に法人番号がない場合は、以下を候補照合に使いますが、自動確定は慎重に行います。

```text
normalized company name
prefecture/city/address
official website domain
phone
```

## LLMとの境界

```text
user text
  -> Luna
  -> structured JSON
  -> Rust validation
  -> connector / DuckDB
  -> normalized result
  -> Luna (only when analysis is needed)
  -> UI / DB / Salesforce
```

## 追加候補

- EDINET: 上場/有報/財務
- 社内企業DB API
- 独自CRM
- 外部企業情報サービス
- Webサイト crawler/indexer

各接続先の規約・ライセンス・レート制限を守り、非公開APIの逆解析を前提にしません。
