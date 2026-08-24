# アーキテクチャ

## 処理経路

```text
Queria 公開 DuckLake
  ├─ houjin_bangou.main.mart_houjin_bangou
  ├─ gbizinfo.main.mart_gbizinfo_company / subsidy / procurement / patent
  ├─ gbizinfo.main.mart_gbizinfo_certification / commendation
  ├─ edinet.main.mart_* / stg_financial_facts
  ├─ mhlw.*（女性活躍・介護・障害福祉・NDB）
  ├─ p_portal.main.procurement_award
  └─ metro_tokyo.ods の選定テーブル
             │
             │ Queria CLI / 読み取り専用 SQL
             ▼
cache/all-public-latest.partial/*.parquet
             │ 完了後に原子的 rename
             ▼
cache/all-public-latest/*.parquet
             │ DuckDB Python API / read_parquet
             ▼
data/queria_master.duckdb.building
             │ スキーマ検査・件数検査・索引・CHECKPOINT
             ▼
data/queria_master.duckdb
```

速度優先の運用経路では、更新用の2つの入力DBから一つの統合ランタイムDBを作ります。

```text
data/queria_master.duckdb          更新用の正規公開スナップショット
data/queria_enrichment.duckdb      更新用の証拠・状態・抑止層
             │  publish-runtime（読み取り専用入力、同一generation公開）
             ▼
data/queria_runtime.duckdb         利用時に跨ぐDBはこれ一つ
  ├─ core / raw / gbizinfo / edinet / mhlw / p_portal / metro_tokyo
  ├─ enrichment / compliance / crm
  └─ search.company_documents       1法人1行の検索・表示用マート
             │
             └─ data/search.sqlite   FTS5 trigramの読み取り専用副索引
```

この二層構成は速度と更新安全性の折衷ではなく、役割を分けた構成です。更新側は出典を失わない正規形を保持し、利用側は必要な列と現在値を同一DuckDBへ物理化します。したがって、利用時の法人検索・カテゴリ絞り込み・営業リスト作成は別DBへの毎回のJOINを必要としません。SQLite FTS5は全文候補の選別だけを担当し、最終的な値・証拠・抑止判定は統合DuckDBから読みます。

履歴Hojinjoho活動情報ZIPは別経路です。

```text
Hojinjoho ZIP（top-level JSON array）
             │  全member metadata・JSON payload・record検証、展開上限、SHA-256
             ▼
.<staging-name>.<uuid>.building
             │  完了後にatomic no-clobber hard link
             ▼
gbiz_archive staging DuckDB
  ├─ import_runs / archive_members
  └─ companies / activities
```

このstagingは非正本で、`core.*` / `gbizinfo.*` / runtime / 検索索引へ自動昇格しません。Basic CSV取込も対象外です。安全性と正規化は合成ZIPで自動テストしています。別途復元できた companion 監査記録には379,025,154 bytesなどの値がありますが、元ZIP本体は取得できず、現 importer の完走検証は未実施です。詳細は [`GBIZ_ARCHIVE_IMPORT_JA.md`](GBIZ_ARCHIVE_IMPORT_JA.md) を参照してください。

全公開スコープのローカル構成:

```text
raw.houjin_bangou          国税庁法人番号の全列
gbizinfo.company_summary   gBizINFO 法人サマリーの全列
gbizinfo.*                 補助金・調達・特許・認定・表彰の明細全列
edinet.*                   会社・提出書類・財務ファクト・ファンド
mhlw.*                     法人関連事業所と地域統計
p_portal.*                 政府電子調達落札実績
metro_tokyo.*              東京都ODSの選定テーブル
core.companies             NTA と gBizINFO の重複を排した統合法人マスタ
core.company_industries    全大分類・中分類・小分類を階層行へ正規化した JSIC コード
core.v_category_summary    カテゴリ別の重複排除済み法人件数
core.v_company_source_*    ソース別法人番号明細・件数・結合可否
core.v_*                   検索・候補分類・活動件数ビュー
meta.*                     出典、スキーマ、24テーブル件数、更新証跡、収録境界
```

## なぜ DuckLake を素の DuckDB から直接 ATTACH しないか

Queria は DuckLake と DuckDB の互換バージョンを管理する公式クライアントを提供しています。本プロジェクトは、公開カタログへの接続を Queria CLI に任せ、抽出済み Parquet から先を DuckDB 純正エンジンで処理します。これにより、カタログの書き換え事故やクライアント互換性の問題を避けつつ、検索時は完全にローカルで動作します。

## 情報通信業の判定

現行 gBizINFO の `business_items` は、`G:情報通信業-40:インターネット附随サービス業-401:` のようなラベル付き文字列です。
全公開ビルドでは `|` で分かれた複数パスを分割し、大・中・小分類の名称とコードを `core.company_industries` へ保存します。
情報通信業用の互換列 `jsic_codes_raw` と、全業種用の `jsic_codes_all_raw` は元の `business_items_raw` と併存します。

```text
G
G37 / G38 / G39 / G40 / G41
```

既定スコープでは、情報通信業の抽出条件を gBizINFO 側へ先にプッシュダウンしてから国税庁法人番号マスタを結合します。
全公開スコープではgBizINFO法人サマリーを起点に全法人を保持し、結果の法人番号は13桁文字列のまま保持します。

## 収録境界

現行のQueriaカタログから、法人番号・活動・提出書類・財務・事業所・地域統計を含む24テーブルを
スナップショットとしてローカル化します。gBizINFO公式の財務・職場情報は、Queriaの法人サマリーにある最新指標に加え、
Queriaが公開するEDINET財務ファクトは縦持ち明細として収録します。gBizINFO側の生明細でQueriaが公開していない範囲は
`summary_only` として扱います。
EDINET財務ファクトも現行Queriaの段階充填分であり、訂正報告書・全期間の完全複製ではありません。
モデル・個人プロフィールはこの法人データセットの対象外です。機械的に「すべて」と表示しないよう、DBの
`meta.coverage_boundary` に `complete_source_snapshot` / `selected_source_snapshot` / `summary_only` / `not_in_scope` を保存します。

## 原子的更新

既存 DB を直接更新しません。新 DB を `.building` へ作り、必要列の存在、0件でないこと、索引作成、`CHECKPOINT` の完了後にだけ `os.replace` で本番ファイルへ差し替えます。途中失敗時は既存 DB が残ります。

## 秘密情報

- gBizINFO API トークンは使用しません。
- Queria のログイン情報は Queria 標準設定領域に保存されます。
- ZIP、SQL、DuckDB、更新ログにはトークンを保存しません。
- `QUERIA_NO_TELEMETRY=1` を実行環境に設定します。
