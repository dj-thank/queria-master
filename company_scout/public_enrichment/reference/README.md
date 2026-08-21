# Verified Public Contacts Reference

`verified_public_contacts.csv` は、企業自身が管理する公式ページを根拠として、電話番号と関連情報を整形した公開安全版データです。

## 収録方針

- 各電話番号に HTTPS の公式根拠 URL を保持します。
- 代表電話、本社電話、番号案内、サービス・広報・個人情報など用途限定窓口を区別します。
- 用途限定番号を代表電話として扱いません。
- 企業名だけでは自動照合しません。
- 証券コード＋企業名、または企業名＋所在地が一意の場合だけ採用します。
- 特定の入力データベースに依存する ID、URL、API 情報、件数は含みません。

## ローカル DB への反映

先に任意の企業 CSV を準備します。

```bash
python public_data_enricher.py prepare companies.csv --replace
```

参照連絡先を反映します。

```bash
python import_verified_contacts.py \
  --db output/company_public_data.sqlite3 \
  --contacts reference/verified_public_contacts.csv \
  --replace-source \
  --output output/csv/verified_contacts_reflected.csv
```

照合の優先順位は次のとおりです。

1. 呼び出し側が明示した `SOURCE_ID`
2. 証券コード＋正規化企業名が一意
3. 正規化企業名＋正規化所在地が一意

曖昧な行は自動採用せず、`verified_contact_import_audit` テーブルへ `review` として記録されます。

## 保存先

- `site_contacts`: 電話番号、種別、用途、根拠 URL、信頼度、確認日など
- `verified_contact_import_audit`: 各行の照合方法、採用・要確認・不正の状態
- `verified_contacts_reflected.csv`: ローカル `SOURCE_ID` と公開連絡先の反映結果

公式ページの表示は変更される可能性があります。実利用前に各行の `根拠URL` と各サイトの利用条件を再確認してください。
