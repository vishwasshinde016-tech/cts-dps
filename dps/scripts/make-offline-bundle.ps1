# Builds ..\offline-bundle for a laptop without internet, or where PyPI, modelscope.cn or Docker Hub are blocked
# (the office laptop). Run on a laptop with internet and the SAME Python version (3.12) as the target laptop;
# copy the whole project with offline-bundle to the target; there run: scripts\setup.ps1 -Offline [-Heavy] [-Observability]
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make-offline-bundle.ps1             # DPS + OCR model + Langfuse images
#   ... make-offline-bundle.ps1 -Heavy      # also Docling (torch: about 2-3 GB of wheels) and its models
#   ... make-offline-bundle.ps1 -NoImages   # without the Langfuse images (no Docker on the target)
#
# offline-bundle\
#   wheelhouse\          every Python package DPS needs (pip download), for this Python version and Windows
#   wheelhouse-heavy\    with -Heavy: the same plus Docling
#   models\PP-OCRv6_rec_small.onnx     the OCR model (SHA-256 checked)
#   models\docling\      with -Heavy: Docling's layout, table and OCR models
#   ..\deploy\observability\images\langfuse-images.tar   the Langfuse images (save-images.ps1)
#   MANIFEST.txt         what is in it, with sizes and the Python version it was made for

param(
    [switch]$Heavy,
    [switch]$NoImages,
    [string]$Bundle = ""
)

$ErrorActionPreference = "Continue"  # pip and docker write progress to stderr; exit codes are checked
$dps = Split-Path -Parent $PSScriptRoot
$repo = Split-Path -Parent $dps
if (-not $Bundle) { $Bundle = Join-Path $repo "offline-bundle" }
$py = Join-Path $dps ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Write-Host "run scripts\setup.ps1 first (it creates .venv)" -ForegroundColor Red; exit 1 }
Set-Location $dps

function Step($text) { Write-Host ""; Write-Host "=== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "BUNDLE FAILED: $text" -ForegroundColor Red; exit 1 }

New-Item -ItemType Directory -Force -Path (Join-Path $Bundle "models") | Out-Null

Step "1. Python packages (wheelhouse)"
& $py -m pip download -d (Join-Path $Bundle "wheelhouse") ".[dev]" pip setuptools wheel
if ($LASTEXITCODE -ne 0) { Fail "pip download failed" }

if ($Heavy) {
    Step "1b. Python packages with Docling (wheelhouse-heavy)"
    & $py -m pip download -d (Join-Path $Bundle "wheelhouse-heavy") ".[dev,heavy]" pip setuptools wheel
    if ($LASTEXITCODE -ne 0) { Fail "pip download (heavy) failed" }
}

Step "2. OCR model"
$ocr = Join-Path $env:USERPROFILE ".cache\docling\models\RapidOcr\onnx\PP-OCRv6\rec\PP-OCRv6_rec_small.onnx"
if (-not (Test-Path $ocr)) { Fail "the OCR model is not on this laptop: run scripts\setup.ps1 first" }
$sha = (Get-FileHash -Algorithm SHA256 $ocr).Hash.ToLower()
if ($sha -ne "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884") { Fail "the OCR model on this laptop has the wrong SHA-256" }
Copy-Item $ocr (Join-Path $Bundle "models\PP-OCRv6_rec_small.onnx") -Force

if ($Heavy) {
    Step "2b. Docling models"
    $docling = Join-Path $env:USERPROFILE ".cache\docling\models"
    if (-not (Test-Path (Join-Path $docling "docling-project--docling-models"))) { Fail "Docling models missing: run scripts\setup.ps1 -Heavy first" }
    $target = Join-Path $Bundle "models\docling"
    New-Item -ItemType Directory -Force -Path $target | Out-Null
    Copy-Item -Path (Join-Path $docling "*") -Destination $target -Recurse -Force
}

if (-not $NoImages) {
    Step "3. Langfuse images"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repo "deploy\observability\save-images.ps1")
    if ($LASTEXITCODE -ne 0) { Fail "saving the Langfuse images failed (is Docker running?)" }
}

Step "4. Manifest"
$pyVersion = & $py -c "import sys, platform; print(sys.version.split()[0], platform.machine())"
$lines = @("CTS DPS offline bundle, made $(Get-Date -Format s) for Python $pyVersion on Windows.",
           "Install on the target: scripts\setup.ps1 -Offline" + $(if ($Heavy) { " -Heavy" } else { "" }) + " [-Observability]", "")
foreach ($item in Get-ChildItem $Bundle) {
    $size = (Get-ChildItem $item.FullName -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
    $lines += ("{0,-22} {1,8:N0} MB" -f $item.Name, ($size / 1MB))
}
Set-Content -Path (Join-Path $Bundle "MANIFEST.txt") -Value $lines -Encoding ASCII
$lines | ForEach-Object { Write-Host $_ }
Write-Host ""
Write-Host "Bundle ready: $Bundle" -ForegroundColor Green
