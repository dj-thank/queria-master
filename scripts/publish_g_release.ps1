param(
  [string]$Version = "0.10.1",
  [string]$Repository = "dj-thank/queria-master",
  [string]$ReleaseDirectory = "releases/CompanyMaster-G37-41",
  [Parameter(Mandatory = $true)]
  [string]$InstallerDirectory,
  [string]$Target = "main"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$release = (Resolve-Path (Join-Path $root $ReleaseDirectory)).Path
$auditPath = Join-Path $release "audit.json"
$audit = Get-Content -Raw -LiteralPath $auditPath | ConvertFrom-Json
$tag = "v$Version"
$releaseNoteCode = $Version.Replace(".", "")
$releaseNotes = Join-Path $root "docs\RELEASE_G_V${releaseNoteCode}_JA.md"

if ($audit.version -ne $Version) {
  throw "audit.version=$($audit.version) does not match requested version $Version"
}

$required = @(
  (Join-Path $release "data\queria_master_g_fuma.duckdb"),
  (Join-Path $release "data\queria_runtime_g_fuma.duckdb"),
  (Join-Path $release "data\search_g_fuma.sqlite"),
  (Join-Path $release "data\phone_targets_g37_41.csv"),
  (Join-Path $release "data\source_metadata.json"),
  (Join-Path $release "CompanyMaster-G37-41.exe"),
  (Join-Path $release "README_PORTABLE_JA.md"),
  $auditPath
)

foreach ($path in $required) {
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Missing release artifact: $path"
  }
  if (Test-Path -LiteralPath "$path.wal") {
    throw "DuckDB WAL must not be published: $path.wal"
  }
}

$installerRoot = (Resolve-Path (Join-Path $root $InstallerDirectory)).Path
$installers = @(Get-ChildItem -LiteralPath $installerRoot -Recurse -File | Where-Object {
  $_.Extension -in @(".exe", ".msi") -and $_.Name -ne "CompanyMaster-G37-41.exe"
} | Select-Object -ExpandProperty FullName)
if ($installers.Count -eq 0) {
  throw "No Windows installer (.exe or .msi) found in $installerRoot"
}

foreach ($property in $audit.artifacts.PSObject.Properties) {
  $name = $property.Name
  $candidate = if ($name -eq "CompanyMaster-G37-41.exe") {
    Join-Path $release $name
  } elseif ($name -in @("audit.json", "README_PORTABLE_JA.md")) {
    Join-Path $release $name
  } else {
    Join-Path (Join-Path $release "data") $name
  }
  if (Test-Path -LiteralPath $candidate) {
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $candidate).Hash.ToLowerInvariant()
    if ($actual -ne $property.Value.sha256) {
      throw "SHA-256 mismatch: $name"
    }
  }
}

$publicMetadata = (Get-Content -Raw -LiteralPath $auditPath) + (Get-Content -Raw -LiteralPath (Join-Path $release "data\source_metadata.json"))
if ($publicMetadata -match '(?i)[a-z]:\\users\\|/users/') {
  throw "Personal local path found in public metadata"
}

gh auth status --hostname github.com | Out-Null
$existing = gh release view $tag --repo $Repository --json tagName 2>$null
if (-not $existing) {
  if (-not (Test-Path -LiteralPath $releaseNotes -PathType Leaf)) {
    throw "Missing release notes: $releaseNotes"
  }
  gh release create $tag --repo $Repository --target $Target --title "CompanyMaster 大分類G $tag" --notes-file $releaseNotes
}
$assets = @($required) + @($installers)
gh release upload $tag --repo $Repository --clobber @assets
gh release view $tag --repo $Repository --json url,tagName,assets
