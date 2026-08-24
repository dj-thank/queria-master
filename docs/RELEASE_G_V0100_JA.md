# CompanyMaster 大分類G v0.10.0

日本標準産業分類の大分類G（情報通信業、中分類37〜41）専用の検索DBとWindowsアプリです。

## 主な変更

- FUMA 56,353行と全国法人マスタを統合
- 名称と本店住所が双方で一意に完全一致する場合だけ法人番号を回復
- GitHub v0.9.0完全版に収録済みの厚生労働省公開事業所データを法人番号で再利用
- 事業所電話を`phone_type=establishment`として保存し、本社代表電話と明確に分離
- 事業所HP候補を`enrichment.website_candidates`へ証拠URL付きで保存
- 監査JSONとメタデータからローカルPCの絶対パスを除去
- Python、Tauri、npmのバージョンを0.10.0へ統一
- 100件単位の正確なページングと、SQLite FTS5からDuckDBへの安全なフォールバックを備えた最新検索ワークベンチを統合
- FUMA ID、業種の中・小・細分類、電話種別・出典を企業詳細とCSVへ追加
- 公式サイトからの電話確認にDNS pinning、private/reserved IP拒否、redirect再検証、応答サイズ制限を適用
- ポータブルEXE、NSISセットアップ、MSIを同じmain系列から配布

## 収録件数

- 統合企業: 59,581件
- 法人番号付き: 52,490件
- 電話付き: 459件
- 公式HP付き: 4,380件
- 電話候補: 927件
- 事業所HP候補: 427件
- JSIC分類: 71件

## 配布物

- `CompanyMaster-G37-41.exe`: DBと同じフォルダで使うポータブル版
- `CompanyMaster-G37-41_0.10.0_x64-setup.exe`: NSISセットアップ
- `CompanyMaster-G37-41_0.10.0_x64_en-US.msi`: MSIインストーラー
- `queria_master_g_fuma.duckdb`, `queria_runtime_g_fuma.duckdb`, `search_g_fuma.sqlite`: 正本・アプリ用DB・検索索引

## データ境界

`core.g_companies.website`は企業公式HPを優先します。事業所URLを企業公式HPへ自動昇格しません。電話も公開事業所の連絡先であり、本社代表電話とは断定しません。全候補は出典、取得時点、連絡先種別とともに保持します。

FUMA入力は利用者提供データです。再配布時は入力元に適用される契約・利用条件を確認してください。コードはMIT License、行政データには各提供元の利用条件が適用されます。
