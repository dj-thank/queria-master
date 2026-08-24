# FUMA 検索 URL の読み方

この資料は URL の意味を正規化するだけです。FUMA の画面スクレイピング、非公開 API の解析、検索結果の自動取得は行いません。

例:

```text
https://fumadata.com/search?area_id[]=&chu_code[]=37,38,39,40,41&core_keyword=&listed=
```

URL パーサーがそのまま読む値は、概ね次です。

```json
{
  "path": "/search",
  "query": {
    "area_id[]": [""],
    "chu_code[]": ["37,38,39,40,41"],
    "core_keyword": [""],
    "listed": [""]
  }
}
```

アプリケーション側ではカンマ区切りを配列へ正規化します。

```json
{
  "path": "/search",
  "filters": {
    "area_id": [],
    "industry": {
      "major_code": "G",
      "middle_codes": ["37", "38", "39", "40", "41"]
    },
    "keyword": null,
    "listed": null
  }
}
```

業種コードの階層は次のように扱います。

```json
{
  "dai_code": "G",
  "chu_code": "39",
  "syou_code": "391",
  "sai_code": "3913"
}
```

本プロジェクトの正規母集団は FUMA の URL ではなく、gBizINFO の `business_items` に格納された日本標準産業分類コードです。FUMA/FDS の正規 CSV を別途取得した場合だけ、法人番号をキーに追加結合します。
