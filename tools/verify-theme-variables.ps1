# tools\verify-theme-variables.ps1
# Robust verifier for PowerShell 7 — scans webui and compares to webui/themes/variables.css
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$repoRoot = (Get-Location).ProviderPath
$webuiRoot = Join-Path $repoRoot 'webui'
$masterPath = Join-Path $webuiRoot 'themes\variables.css'
$outDir = Join-Path $repoRoot 'tools'
$outPath = Join-Path $outDir 'theme-variable-report.json'
$skipLog = Join-Path $outDir 'verify-skip-log.txt'

if (-not (Test-Path $masterPath)) {
  Write-Error "Master variables file not found at $masterPath"
  exit 2
}

function SafeReadFile([string]$path) {
  try {
    $stream = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
      $buffer = New-Object byte[] (4096)
      $bytesRead = $stream.Read($buffer, 0, $buffer.Length)
      for ($i=0; $i -lt $bytesRead; $i++) {
        if ($buffer[$i] -eq 0) { return $null } # binary file
      }
    } finally { $stream.Close() }
    try { return Get-Content -Raw -Path $path -Encoding UTF8 -ErrorAction Stop } catch { return Get-Content -Raw -Path $path -Encoding Default -ErrorAction Stop }
  } catch {
    Add-Content -Path $skipLog -Value ("SKIP: {0} - {1}" -f $path, $_.Exception.Message)
    return $null
  }
}

# Read master variables
$masterText = SafeReadFile $masterPath
if (-not $masterText) { Write-Error "Unable to read master variables file."; exit 2 }

# Extract defined variables from master
$definedMatches = [System.Text.RegularExpressions.Regex]::Matches($masterText, '--([a-z0-9-_]+)\s*:', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
$definedSet = [System.Collections.Generic.HashSet[string]]::new()
foreach ($m in $definedMatches) { $definedSet.Add("--$($m.Groups[1].Value)") | Out-Null }

# Files to scan under webui
function Get-FilesRecursively($dir) {
  Get-ChildItem -Path $dir -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch '\\node_modules\\' -and $_.FullName -notmatch '\\.git\\' -and $_.FullName -notmatch '\\tools\\preview\\' } |
    Where-Object { @('.js','.css','.html','.ts','.jsx','.tsx') -contains $_.Extension } |
    Select-Object -ExpandProperty FullName
}

$files = Get-FilesRecursively $webuiRoot

$usedSet = [System.Collections.Generic.HashSet[string]]::new()
$skippedFiles = @()

# Use static Matches to avoid object method issues
$pattern = 'var\(\s*--([a-z0-9-_]+)\s*(?:,[^)]+)?\)'
foreach ($f in $files) {
  $txt = SafeReadFile $f
  if (-not $txt) { $skippedFiles += $f; continue }
  try {
    $matches = [System.Text.RegularExpressions.Regex]::Matches($txt, $pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    foreach ($m in $matches) {
      $usedSet.Add("--$($m.Groups[1].Value)") | Out-Null
    }
  } catch {
    Add-Content -Path $skipLog -Value ("REGEX-ERROR: {0} - {1}" -f $f, $_.Exception.Message)
    $skippedFiles += $f
  }
}

# Convert sets to arrays using pipeline (works for both .NET and PS collections)
$used = $usedSet | Sort-Object
$definedArr = $definedSet | Sort-Object

# Compute missing and unused using pipeline operations
$missing = @()
if ($used) { $missing = $used | Where-Object { -not ($definedSet.Contains($_)) } | Sort-Object }
$unused = @()
if ($definedArr) { $unused = $definedArr | Where-Object { -not ($used -contains $_) } | Sort-Object }

$report = @{
  summary = @{
    usedCount = ($used | Measure-Object).Count
    definedCount = ($definedArr | Measure-Object).Count
    missingCount = ($missing | Measure-Object).Count
    unusedCount = ($unused | Measure-Object).Count
    skippedFiles = $skippedFiles.Count
  }
  used = $used
  defined = $definedArr
  missing = $missing
  unused = $unused
  skippedFiles = $skippedFiles
}

if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
$report | ConvertTo-Json -Depth 6 | Set-Content -Path $outPath -Encoding UTF8

Write-Host "Report written to $outPath"
Write-Host "Summary: used=$($report.summary.usedCount) defined=$($report.summary.definedCount) missing=$($report.summary.missingCount) unused=$($report.summary.unusedCount) skippedFiles=$($report.summary.skippedFiles)"
if ($report.summary.missingCount -gt 0) { Write-Warning "Missing variables detected: $($report.summary.missingCount)" } else { Write-Host "No missing variables. Master file covers all usages." }
if ($skippedFiles.Count -gt 0) { Write-Host "Skipped files logged to $skipLog" }
