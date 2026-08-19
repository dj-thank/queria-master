# 検証レポート 0.5.0 — 全量拡張スナップショット

検証日: 2026-08-19
対象: Queria Master 0.5.0 / `all-public`

## 結論

Queria が現時点で公開している対象24テーブルを取得し、国税庁法人番号とgBizINFO法人サマリーの和集合を法人番号で統合した。
法人マスターは5,823,039件、13桁法人番号の形式不正・重複は0件だった。追加したEDINET、厚生労働省、政府電子調達、東京都ODSの
法人関連明細は、`core.v_company_source_records` / `core.v_company_source_counts` からソース別に検索できる。

これは「インターネット上の全情報」や「全自治体・全個人・全モデル情報」の収録を意味しない。`meta.coverage_boundary` に、
現行Queriaの完全スナップショット、選定スナップショット、要約のみ、対象外を分離して記録している。

## 全量スナップショット

| 指標 | 値 |
|---|---:|
| 統合法人 `core.companies` | 5,823,039 |
| 国税庁法人番号 | 5,006,803 |
| gBizINFO法人サマリー | 5,819,874 |
| 収録公開テーブル | 24 |
| Parquet合計 | 6,636,028,536 bytes |
| ローカルDuckDB | 28,476,452,864 bytes |
| EDINET財務ファクト | 39,059,556行 |
| 13桁形式不正 | 0 |
| 統合マスター重複法人番号 | 0 |
| refresh開始 | 2026-08-19 01:51:32 JST |
| refresh完了 | 2026-08-19 02:22:30 JST |
| Queria CLI | 0.21.0 |

### ソース別行数

| ソース | テーブル | 行数 | 法人番号キー数 | 法人マスター結合キー数 |
|---|---|---:|---:|---:|
| 国税庁 | `raw.houjin_bangou` | 5,006,803 | 5,006,803 | 5,006,803 |
| gBizINFO | `gbizinfo.company_summary` | 5,819,874 | 5,819,874 | 5,819,874 |
| gBizINFO | `gbizinfo.subsidies` | 545,877 | 131,760 | 131,760 |
| gBizINFO | `gbizinfo.procurements` | 308,613 | 23,596 | 23,596 |
| gBizINFO | `gbizinfo.patents` | 4,600,382 | 140,362 | 140,362 |
| gBizINFO | `gbizinfo.certifications` | 132,497 | 80,572 | 80,572 |
| gBizINFO | `gbizinfo.commendations` | 16,711 | 10,182 | 10,182 |
| EDINET | `edinet.business_results` | 109,990 | 4,604 | 4,604 |
| EDINET | `edinet.companies` | 11,382 | 7,236 | 7,236 |
| EDINET | `edinet.documents` | 915,816 | 7,229 | 7,229 |
| EDINET | `edinet.financial_facts` | 39,059,556 | 4,797 | 4,797 |
| EDINET | `edinet.funds` | 6,368 | — | — |
| 厚労省 | `mhlw.josei_katsuyaku_company` | 64,717 | 62,373 | 62,361 |
| 厚労省 | `mhlw.kaigo_establishment` | 223,108 | 68,700 | 68,135 |
| 厚労省 | `mhlw.shougai_establishment` | 209,452 | 51,816 | 48,593 |
| 厚労省 | `mhlw.ndb_health_checkup` | 77,280 | — | — |
| 調達ポータル | `p_portal.procurement_award` | 275,052 | 21,454 | 21,454 |
| 東京都ODS | 選定7テーブル | 45,177 | — | — |

法人番号のないファンド、NDB地域統計、東京都支援制度は、rawテーブルとして保存し法人マスターへ誤結合していない。
東京都の一部テーブルは法人番号列を持つが、空欄・独自組織コード・法人マスター未収録法人があるため、結合件数を別計測している。

## 分類・検索

- `core.company_industries`: 149,538行
- 業種コードを持つ法人: 149,358件
- JSIC大分類: A〜Tの20コードすべて
- JSIC中分類: 28コード
- 情報通信業の正式分類: 7,964件
- 情報通信業のキーワード候補: 25,632件（正式分類と別ビュー）
- `core.company_category_index`: 149,512行。法人×分類×地域の絞り込み用マテリアライズド索引

大分類・中分類・小分類の元ラベルは `core.company_industries` に階層行として保持し、分類集計は
`core.v_category_summary` で重複排除している。推測キーワードを正式JSIC分類へ混ぜていない。

