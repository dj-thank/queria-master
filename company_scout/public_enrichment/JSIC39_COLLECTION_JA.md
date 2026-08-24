# 業種コード39：公式ホームページ・電話番号収集

日本標準産業分類の中分類39「情報サービス業」を対象に、法人番号で企業を同定し、公開法人情報の公式Web URLと、企業自身が管理する公式ページ上の電話番号候補を収集する手順です。

## 基本原則

- 法人番号を企業の結合キーにする
- 企業名だけの一致を自動採用しない
- 公開法人情報に登録されたWeb URLを起点にする
- 公式サイトと同一ホスト内だけを巡回する
- `robots.txt`、アクセス間隔、ページ数、受信サイズ上限を守る
- localhost、プライベートIP、リンクローカル、予約アドレスを拒否する
- 電話番号とともに種別候補、根拠URL、周辺テキスト、抽出方法、信頼度、取得日時を保存する
- FAX、採用、サポート、広報・IR、個人情報相談、支店番号を代表電話と混同しない
- 見つからなかった企業も処理済みとして記録し、再開位置を管理する
- 対象manifestを処理完了の証拠にせず、1社ごとのappend-only `progress.jsonl`だけを完了証拠にする

## 2つの収集経路

### A. Queria公開データだけで現在の業種39を再構築する

`.github/workflows/jsic39-contact-collection.yml` を使用します。公開法人データ上で中分類39が明示された企業を再構築し、公式URLのある企業を従業員数・資本金順に優先して8シャードで巡回します。

この経路は完全に公開データだけで再現できますが、公開法人データの業種ラベル収録率に依存します。業種39の全企業を保証するものではありません。

### B. 手元の任意企業リストを全法人番号マスタへ照合する

`.github/workflows/public-corporate-number-index.yml` で、公開法人データから全法人のストリーム照合用インデックスを生成します。成果物には企業名、所在地、法人番号、公開URL、代表者、従業員数、資本金等を含みます。

企業リスト自体はGitHubへアップロードせず、ダウンロードした公開インデックスと手元CSVをローカルで照合します。

```powershell
python corporate_index_matcher.py `
  --targets companies.csv `
  --public-index corporate_number_index.tsv.zst `
  --output output\corporate_matches.csv `
  --review-output output\corporate_match_review.csv `
  --summary output\corporate_match_summary.json
```

自動採用は、正規化した企業名と所在地が完全一致し、同スコア候補が1件だけの場合に限定します。所在地の前方一致は既定でレビュー対象です。`--accept-prefix` を明示した場合だけ、単一の前方一致候補を採用できます。

## GitHub Actionsのバッチ設定

手動実行時は次の入力を変更できます。

- `start_offset`: 優先順位付き対象リストの開始位置
- `batch_size`: 1シャード当たりの企業数
- `max_pages`: 1社当たりに巡回する同一ホスト内ページ数

既定値は1シャード100社、8シャード合計800社です。次のバッチへ進む場合は、前回の `start_offset + 8 × batch_size` を新しい `start_offset` にします。

## 電話番号候補

1社につき最大5候補を保持します。各候補は次のいずれかへ分類します。

- `代表電話`
- `本社電話`
- `問い合わせ電話`
- `採用窓口`
- `サポート窓口`
- `広報・IR窓口`
- `個人情報・相談窓口`
- `支店・事業所`
- `FAX`
- `未分類`

画面に表示された用途付き電話番号を、ラベルのない隠れた `tel:` リンクより優先します。同じ番号が複数ページにある場合は、代表・本社等の明示、表示テキスト、固定電話、信頼度を比較して最良の証拠を残します。

## 成果物

`jsic39-contact-batch` アーティファクトに次を出力します。

- `jsic39_public_contacts.csv`
- `jsic39_collection_summary.json`
- `export_summary.json`

統合CSVには最有力候補に加え、候補件数と `電話候補一覧JSON` を保存します。

| 状態 | 意味 |
| --- | --- |
| `phone_candidate_found` | 公式サイト内で1件以上の電話番号候補と根拠を取得 |
| `fax_only` | FAX候補だけを取得。通話可能な電話番号の成功件数には含めない |
| `processed_no_phone` | 規定ページを確認したが電話番号候補なし |
| `website_pending` | 公式URLあり、まだ今回のバッチでは未処理 |
| `website_missing` | 公開法人データ上で公式URLを確認できない |
| `blocked_by_policy` | URL安全性またはrobots.txtで取得を拒否 |
| `needs_review` | robots.txt取得不能やページ取得失敗のため、未取得と断定せず要確認 |

`manifest.csv`は処理予定、`progress.jsonl`は実際に完了した企業、`phones.csv`は候補の表示用CSVです。途中停止後は同じ`progress.jsonl`を指定して再実行すると完了企業をスキップします。merge時もmanifestだけでは`processed_no_phone`へ昇格しません。

一時的な取得失敗だけを再試行する場合は、成功済みを消さずに `--retry-state needs_review` を追加します。robots拒否を無断で再試行したり、全進捗を消したりしません。

## ローカル実行：公開業種39経路

Queria Masterの公開法人DBを構築し、業種39をCSVへ出力します。

```powershell
.\.venv\Scripts\python.exe -m queria_master refresh --scope info-communications --no-cache
.\.venv\Scripts\python.exe -m queria_master --db data\queria_master.duckdb search `
  --industry-middle 39 `
  --limit 100000 `
  --out collection\jsic39_all.csv
.\.venv\Scripts\python.exe -m queria_master --db data\queria_master.duckdb search `
  --industry-middle 39 `
  --has-web `
  --limit 100000 `
  --out collection\jsic39_with_web.csv
```

対象シャードを作成します。

```powershell
cd company_scout\public_enrichment
python jsic39_collection.py prepare-shard `
  --companies ..\..\collection\jsic39_with_web.csv `
  --db work\shard-0\targets.sqlite3 `
  --manifest work\shard-0\manifest.csv `
  --summary work\shard-0\prepare_summary.json `
  --offset 0 `
  --limit 100
```

公式サイト内の電話番号候補を収集します。

```powershell
python official_site_phone_enricher.py `
  --db work\shard-0\targets.sqlite3 `
  --output work\shard-0\phones.csv `
  --progress work\shard-0\progress.jsonl `
  --summary work\shard-0\collection_summary.json `
  --max-pages 4 `
  --max-candidates 5 `
  --sleep 0.75 `
  --timeout 20
```

複数シャードを統合します。

```powershell
python jsic39_collection.py merge `
  --all-companies ..\..\collection\jsic39_all.csv `
  --manifest "work\shard-*\manifest.csv" `
  --phones "work\shard-*\phones.csv" `
  --progress "work\shard-*\progress.jsonl" `
  --output output\jsic39_public_contacts.csv `
  --summary output\jsic39_collection_summary.json
```

## 確定前レビュー

電話番号候補は、根拠URLと周辺テキストを確認した後に用途を確定します。特にFAX、採用、製品サポート、営業問い合わせ、広報・IR、個人情報相談、支店・事業所、フリーダイヤル、ナビダイヤル、IP電話は代表電話から分離します。

確定済みデータを再利用する場合は、`reference/verified_public_contacts.csv` と `import_verified_contacts.py` の形式に揃えます。
