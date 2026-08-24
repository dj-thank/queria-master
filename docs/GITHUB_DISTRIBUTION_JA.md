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
  --manifest .\queria-master-0.9.0-full-app.parts.json `
  --out .\queria-master-0.9.0-full-app.zip
```

スクリプトは各partと結合後のZIPの両方をSHA-256検証し、不一致時は完成ファイルを確定しません。

## 再分割

```powershell
python scripts\split_full_app_bundle.py `
  --input F:\QueriaReleases\queria-master-0.9.0-full-app.zip `
  --out-dir F:\QueriaReleases\queria-master-0.9.0-full-app-parts
```

GitHub Release assetは1ファイル2GiB未満に保ち、Gitリポジトリ本体へDBをコミットしません。DBを何度も更新する場合は、Releaseをバージョンごとに作成し、古いassetを削除せず履歴として保持します。

## 大分類G専用DB

大分類G専用DBは完全版71GBを再配布せず、次の小型成果物をReleaseへ個別アップロードします。

- `queria_master_g_fuma.duckdb`: 正本DB
- `queria_runtime_g_fuma.duckdb`: アプリ用読取DB
- `search_g_fuma.sqlite`: 高速検索索引
- `phone_targets_g37_41.csv`: 未取得連絡先の再開用状態
- `source_metadata.json`, `audit.json`, `README_PORTABLE_JA.md`: 出典・完全性・操作説明
- `CompanyMaster-G37-41.exe`: DBと同じフォルダで使うポータブルWindowsアプリ
- `CompanyMaster-G37-41_*_setup.exe`, `CompanyMaster-G37-41_*.msi`: Windowsインストーラー

リポジトリ本体にはバイナリDBをコミットしません。GitHub Actionsの`CompanyMaster-Windows` artifactから同じcommitのポータブルEXEとインストーラーを取得します。`scripts/publish_g_release.ps1 -InstallerDirectory <artifact展開先>`は監査JSONとのSHA-256一致、WAL不在、個人ローカルパス不在を確認してからReleaseへアップロードします。
