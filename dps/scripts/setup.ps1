# One-command local install of the CTS DPS on a Windows laptop (PowerShell 5.1 or 7).
#
#   cd C:\cts-dps\dps
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1            # install + smoke demo
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -Tests     # also run the full test suite
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1 -Samples   # also download the demo files
#
# What it does, in order: checks Python (3.12 preferred, 3.11-3.13 accepted), creates .venv, installs the package with
# the worker and dev extras, creates .env from .env.example (only if .env is missing), creates the SQLite tables with
# Alembic, and runs the one-document demo on golden-set/documents/AGR-001.pdf. It never writes keys anywhere.

param(
    [switch]$Tests,
    [switch]$Samples,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$dps = Split-Path -Parent $PSScriptRoot          # ...\cts-dps\dps
Set-Location $dps

function Step($text) { Write-Host ""; Write-Host "=== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "SETUP FAILED: $text" -ForegroundColor Red; exit 1 }

if ($dps.Length -gt 80) {
    Write-Host "Warning: the folder path is long ($($dps.Length) characters). Windows' 260-character path limit can" `
        "break 'pip install'. Clone into a short path such as C:\cts-dps if the install fails." -ForegroundColor Yellow
}

Step "1. Python"
if (-not $Python) {
    foreach ($candidate in @("py -3.12", "py -3.13", "py -3.11", "python")) {
        $parts = $candidate.Split(" ")
        try {
            $v = & $parts[0] $parts[1..($parts.Length - 1)] -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        } catch { $v = $null }
        if ($v -and @("3.11", "3.12", "3.13") -contains "$v".Trim()) { $Python = $candidate; break }
    }
}
if (-not $Python) { Fail "Python 3.11, 3.12 or 3.13 not found. Install Python 3.12 from python.org (tick 'Add to PATH')." }
Write-Host "using: $Python"

Step "2. Virtual environment (.venv)"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $parts = $Python.Split(" ")
    & $parts[0] $parts[1..($parts.Length - 1)] -m venv .venv
    if ($LASTEXITCODE -ne 0) { Fail "could not create .venv" }
}
$py = Join-Path $dps ".venv\Scripts\python.exe"
& $py -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip could not reach the package index. Behind a proxy: pip config set global.proxy http://proxy:port" }

Step "3. Packages (API, worker extras: CrewAI, OCR, PDF/Office readers; test tools)"
& $py -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { Fail "pip install failed (see the lines above). A long path or a proxy are the usual causes." }

Step "4. Local settings (.env)"
if (Test-Path ".env") {
    Write-Host ".env already exists: left as it is"
} else {
    Copy-Item ".env.example" ".env"
    Write-Host ".env created from .env.example (SQLite, local storage, dev sign-in, AI off)"
}

Step "5. Database tables (Alembic)"
& (Join-Path $dps ".venv\Scripts\alembic.exe") upgrade head
if ($LASTEXITCODE -ne 0) { Fail "alembic upgrade head failed" }

Step "6. Smoke demo: one agreement, rules only"
$agr = Join-Path (Split-Path -Parent $dps) "golden-set\documents\AGR-001.pdf"
& $py scripts\demo_onboard.py $agr
if ($LASTEXITCODE -ne 0) { Fail "the demo did not run" }

if ($Samples) {
    Step "7. Demo files from the architect's list (needs internet)"
    & $py scripts\fetch_public_samples.py
}

if ($Tests) {
    Step "8. Full test suite (about 10 minutes, coverage must be 90% or more)"
    & (Join-Path $dps ".venv\Scripts\pytest.exe") -q
    if ($LASTEXITCODE -ne 0) { Fail "tests failed" }
}

Write-Host ""
Write-Host "DPS is installed." -ForegroundColor Green
Write-Host "  API:    .venv\Scripts\uvicorn --factory dps.main:create_app --port 9400   (docs at http://localhost:9400/docs)"
Write-Host "  Worker: .venv\Scripts\python -m dps.worker"
Write-Host "  Demo:   .venv\Scripts\python scripts\demo_onboard.py <file.pdf> [--trace run.txt] [--json result.json]"
