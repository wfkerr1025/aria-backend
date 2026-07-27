# tools\reconcile-theme-variables.ps1
Set-StrictMode -Version Latest
$repoRoot = (Get-Location).ProviderPath
$webuiRoot = Join-Path $repoRoot 'webui'
$reportPath = Join-Path $repoRoot 'tools\theme-variable-report.json'
$masterPath = Join-Path $webuiRoot 'themes\variables.css'
$previewDir = Join-Path $repoRoot 'tools\preview'
$previewMaster = Join-Path $previewDir 'variables.css'
$patchPath = Join-Path $repoRoot 'tools\theme-variable-patch.diff'

if (-not (Test-Path $reportPath)) { Write-Error "Run verifier first: $reportPath not found"; exit 2 }
if (-not (Test-Path $masterPath)) { Write-Error "Master variables file not found at $masterPath"; exit 2 }

$report = Get-Content -Raw -Path $reportPath | ConvertFrom-Json
$missing = $report.missing

if ($missing.Count -eq 0) { Write-Host "No missing variables. Nothing to reconcile."; exit 0 }

$masterText = Get-Content -Raw -Path $masterPath
$defined = [System.Text.RegularExpressions.Regex]::Matches($masterText, '--([a-z0-9-_]+)\s*:', 'IgnoreCase') |
           ForEach-Object { "--$($_.Groups[1].Value)" } | Sort-Object -Unique

$aliases = @()
foreach ($m in $missing) {
  if ($defined -contains $m) { continue }
  $aliases += "$m: transparent; /* AUTO-ADDED: reconcile missing variable */"
}

if (-not (Test-Path $previewDir)) { New-Item -ItemType Directory -Path $previewDir | Out-Null }
$previewContent = $masterText + "`n`n/* AUTO-ADDED ALIASES - reconcile missing variables */`n" + ($aliases -join "`n") + "`n"
Set-Content -Path $previewMaster -Value $previewContent -Encoding UTF8

if (Get-Command git -ErrorAction SilentlyContinue) {
  $tmpOrig = Join-Path $previewDir 'variables.orig.css'
  Set-Content -Path $tmpOrig -Value $masterText -Encoding UTF8
  $diff = git --no-pager diff --no-index -- $tmpOrig $previewMaster 2>$null
  if ($diff) { Set-Content -Path $patchPath -Value $diff -Encoding UTF8; Write-Host "Patch created at $patchPath" }
  else { Write-Warning "No diff produced." }
} else {
  Set-Content -Path $patchPath -Value "Preview created at $previewMaster. Git not found to create unified diff." -Encoding UTF8
  Write-Host "Git not found. Preview created at $previewMaster. Manual review required."
}

Write-Host "Preview files in $previewDir"
Write-Host "Review preview and patch before applying. Apply with: git apply tools/theme-variable-patch.diff"
