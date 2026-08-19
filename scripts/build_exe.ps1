param(
    [string]$Out = "dist",
    [ValidateSet("cli", "desktop")]
    [string]$Mode = "cli",
    [ValidateSet("onefile", "onedir")]
    [string]$Bundle = "onefile",
    [switch]$Console
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    throw "先に bootstrap.ps1 を実行して .venv を作成してください。"
}
& $Python -m pip install --upgrade -r requirements-exe.txt
if ($LASTEXITCODE -ne 0) { throw "EXE build dependency installation failed." }
$BuildArgs = @("scripts\build_exe.py", "--out", $Out, "--mode", $Mode, "--bundle", $Bundle)
if ($Console) { $BuildArgs += "--console" }
& $Python @BuildArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
