# Queria 高速検索EXE 配布手順

## 速度優先の推奨構成

画面付きアプリは PyInstaller の `onedir` 版を使います。EXEを起動したままSQLite FTS5索引を保持するため、検索のたびにPythonやDuckDBを起動し直しません。

配布先は次のどちらかの配置にしてください。

```text
release-root/
  data/
    queria_runtime.duckdb
    search.sqlite
  queria-master-desktop/
    queria-master-desktop.exe
    (PyInstallerの依存DLL・データ)
```

または、`data/` を `queria-master-desktop/` の中へ置く構成も自動検出します。検出できない場合は、データを置いた親ディレクトリを環境変数で指定できます。

```powershell
$env:QUERIA_MASTER_HOME = 'D:\Queria\release-root'
.\queria-master-desktop\queria-master-desktop.exe
```

全量データをEXEへ埋め込まないのは、5.8百万法人のDBを更新するたびに実行ファイルを再配布しないためです。DBと索引は同じ更新単位で差し替え、EXEは固定したまま使えます。

## 起動

`queria-master-desktop.exe` を起動し、キーワード、地域、JSIC大・中分類、法人種別、従業員数、資本金、URL有無、件数を入力します。件数の上限は画面では1,000件です。Enterで検索し、結果をダブルクリックするとURLを開きます。表示中の結果はCSVへ書き出せます。［設定・診断］では現在のDB、索引、refresh ID、generation ID、機能可否を確認できます。

## 実測値

全量5,823,039法人、SQLite索引約2.4GB、キーワード `ソフトウェア`、ウォームアップ後5回の常駐CLI実測では、0.8.0基準onedirビルドの1,000件検索・JSONL往復はp50 13.023msでした。修正版EXEの再起動はこのホストのアプリケーション制御ポリシーでブロックされたため、現行ソースdaemonの再計測14.398msと区別しています。これは検索取得とIPCの値です。EXE初回起動、GUIの行描画、CSV書き込みは別の処理なので、同じ0.5秒という目標へ混ぜずに計測します。

単一ファイルが必要な場合は `queria-master.exe` の `onefile` 版を使えます。ただしPyInstallerの仕様上、onefileは起動時展開があるため、速度優先の通常利用はonedir版にしてください。

## CLI常駐プロトコル

画面を使わず別アプリから利用する場合はCLI daemonを起動します。

```powershell
.\queria-master.exe --db data\queria_runtime.duckdb daemon --search-index data\search.sqlite
```

標準入力へ1行1JSONを送り、標準出力から1行1JSONを読みます。

```json
{"op":"search","keyword":"ソフトウェア","prefecture":"東京都","limit":1000}
```

返却値は `columns` と値配列です。列名を各行へ重複させないため、1,000行でもIPCのJSONサイズを抑えます。終了時は `{"op":"shutdown"}` を送ります。
