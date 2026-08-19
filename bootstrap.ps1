param(
    [ValidateSet("all-public", "info-communications", "gbizinfo-companies", "all-corporations")]
    [string]$Scope = "all-public"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Invoke-Python {
    param([string[]]$Arguments)
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 @Arguments
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python @Arguments
    } else {
        throw "Python 3.10 or newer was not found. Install Python and try again."
    }
    if ($LASTEXITCODE -ne 0) { throw "Python command failed. exit=$LASTEXITCODE" }
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[1/4] Creating the Python virtual environment"
    Invoke-Python -Arguments @("-m", "venv", ".venv")
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
Write-Host "[2/4] Installing or updating Queria CLI and DuckDB"
& $Python -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "pip update failed." }
& $Python -m pip install --upgrade -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

$env:QUERIA_NO_TELEMETRY = "1"
if ((Test-Path "data\queria_master.duckdb") -and (Test-Path "cache\all-public-latest")) {
    Write-Host "[3/4] Using the bundled full dataset (no re-download)"
} else {
    Write-Host "[3/4] Downloading public Queria data and building DuckDB"
    & $Python -m queria_master refresh --scope $Scope
    if ($LASTEXITCODE -ne 0) { throw "Data build failed." }
}

Write-Host "[4/4] Validating the local database"
& $Python -m queria_master doctor
if ($LASTEXITCODE -ne 0) { throw "Validation failed." }

Write-Host ""
Write-Host "Ready: data\queria_master.duckdb" -ForegroundColor Green
Write-Host ".\.venv\Scripts\python.exe -m queria_master search --keyword AI --limit 100"
