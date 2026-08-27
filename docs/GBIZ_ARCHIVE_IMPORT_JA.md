# 履歴gBizINFO Hojinjoho ZIP importer

## 目的と境界

`import-gbiz-archive` は、過去のgBizINFO Hojinjoho活動情報ZIPを検証し、監査用の新規DuckDBへ正規化する任意機能です。通常の `refresh`、canonical DB、enrichment DB、runtime、検索索引から完全に分離されています。

- canonical DBを参照・更新しない
- runtimeや検索索引へ自動反映しない
- stagingからcanonicalへの昇格を行わない
- gBizINFO Basic CSVや全法人母集団を取り込まない
- 入力にないJSIC中分類37〜41を推定しない

## CLI

```powershell
.\.venv\Scripts\python.exe -m queria_master import-gbiz-archive `
  --archive work\Hojinjoho.zip `
  --staging-db work\hojinjoho-history.duckdb `
  --target-industry G `
  --batch-size 1000
```

| 引数 | 必須 | 契約 |
|---|---|---|
| `--archive` | はい | top-level JSON arrayを含むHojinjoho ZIP |
| `--staging-db` | はい | 未存在の `.duckdb` / `.ddb`。既存ファイルとsymlinkは拒否 |
| `--target-industry` | いいえ | filter値A〜T。既定は`G`。`ALL`は業種欠損を含む全valid record |
| `--batch-size` | いいえ | 1〜100,000法人。既定は1,000 |

グローバル `--db` を指定してもcanonical DBとして使用しません。`--force`、`--replace`、`--promote` はありません。

成功時は次のJSONを標準出力へ返します。

```json
{
  "staging_database": "/absolute/path/hojinjoho-history.duckdb",
  "import_id": "UUID",
  "source_sha256": "SHA-256",
  "source_records": 0,
  "imported_records": 0,
  "activity_records": 0,
  "json_member_count": 0,
  "json_uncompressed_bytes": 0
}
```

## Python API

```python
from queria_master.gbiz_archive import (
    ArchiveValidationError,
    ArchiveLimits,
    GBizArchiveError,
    ImportResult,
    StagingDatabaseError,
    import_archive_to_staging,
    iter_normalized_batches,
)

result = import_archive_to_staging(
    archive_path,
    staging_database,
    batch_size=1000,
    target_industry="G",
    limits=ArchiveLimits(),
)

for batch in iter_normalized_batches(
    archive_path,
    batch_size=1000,
    target_industry="G",
    limits=ArchiveLimits(),
):
    ...
```

`iter_normalized_batches` は書き込みを行いません。最初のbatchを返す前に、全memberのmetadata preflightと全JSON member payloadの読み取り検証を1回行います。2回目のJSON読み取りを最後まで消費した時点で、入力ファイルが途中変更されていないことも確認します。

`import_archive_to_staging` は `ImportResult` を返します。CLIのJSONと同じ7項目を属性として持ち、`staging_database`だけは `Path` です。入力ZIP・JSON・recordの不正は `ArchiveValidationError`、出力先・公開条件の不正は `StagingDatabaseError`、その他のimport失敗は基底 `GBizArchiveError` で通知します。`ValueError` はAPI引数・上限設定自体の不正です。

## 入力検証と上限

既定値は次のとおりです。CLIから上限を緩和する機能は公開していません。

| 項目 | 既定上限 |
|---|---:|
| ZIPファイル | 1 GiB |
| ZIP member数 | 256 |
| JSON member数 | 128 |
| 1 memberの展開サイズ | 512 MiB |
| 展開サイズ合計 | 8 GiB |
| 圧縮比 | 100倍 |
| top-level arrayの1 item | 4 Mi文字 |
| 1法人の活動数 | 100,000 |
| 1正規化record | 16 MiB |
| 1batchの正規化データ | 64 MiB |

ZIP slipとなるパス、絶対パス、重複パス、symlink、暗号化member、未対応圧縮を全memberのmetadata preflightで拒否します。JSON memberは実際にopen/readし、CRC、member SHA-256、UTF-8、top-level array、重複JSON key、非標準数値、13桁の法人番号、`industry`が文字列配列またはnullであること、活動構造を検証します。非JSON memberはpayloadを展開しないため、そのCRCやpayload hashは検証・保存しません。入力の`industry`文字列自体がJSIC大分類A〜Tであることは検証せず、そのまま保持します。A〜T制約は`--target-industry`のfilter値だけに適用します。

## staging schema

| table | 内容 |
|---|---|
| `gbiz_archive.import_runs` | 入力パス・ZIP SHA-256・件数・業種条件・上限・importer version |
| `gbiz_archive.archive_members` | member名・圧縮/展開サイズ・member SHA-256・件数 |
| `gbiz_archive.companies` | 法人基本値・入力industry文字列配列・正規化JSON・record SHA-256 |
| `gbiz_archive.activities` | 活動種別・source key・正規化JSON・SHA-256 |

書き込みはhidden `.<staging-name>.<uuid>.building` DBのtransaction内で行います。対象JSON memberの全読み取りとcommit後、同じfilesystem上のhard linkを使って未存在の出力名へatomic no-clobber公開します。hard linkを利用できないfilesystemでは安全側に失敗し、final staging名は公開しません。失敗時はbuilding DBとWALの削除を試みますが、OSや他プロセスが保持している場合の削除までは保証しません。

staging DBが保存する取得元情報は、入力のローカル絶対パス、ファイル名、bytes、SHA-256です。ダウンロードURL、取得日時、提供条件はZIPから安全に導出できないため自動記録しません。運用者は実際に使用した配布ページURL、取得日時、利用条件の確認結果を別の監査記録へ残し、このstagingをQueria経由canonicalソースの証跡と混同しないでください。

## 検証状況

合成ZIPによる自動テストでは、正規化、出典ハッシュ、業種filter、batch上限、ZIP slip、symlink、ZIP bomb、活動増幅、不正JSON、既存DB保護、同時作成競合、失敗時cleanupを確認しています。

別途復元した companion 監査記録は、元アーカイブの値としてSHA-256 `4617dd5ae7f31f8e1a6201295d95516e21b84e06ef37c949145119b58f520a4e`、379,025,154 bytes、45 JSON member、展開5,786,410,982 bytes、447,900法人、`G` 7,948法人を主張しています。元ZIP本体は取得できなかったため、これらは現 importer が実測した値でも、こちらで全byteを検証した結果でもありません。元ZIPを再取得できた時点で、SHA-256を照合してstaging importと件数・member hashを再検証する必要があります。