## ローカル検索ベンチマーク

DuckDBネイティブ、`PRAGMA threads=4`、ウォームアップ後4回、中央値。2026-08-19実測値。

| 操作 | 件数 | p50 |
|---|---:|---:|
| 法人番号1件照会 | 1 | 5.73 ms |
| 大分類＋都道府県 | 1,000 | 199.38 ms |
| 中分類＋都道府県 | 8 | 82.80 ms |
| カテゴリ集計ビュー | 28 | 4.90 ms |
| 都道府県別リスト | 48 | 150.09 ms |
| 1万件リスト | 10,000 | 464.70 ms |
| キーワード部分一致 | 1,000 | 4,736.92 ms |
| CSV 1万件書出し | 10,000 | 461.16 ms |
| Parquet 1万件書出し | 10,000 | 606.05 ms |

カテゴリ・分類・法人番号検索は、全法人を保持したまま実用的な応答時間になっている。任意語の部分一致は5.8百万法人を
スキャンするため数秒かかる。全文検索拡張を入れる場合は、DuckDB FTS拡張の配布・更新・日本語形態素の利用条件を別途検証する必要があり、
現版では依存を増やさず再現性を優先した。

## 検証項目

- Python構文検査、標準ライブラリ `unittest` 20件
- 全24 Parquetの非空・スキーマ必須列・SHA-256記録
- 合成データによるNTA/gBizINFO和集合、重複排除、24ソース契約、候補分類隔離
- 実データの全法人番号形式・重複・A〜T分類網羅性
- EDINET財務ファクト39,059,556行の取得・DuckDB物理化
- `core.v_company_source_counts` の法人番号結合率測定
- カテゴリ索引の生成と分類検索ベンチマーク
- CSV / JSON / JSONL / Parquet出力の既存契約
- リモートSQLが `SELECT` / `WITH` のみであること
- ZIP内マニフェスト・CRC・SHA-256照合
- ソース／同梱アセットの一致、資格情報らしい値の不存在

再検証コマンド:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\verify_package.py
.\.venv\Scripts\python.exe -m queria_master --db data\queria_master.duckdb doctor
.\.venv\Scripts\python.exe -m queria_master --db data\queria_master.duckdb sql --query "SELECT * FROM meta.coverage_boundary"
.\.venv\Scripts\python.exe -m queria_master --db data\queria_master.duckdb search --industry-major G --industry-middle 39 --prefecture 東京都 --limit 100
```

## 収録しないもの・利用上の境界

- gBizINFO公式ダウンロードのうち、現行Queriaに生明細として公開されていない財務・職場カテゴリ
- EDINETの全期間・全提出書類・訂正反映済み完全履歴（Queriaの段階充填スナップショットのみ）
- e-Statの集計統計全量（法人番号で安定結合できないため別レーン）
- 全自治体の全オープンデータ（今回は東京都ODSの選定テーブルのみ）
- モデル・個人プロフィール、電話番号、非公開API由来データ
- J-PlatPatへの大量ロボットアクセス。INPIT公式案内が大量ダウンロード・ロボットアクセスを禁止しているため、画面スクレイピングは行っていない。

### 公式一次情報

- 国税庁 全件データ: https://www.houjin-bangou.nta.go.jp/download/zenken/
- 国税庁 差分データ: https://www.houjin-bangou.nta.go.jp/download/sabun/
- gBizINFO データダウンロード概要: https://help.info.gbiz.go.jp/hc/ja/articles/4795192362014-%E3%83%87%E3%83%BC%E3%82%BF%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89%E3%81%AE%E3%82%B5%E3%83%BC%E3%83%93%E3%82%B9%E6%A6%82%E8%A6%81
- EDINET API仕様: https://disclosure2dl.edinet-fsa.go.jp/guide/static/disclosure/download/ESE140206.pdf
- 調達ポータル落札実績: https://www.p-portal.go.jp/pps-web-biz/UAB02/OAB0201
- NDBオープンデータ: https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/0000177182.html
- 自治体標準オープンデータセット: https://www.digital.go.jp/resources/open_data/municipal-standard-data-set-test
- J-PlatPat利用案内: https://www.inpit.go.jp/j-platpat_info/guide/j-platpat_notice.html

詳細な出典・ライセンス表記は `reference/sources.json` と DB の `meta.source_registry` に収録している。
