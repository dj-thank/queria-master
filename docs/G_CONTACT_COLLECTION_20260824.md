# 大分類G 既知公式HPの電話候補収集 receipt（2026-08-24）

## 境界

大分類G（中分類37〜41）のうち、既存の公開・利用者提供データで公式HPを確認済み、かつ電話が未収集だった企業だけを対象にした。検索エンジンによるHP発見、有料API、認証、非公開データ取得、候補の代表電話への自動昇格は行っていない。企業別の入力・候補・progressはGitへ追加していない。

機械可読の集計、run ID、ローカル証拠bytesのサイズ・SHA-256、10,000行の状態内訳、再現性の限界は [`G_CONTACT_COLLECTION_20260824.json`](G_CONTACT_COLLECTION_20260824.json) に固定した。秘密・企業別データをGitへ出さずに、保持中のローカル証拠と照合できるreceiptである。

各ホスト内は逐次処理し、robots.txt、HTTP(S)限定、public IPへのDNS固定、proxy拒否、同一ホスト、リダイレクト、最大ページ数、受信サイズ、タイムアウト、0.75秒間隔を適用した。対象manifestは予定、append-only progress JSONLだけを処理完了証拠とした。

## 1,000社 pilot

| 指標 | 値 |
| --- | ---: |
| 対象・完了 | 1,000 / 1,000社 |
| 取得HTMLページ | 3,203 |
| 非FAXの通話候補あり | 396社（39.6%） |
| FAXのみ | 133社 |
| ページ確認済み・電話候補なし | 350社 |
| 要確認 | 103社 |
| policy/安全性で明示ブロック | 18社 |
| 候補総行 | 1,633 |
| 高信頼の非FAX候補行 | 636 |
| 有料API費用 | 0 |
| 誤同定率 | 未測定（人手判定前のcandidate） |

## 既知公式HPの全件結果

| 指標 | 値 |
| --- | ---: |
| 対象・完了 | 3,930 / 3,930社 |
| 取得HTMLページ | 11,620 |
| 非FAXの通話候補あり | 1,726社（43.9%） |
| FAXのみ | 532社 |
| ページ確認済み・電話候補なし | 1,215社 |
| 要確認 | 354社 |
| policy/安全性で明示ブロック | 103社 |
| 候補総行 | 5,843 |
| 高信頼の非FAX候補行 | 2,086 |
| 同一ホスト違反 | 0 |
| 重複会社×電話 | 0 |
| 番号形式不一致 | 0 |

ローカル候補CSVのSHA-256は `7b07ba11580234a9f2451c715e1a59422646a77bb63b743b7d967f6bb7afac77`。候補種別は代表、本社、問い合わせ、採用、サポート、広報・IR、個人情報・相談、支店・事業所、未分類、FAXを分離している。

## 判定

`LOCAL_PASS`。既知公式HPに対する収集経路、再開、選択再試行、FAX分離、fail-closed merge、集計・検証は通過した。誤同定率は人手サンプルレビュー前なので未到達。HP欠損企業への公式HP候補発見は、利用条件・検索予算・候補レビューを別契約として実装する。

## GitHub Actionsでの継続

`.github/workflows/g-contact-collection.yml` は手動実行専用で、公開済みReleaseの `phone_targets_g37_41.csv` と同じRelease generationのruntime DBを読み、会社名・従業員数・資本金を復元して8シャードへ分ける。既定は1シャード125社、合計1,000社。

公開済みv0.10.0アセットの現行smokeでは59,581行を59,581行すべてruntime DBへ照合でき、generation `g-v0.10.0-fuma-c3c570cd5a5d`、scope `G37-G41`、JSIC大分類`G`を各ターゲットとmanifestへ束縛した。電話未収集の既知公式HPは4,159社だった。本receiptのローカル再生成スナップショット3,930社とは入力generationが異なるため、件数を混ぜず、各runのRelease tagとmanifestで固定する。

前回runを継続する場合は `prior_run_id` を指定する。各シャードのprior manifestが現在manifestとbyte一致した場合だけprogress JSONLを復元し、不一致、欠落、非数値inputは開始前にfail closedする。新しいmerge経路ではprogressが必須であり、manifestだけを処理済み証拠にする旧挙動は明示的なlegacy flagへ隔離した。

このrepositoryはpublicなので、workflowは32文字以上のrepository secret `G_CONTACT_ARTIFACT_KEY` を必須にし、job間・run間で保持するtarget、manifest、progress、候補、merge結果をAES-256-CBC/PBKDF2で暗号化してからActions artifactへ置く。平文artifactはuploadしない。artifactは90日保持であり、鍵を変更すると既存runから再開できない。ReleaseやGit履歴へ候補データを自動公開しない。
