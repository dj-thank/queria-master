# dj-thank/queria-master ブランチ監査（2026-08-24）

## 結論

GitHubの全通常ブランチ12本、タグ3本、PR参照を取得した。現在の正本は`main@b16fb642f95bd36cd0efde2ee2b5167ea79d034b`。`feat/jsic39-contact-collection`と`feat/jsic39-contact-campaign`はmainと最終ツリーが完全一致し、JSIC39の公式HP/電話収集はmain統合済みである。

未統合の重要系統は2つある。

- PR #5 `feat/public-enrichment-desktop`: 汎用公開情報補完をデスクトップ操作へ接続。open。
- PR #21 `codex/ui-search-workbench-20260821`: 検索UI、100件ページング、FTS5経路、セキュリティ強化。draft/open。

どちらもmainの後続ハードニングとローカルG拡張から分岐しているため、ブランチ全体をそのままマージしない。機能単位で取り込み、Python/Rust/Reactの現行契約で再試験する。

## 参照一覧

| ブランチ | 先端 | 判定 |
| --- | --- | --- |
| `main` | `b16fb642` | 正本。PR #8まで統合済み |
| `feat/jsic39-contact-campaign` | `b16fb642` | mainと同一 |
| `feat/jsic39-contact-collection` | `54883164` | 履歴は21 commitだが最終ツリーはmainと同一。PR #8 merged |
| `codex/ui-search-workbench-20260821` | `113e4527` | PR #21 draft/open。main比17ファイル、+5,206/-842 |
| `feat/public-enrichment-desktop` | `88611be7` | PR #5 open。固定コマンドのPython補完ブリッジ |
| `data/jsic39-release-20260821-batch0000` | `593d635f` | PR #9 closed/unmerged。分割ZIP・証拠・SHA-256公開処理は再利用候補 |
| `docs/readme-current-20260821` | `944395aa` | PR #7 merged済みの作業履歴。mainへ再マージ不要 |
| `feat/contact-dataset-packaging` | `d8e8c6cc` | PR #4の統合点。main履歴内 |
| `feat/verified-public-contacts-20260821` | `63ce3cc8` | PR #4 merged |
| `feature/public-company-enrichment` | `4a6ed189` | PR #2 merged |
| `hardening/public-enrichment-v1.1` | `2a12609e` | PR #3/#6 merged |
| `release/v0.9.0` | `73f9c010` | PR #1 merged。現在は過去リリース系統 |

## タグ

| タグ | SHA | 用途 |
| --- | --- | --- |
| `v0.8.0` | `2670c03a` | 旧高速EXE/全量配布 |
| `v0.9.0` | `a71dd840` | 設定・runtime/index世代・拡張層 |
| `jsic39-public-contacts-20260821-batch0000` | `d6d09101` | JSIC39公開連絡先の収集バッチ開始点 |

## PR状態

| PR | 状態 | ブランチ | 概要 |
| ---: | --- | --- | --- |
| #1 | merged | `release/v0.9.0` | 0.9.0 |
| #2 | merged | `feature/public-company-enrichment` | 公開企業補完 |
| #3 | merged | `hardening/public-enrichment-v1.1` | 補完ハードニング |
| #4 | merged | `feat/verified-public-contacts-20260821` | 検証済み連絡先 |
| #5 | open | `feat/public-enrichment-desktop` | デスクトップ連携 |
| #6 | merged | `hardening/public-enrichment-v1.1` | 追加ハードニング |
| #7 | merged | `docs/readme-current-20260821` | README更新 |
| #8 | merged | `feat/jsic39-contact-collection` | JSIC39 HP/電話収集 |
| #9 | closed/unmerged | `data/jsic39-release-20260821-batch0000` | 収集データ公開 |
| #21 | draft/open | `codex/ui-search-workbench-20260821` | 検索ワークベンチ |

## G情報DBへの取込方針

1. `main`をソース正本に維持する。
2. PR #5からは「公開補完の進捗・入出力UI」を、現行スキーマに合わせて取り込む。
3. PR #21からは100件ページング、FTS5経路、検索フォールバック表示、電話収集のSSRF強化を優先する。
4. PR #9の分割パッケージ・マニフェスト・SHA-256を、G全体の収集バッチ配布に一般化する。
5. 未統合ブランチをそのまま取り込まず、G版の法人番号回復・出典・重複契約を保持する。

GitHub: [dj-thank/queria-master](https://github.com/dj-thank/queria-master)
