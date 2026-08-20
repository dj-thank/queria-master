$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "このスクリプトはWindowsで実行してください。" }

function Ensure-Command([string]$Name, [scriptblock]$Installer) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    & $Installer
  }
}

Ensure-Command "node" {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "Node.js LTSをインストールしてください。" }
  winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
}

Ensure-Command "rustup" {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "Rustupをインストールしてください。" }
  winget install -e --id Rustlang.Rustup --accept-package-agreements --accept-source-agreements
  $env:Path += ";$HOME\.cargo\bin"
}

rustup default stable

# Tauri Windows builds require the MSVC C++ toolchain.
if (Get-Command winget -ErrorAction SilentlyContinue) {
  $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
  if (-not (Test-Path $vswhere)) {
    Write-Host "[CompanyScout] Installing Visual Studio Build Tools (C++ workload) ..."
    winget install -e --id Microsoft.VisualStudio.2022.BuildTools --accept-package-agreements --accept-source-agreements --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
  }
}

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root
npm install
& (Join-Path $PSScriptRoot "fetch-codex.ps1")

Write-Host ""
Write-Host "Setup complete. Next: .\scripts\build-windows.ps1"
