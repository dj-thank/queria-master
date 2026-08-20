$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "Windows用EXE/MSIのビルドはWindowsで実行してください。" }

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $root

if (-not (Test-Path "src-tauri\resources\bin\codex.exe")) {
  & (Join-Path $PSScriptRoot "fetch-codex.ps1")
}

if (Test-Path "package-lock.json") { npm ci } else { npm install }
npm run tauri:build
if ($LASTEXITCODE -ne 0) { throw "Tauri build failed." }

Write-Host ""
Write-Host "Build complete. Installers:"
Get-ChildItem "src-tauri\target\release\bundle" -Recurse -File -Include *.exe,*.msi | ForEach-Object { Write-Host $_.FullName }
