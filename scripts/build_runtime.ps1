param(
    [string]$CanonicalDb = "data\queria_master.duckdb",
    [string]$EnrichmentDb = "data\queria_enrichment.duckdb",
    [string]$OutputDb = "data\queria_runtime.duckdb",
    [int]$Threads = 4,
    [string]$MemoryLimit = "8GB"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "仮想環境がありません。先に scripts\setup-windows.ps1 を実行してください。"
}

$logPath = Join-Path (Split-Path $projectRoot -Parent) "runtime-build.log"
& $python -m queria_master --db $CanonicalDb build-runtime `
    --enrichment-db $EnrichmentDb `
    --out $OutputDb `
    --threads $Threads `
    --memory-limit $MemoryLimit *>&1 | Tee-Object -FilePath $logPath
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
