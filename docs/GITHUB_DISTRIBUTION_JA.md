# GitHub公開・全量アプリ配布方式

## 公開構成

`dj-thank/queria-master` はソース、SQL、検索コード、ビルドスクリプト、検証資料を公開するリポジトリです。巨大DBをGit履歴へ入れず、GitHub Releaseへ配布物を置きます。

完全版ZIPはGitHub Release assetの上限に合わせ、1,800MiB単位へ分割します。Releaseには次を置きます。

- ソースZIP
- GUI windowed / console fallback ZIP
- CLI onedir ZIPとonefile EXE
- `*.parts.json`（全体サイズ、全体SHA-256、各partのSHA-256）
- 全量アプリZIPのpartファイル群

利用者はpartを同じフォルダへ保存し、次で結合します。

```powershell
python scripts\join_full_app_bundle.py `
  --manifest .\queria-master-0.8.0-full-app.parts.json `
  --out .\queria-master-0.8.0-full-app.zip
```

スクリプトは各partと結合後のZIPの両方をSHA-256検証し、不一致時は完成ファイルを確定しません。

## 再分割

```powershell
python scripts\split_full_app_bundle.py `
  --input F:\QueriaReleases\queria-master-0.8.0-full-app.zip `
  --out-dir F:\QueriaReleases\queria-master-0.8.0-full-app-parts
```

GitHub Release assetは1ファイル2GiB未満に保ち、Gitリポジトリ本体へDBをコミットしません。DBを何度も更新する場合は、Releaseをバージョンごとに作成し、古いassetを削除せず履歴として保持します。
