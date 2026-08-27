# CompanyMaster 大分類G v0.10.1

日本標準産業分類の大分類G（情報通信業、中分類37〜41）専用版の統合・安全性更新です。アプリ、公開情報補完、GitHub CI、配布手順を同じ版へ揃えます。

## 主な変更

- v0.10.0の検索ワークベンチと59,581社のG版データを維持
- 公式サイト発見、法人同一性の検証、連絡先抽出、レビュー、runtime/index公開を別段階として実装
- 公開情報補完をWindowsアプリから固定コマンド境界で実行可能にし、進捗と結果を表示
- enrichment writer lock、lease token、dataset generation一致を要求し、古いworkerや別世代データの混入を拒否
- DNS全回答検査、IP pinning、redirect再検証、HTTPS downgrade拒否、応答上限を公式サイト取得へ適用
- 履歴Hojinjoho ZIPはcanonicalへ直結せず、上限付き・opt-inのstaging importerとして追加
- Python/package、React、Rust、manifestをPull Requestごとに検証するCIを追加
- G37〜41の既知公式HPを8 shardで再開可能に収集し、成果物を暗号化。電話候補は人手審査まで確定値へ昇格しない
- Python、Tauri、npmのバージョンを0.10.1へ統一

## データ収録件数

v0.10.1では検証済みcanonicalデータを再利用し、データ世代 `g-v0.10.0-fuma-c3c570cd5a5d` を維持します。未審査のクロール候補を混ぜないため、件数はv0.10.0と同じです。

- 統合企業: 59,581件
- 法人番号付き: 52,490件
- 電話付き: 459件
- 公式HP付き: 4,380件
- 電話候補: 927件
- 事業所HP候補: 427件
- JSIC分類: 71件

## 配布物

- `CompanyMaster-G37-41.exe`: ポータブル版
- `CompanyMaster-G37-41_0.10.1_x64-setup.exe`: NSISセットアップ
- `CompanyMaster-G37-41_0.10.1_x64_en-US.msi`: MSIインストーラー
- `queria_master_g_fuma.duckdb`, `queria_runtime_g_fuma.duckdb`, `search_g_fuma.sqlite`: 正本・アプリ用DB・検索索引
- `phone_targets_g37_41.csv`: 同一データ世代に固定された電話レビュー対象台帳
- `audit.json`, `source_metadata.json`: 件数、世代、出典、SHA-256

## 公開判定

- Python: 106 passed、1 skipped
- 公開情報補完: 49 passed
- Rust: 24 passed
- React/Vite production build: 成功
- GitHub Actions上のWindowsビルド、成果物ハッシュ、アプリ版表示を最終ゲートとする

## データ境界とコスト

行政公開データと公式サイトを優先し、有料APIは必須にしません。LLMは低信頼候補の選別支援に限定でき、DBの確定値へ自動昇格しません。電話は公開事業所連絡先または候補であり、本社代表番号と断定しません。FUMA入力は利用者提供データのため、再配布時は適用される契約・利用条件を確認してください。

## ロールバック

runtime/indexのgeneration不一致、アプリがDBを開けない、主要検索・補完操作の失敗、またはRelease assetのSHA-256不一致があればv0.10.1を公開せず、既存Latestのv0.10.0を継続します。公開後の問題ではv0.10.0をLatestへ戻し、v0.10.1資産を調査対象として保持します。
