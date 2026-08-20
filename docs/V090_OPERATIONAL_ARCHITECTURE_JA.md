# Queria 0.9 運用アーキテクチャ

## 目的

0.9は「EXEが開く」だけでなく、利用者が選んだ設定、実際に開いたDBと索引、利用可能な機能、データ拡張の進捗を確認できる状態を契約とします。

## アーティファクトの役割

| 成果物 | 役割 | 主な書き込み処理 |
|---|---|---|
| `queria_master.duckdb` | 公開ソースから再構築するcanonical DB | `refresh` |
| `queria_enrichment.duckdb` | 証拠、拡張値、調査状態、抑止 | 単一writer |
| `queria_runtime.duckdb` | canonicalとenrichmentを統合した検索用snapshot | `build-runtime` |
| `search.sqlite` | runtime DB専用のFTS・カテゴリ索引 | `build-search-index` |
| `config/queria-settings.json` | portableな保存設定 | GUI / `configure` |

`runtime` と `search.sqlite` は同じ生成世代でなければ使用しません。0.9ではruntime manifestとSQLite metadataへ同じ`generation_id`を記録し、サイズだけでなく世代IDを照合します。

## 設定の優先順位

1. CLIの明示パス
2. 項目別環境変数
3. 保存設定
4. アプリホーム配下の既定パス

項目別環境変数:

```text
QUERIA_MASTER_HOME
QUERIA_CANONICAL_DB
QUERIA_ENRICHMENT_DB
QUERIA_RUNTIME_DB
QUERIA_SEARCH_INDEX
QUERIA_SETTINGS
```

検索系の暗黙DBはruntime、更新・統合系の入力はcanonicalです。`DEFAULT_DB`を一律runtimeへ変更しません。

## 設定・診断画面

Desktop EXEの［設定・診断］では次を確認・変更できます。

- アプリホーム
- canonical / enrichment / runtime DB
- search index
- 既定表示件数
- 起動時index検証
- 法人数、refresh ID、generation ID、機能可否

DBとindexが壊れていても画面自体は起動し、正しいペアを選び直せます。保存は一時ファイルからの原子的置換です。

CLIでは次を使います。

```powershell
queria-master configure
queria-master configure --home D:\Queria --default-limit 500
queria-master app-health
```

## 実データ拡張

同梱済み厚労省公開データには、法人番号で結合できる介護・障害福祉事業所の電話・URLがあります。0.9では次で証拠付き同期できます。

```powershell
queria-master sync-embedded-public
```

実全量での同期結果:

- 事業所レコード: 401,238件
- 事業所電話がある法人: 100,515法人
- 事業所URLがある法人: 65,092法人

これらは本社代表連絡先ではありません。`enrichment.company_establishments`へ`contact_scope=establishment`として保存し、`company_contact_points`の代表電話へ混ぜません。営業利用時は拠点種別、サービス種別、出典、抑止を確認します。

## 配布ゲート

完全版ZIPは、次の読み取り専用監査がすべて通った場合だけ生成します。

- canonical法人件数が1以上
- 法人番号重複0
- runtime法人件数と検索profile件数がcanonicalと一致
- search index件数がruntimeと一致
- runtime/indexの`generation_id`一致
- enrichment/runtime schemaが読み取り可能

途中生成物は`.building`または`.part`へ書き、検査後にのみ正式名へ切り替えます。

## データ拡張の次段階

1. 既存の構造化公開データを法人番号で追加統合
2. 既知の公式URL 44,433法人を限定crawlし、1回の取得から電話・フォーム・事業内容を抽出
3. 公式サイト候補発見を独立adapterとして追加
4. workerはDuckDBへ直接書かず、JSONL/Parquet spoolへ出力
5. 単一ingestion writerが検証・重複排除・証拠付与して反映

全法人に値が存在すると仮定しません。KPIは`eligible`、`processed`、`found`、`verified`、`blocked_by_policy`、鮮度を分けます。推定メール、SMTP probing、個人アドレス生成は行いません。

## Proレビューの扱い

ChatGPT Proレビューは設計提案として使用し、次を採用しました。

- role-aware DB defaults
- 保存設定とResolvedArtifacts
- Desktop startup preflightと回復画面
- runtime/index generation ID
- auditとbundleのstrict gate
- 公開データ優先、host-aware crawl、単一writer

active generation切替、append-only observations、差分索引更新は後続段階です。ローカル実装・テスト・実データ検証が未完了の提案は完成機能として扱いません。
