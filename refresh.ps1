param(
    [ValidateSet("all-public", "info-communications", "gbizinfo-companies", "all-corporations")]
    [string]$Scope = "info-communications"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Run bootstrap.ps1 or 01_初回セットアップ.bat first."
}
$env:QUERIA_NO_TELEMETRY = "1"
& $Python -m queria_master refresh --scope $Scope
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m queria_master init-enrichment
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $Python -m queria_master publish-runtime
exit $LASTEXITCODE
