param(
  [string]$Version = "0.146.0"
)

$ErrorActionPreference = "Stop"
if ($env:OS -ne "Windows_NT") { throw "このスクリプトはWindowsで実行してください。" }
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { throw "npmが見つかりません。Node.js LTSをインストールしてください。" }

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$targetDir = Join-Path $root "src-tauri\resources\bin"
$target = Join-Path $targetDir "codex.exe"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("companyscout-codex-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

try {
  Write-Host "[CompanyScout] Fetching @openai/codex@$Version ..."
  npm install --prefix $tmp "@openai/codex@$Version" --no-save --no-audit --no-fund
  if ($LASTEXITCODE -ne 0) { throw "Codex package installation failed." }

  $candidates = @(Get-ChildItem -Path $tmp -Filter "codex.exe" -File -Recurse -ErrorAction SilentlyContinue)
  if ($candidates.Count -eq 0) { throw "codex.exe was not found in the installed package." }

  $arch = $env:PROCESSOR_ARCHITECTURE
  if ($arch -eq "ARM64") {
    $preferred = @($candidates | Where-Object { $_.FullName -match "aarch64|arm64" })
  } else {
    $preferred = @($candidates | Where-Object { $_.FullName -match "x86_64|x64" })
  }
  $source = if ($preferred.Count -gt 0) { $preferred[0] } else { $candidates[0] }
  Copy-Item -Force $source.FullName $target
  & $target --version
  if ($LASTEXITCODE -ne 0) { throw "Bundled codex.exe did not start correctly." }
  Write-Host "[CompanyScout] Bundled Codex: $target"
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
