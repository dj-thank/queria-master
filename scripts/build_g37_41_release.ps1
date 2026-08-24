$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
$release = Join-Path $root "releases\CompanyMaster-G37-41"

Set-Location $root
& $python scripts\build_g37_41_fuma.py
if ($LASTEXITCODE -ne 0) { throw "G37-G41 DB build failed." }

Set-Location (Join-Path $root "company_scout")
npm run tauri:build
if ($LASTEXITCODE -ne 0) { throw "Tauri EXE build failed." }

$exe = Get-ChildItem (Join-Path $root "company_scout\src-tauri\target\release") -File -Filter "CompanyMaster-G37-41.exe" | Select-Object -First 1
if (-not $exe) { throw "CompanyMaster-G37-41.exe was not produced." }
Copy-Item -LiteralPath $exe.FullName -Destination (Join-Path $release "CompanyMaster-G37-41.exe") -Force

$auditPath = Join-Path $release "audit.json"
$audit = Get-Content -LiteralPath $auditPath -Raw | ConvertFrom-Json
if (-not $audit.artifacts) { $audit | Add-Member -MemberType NoteProperty -Name artifacts -Value ([pscustomobject]@{}) }
$hash = (Get-FileHash -LiteralPath (Join-Path $release "CompanyMaster-G37-41.exe") -Algorithm SHA256).Hash.ToLowerInvariant()
$audit.artifacts | Add-Member -MemberType NoteProperty -Name "CompanyMaster-G37-41.exe" -Value ([pscustomobject]@{ bytes = (Get-Item (Join-Path $release "CompanyMaster-G37-41.exe")).Length; sha256 = $hash }) -Force
$audit | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $auditPath -Encoding UTF8

Write-Host "G37-G41 portable release: $release"
Write-Host "EXE SHA-256: $hash"
