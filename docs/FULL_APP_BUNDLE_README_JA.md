# Queria 0.9.0 全量DB同梱アプリ版

この配布物は、検索アプリと全量データを同じフォルダへ展開して使う完全版です。`data` フォルダには統合ランタイムDB、高速検索索引、更新元DB、証拠付き拡張DBを収録しています。

## GUI

通常は次をダブルクリックして起動します。Desktop EXEにはTcl/Tk互換フックを組み込んでいます。

```powershell
.\queria-master-desktop\queria-master-desktop.exe
```

Windowsのアプリケーション制御でwindowed版が拒否される場合は、コンソール互換版を使います。

```powershell
.\queria-master-desktop-console\queria-master-desktop.exe
```

画面右上の［設定・診断］から、canonical / enrichment / runtime DB、検索索引、既定件数を確認・保存できます。DBと索引が不一致でも設定画面は開きます。`generation_id`が一致したペアだけを検索へ使用します。

## CLI常駐検索

```powershell
.\queria-master-cli\queria-master.exe `
  daemon
```

完全版の配置を自動検出するため、通常はパス指定不要です。確認は `app-health`、設定変更は `configure` を使います。

```powershell
.\queria-master-cli\queria-master.exe app-health
.\queria-master-cli\queria-master.exe configure
```

JSONLで検索要求を送ります。

```json
{"op":"search","keyword":"ソフトウェア","prefecture":"東京都","limit":1000}
```

## 収録データ

- `data\queria_runtime.duckdb`: アプリが参照する統合ランタイムDB
- `data\search.sqlite`: SQLite FTS5 trigram・カテゴリ高速索引
- `data\queria_master.duckdb`: 更新元の法人マスタ
- `data\queria_enrichment.duckdb`: 証拠付き拡張層
- `data\source_metadata.json`: 取得元・更新メタデータ

DBはアプリへ埋め込まず、展開後のファイルとして読み取り専用で使います。これにより検索時の無駄な展開を避け、DB更新時もアプリ本体を作り直さずに済みます。

全量5,823,039法人の高速検索を対象にしています。検索処理、GUI描画、CSV書き込み、初回EXE起動は別の時間として扱います。

厚労省公開データから法人番号で厳密結合した事業所情報を別スコープで収録しています。事業所電話を本社代表電話へ混ぜません。CLIの`establishment-list`で出力できます。
