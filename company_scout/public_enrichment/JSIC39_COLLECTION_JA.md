# 業種コード39：公式ホームページ・電話番号収集

日本標準産業分類の中分類39「情報サービス業」を対象に、公開法人データに登録された公式Web URLと、企業自身が管理する公式ページ上の電話番号候補を収集する実行手順です。

## 基本原則

- 法人番号を企業の結合キーにする
- 公開法人データに登録されたWeb URLを起点にする
- 公式サイトと同一ホスト内だけを巡回する
- `robots.txt`、アクセス間隔、ページ数、受信サイズ上限を守る
- localhost、プライベートIP、リンクローカル、予約アドレスを拒否する
- 電話番号とともに根拠URL、周辺テキスト、信頼度、取得日時を保存する
- FAX、採用窓口、サポート窓口などを無条件に代表電話と確定しない
- 見つからなかった企業も処理済みとして記録し、再開位置を管理する

## GitHub Actions

`.github/workflows/jsic39-contact-collection.yml` を使用します。

Pull Requestでは、公開法人データから業種39の企業一覧を再構築した後、公式URLを持つ企業を従業員数・資本金の順に優先し、8シャードで並列収集します。既定値は1シャード100社、合計800社です。

手動実行時は次の入力を変更できます。

- `start_offset`: 優先順位付き対象リストの開始位置
- `batch_size`: 1シャード当たりの企業数
- `max_pages`: 1社当たりに巡回する同一ホスト内ページ数

次のバッチへ進む場合は、前回の `start_offset + 8 × batch_size` を新しい `start_offset` にします。

## 成果物

`jsic39-contact-batch` アーティファクトに次を出力します。

- `jsic39_public_contacts.csv`
- `jsic39_collection_summary.json`
- `export_summary.json`

CSVの主な状態は次のとおりです。

| 状態 | 意味 |
| --- | --- |
| `phone_candidate_found` | 公式サイト内で電話番号候補と根拠を取得 |
| `processed_no_phone` | 規定ページを確認したが電話番号候補なし |
| `website_pending` | 公式URLあり、まだ今回のバッチでは未処理 |
| `website_missing` | 公開法人データ上で公式URLを確認できない |

## ローカル実行

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
  --companies ..\..\..\collection\jsic39_with_web.csv `
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
  --max-pages 4 `
  --sleep 0.75 `
  --timeout 20
```

複数シャードを統合します。

```powershell
python jsic39_collection.py merge `
  --all-companies ..\..\..\collection\jsic39_all.csv `
  --manifest "work\shard-*\manifest.csv" `
  --phones "work\shard-*\phones.csv" `
  --output output\jsic39_public_contacts.csv `
  --summary output\jsic39_collection_summary.json
```

## 確定前レビュー

電話番号候補は、根拠URLと周辺テキストを確認した後に用途を確定します。特に次は代表電話から分離します。

- FAX
- 採用・応募窓口
- 製品サポート
- 営業問い合わせ
- 広報・IR
- 個人情報相談
- 支店・事業所
- フリーダイヤル、ナビダイヤル、IP電話

確定済みデータを再利用する場合は、`reference/verified_public_contacts.csv` と `import_verified_contacts.py` の形式に揃えます。
