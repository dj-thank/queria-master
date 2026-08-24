# dj-thank/queria-master ブランチ監査（2026-08-24）

## 結論

統合対象として存在していたGitHubの全通常ブランチ12本、ローカルで取得済みのタグ3本、PR参照を確認した。作業中にmainが4回進んだため再取得し、統合基準を`main@78b55497ca9a3977d09df7db6699f6e8004a1d4c`へ更新した。このmainはPR #22〜#25を含み、G版v0.10.0、検索ワークベンチ、portable Windows artifact修正、最終リリースノートまで統合済みである。mainは未保護でrequired status checkも設定されていないため、本統合ではPR CIを実質的な公開ゲートとして扱う。

mainにブランチ単位では未統合の重要系統は2つある。

- PR #5 `feat/public-enrichment-desktop`: 汎用公開情報補完をデスクトップ操作へ接続。open。
- PR #9 `data/jsic39-release-20260821-batch0000`: 分割ZIP、manifest、SHA-256を含む公開データpackaging。closed/unmerged。

旧PR #21はclosed/unmergedだが、その検索ワークベンチはG版へ合わせたPR #23としてmainに統合済みである。PR #5/#9はmainの後続ハードニングとG拡張より前から分岐しているため、ブランチ全体をそのままマージしない。固定command境界、補完UI、再現可能packagingだけを現行v0.10.0へ機能単位で移植し、Python/Rust/Reactの現行契約で再試験する。

## 参照一覧

| ブランチ | 先端 | 判定 |
| --- | --- | --- |
| `main` | `78b55497` | 正本。PR #22〜#25まで統合済み |
| `feat/jsic39-contact-campaign` | `b16fb642` | 旧main時点。現在のmainより4 commit前 |
| `feat/jsic39-contact-collection` | `54883164` | PR #8 merged済みの履歴branch。現在のmainへ再マージ不要 |
| `codex/ui-search-workbench-20260821` | `113e4527` | PR #21 closed/unmerged。PR #23がG版としてsupersedeしてmainへ統合 |
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
| #21 | draft/closed/unmerged | `codex/ui-search-workbench-20260821` | 検索ワークベンチ。#23がsupersede |
| #22 | merged | `codex/g-information-db-v0.10.0` | G情報DB v0.10.0。merge `ab55bb09` |
| #23 | merged | `codex/integrate-search-workbench-v0.10.0` | G版へ検索workbench統合。merge `dacc66ff` |
| #24 | merged | `codex/fix-portable-artifact-path` | portable Windows artifact修正。merge `aa39b7da` |
| #25 | merged | `codex/update-v010-release-notes` | v0.10.0 release note。merge `78b55497` |

## 関連issue

| issue | state | この統合での扱い |
|---:|---|---|
| #10 Public enrichment→canonical runtime/index | open | staging bridge、evidence DB、review、generation一致publish経路を実装。Windows実配布smoke後にclose判断 |
| #15 Rust/React/Python PR CI | open | Python/package、frontend、Rust fmt/testのPR CIを追加。mainのbranch protectionは未設定 |
| #18 DuckDB外部read遮断 | open | 公開SQLのwrite keyword契約は維持。DuckDB全external read surfaceの網羅は別途必要 |
| #19 website network/robots security | open | DNS全回答検査、verified IP pinning、redirect再検証、HTTPS downgrade拒否、body/content-type上限を実装。live network/robots検証は残る |

## G情報DBへの取込方針

1. `main@78b55497`をソース正本に維持し、G版v0.10.0と#23のworkbenchを保持する。
2. PR #5からは固定process境界、公開補完の進捗・入出力UIを、現行canonical→enrichment→runtime/index経路に接続する。
3. PR #21の独自安全性変更は、#23/mainと重複を確認したうえでDNS pinning、redirect再検証、bounded responseなど不足分だけを採用する。
4. PR #9からは再現可能なfile list、manifest、SHA-256、archive path検証を一般release packagingへ移植する。公開済みdata artifact自体をrepositoryへ再取込しない。
5. 未統合branchをそのまま取り込まず、G/FUMAの法人番号回復・出典・重複契約を保持する。
6. 履歴Hojinjoho ZIP由来機能はcanonicalへ直結せず、別のopt-in staging importerとしてのみ移植する。

GitHub: [dj-thank/queria-master](https://github.com/dj-thank/queria-master)
