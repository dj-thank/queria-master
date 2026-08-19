param(
    [string]$Database = "data\queria_runtime.duckdb",
    [string]$Output = "data\search.sqlite",
    [int]$BatchSize = 20000
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "仮想環境がありません。先に scripts\setup-windows.ps1 を実行してください。"
}
$logPath = Join-Path (Split-Path $projectRoot -Parent) "search-index-build.log"
& $python -m queria_master --db $Database build-search-index `
    --out $Output `
    --batch-size $BatchSize *>&1 | Tee-Object -FilePath $logPath
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
