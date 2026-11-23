# One-command local install of the CTS DPS on a Windows laptop (PowerShell 5.1 or 7): everything it needs.
#
#   cd C:\cts-dps\dps
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup.ps1                  # install + OCR model + smoke demo
#   ... scripts\setup.ps1 -Heavy            # also the HEAVY reader: Docling in .venv-heavy and its models (about 2 GB)
#   ... scripts\setup.ps1 -Observability    # also Langfuse (traces) in Docker on this laptop, and DPS pointed at it
#   ... scripts\setup.ps1 -Offline          # no internet: everything from ..\offline-bundle (make-offline-bundle.ps1)
#   ... scripts\setup.ps1 -Tests            # also run the full test suite
#   ... scripts\setup.ps1 -Samples          # also re-download the architect's demo files (needs internet)
#   ... scripts\setup.ps1 -All              # -Heavy -Observability -Tests together
#   ... scripts\setup.ps1 -NoKeyPrompt      # do not ask for the Azure key (unattended install; AI stays off)
#
# What it does, in order:
#   1. checks Python (3.12 preferred, 3.11-3.13 accepted);
#   2. creates .venv;
#   3. installs DPS with the worker and dev extras (from the internet, or from offline-bundle\wheelhouse);
#   4. creates .env from .env.example (only if .env is missing) and asks for the Azure OpenAI key (optional);
#   5. creates the database tables (Alembic);
#   6. puts the PP-OCRv6 English OCR model where DPS looks for it, checking its SHA-256 (without it the OCR runs
#      English words together);
#   7. -Heavy: Docling in .venv-heavy and its layout/table/OCR models in %USERPROFILE%\.cache\docling\models;
#   8. -Observability: starts Langfuse (deploy\observability\start-langfuse.ps1) and writes the OTEL_* lines into .env;
#   9. runs the one-document demo on golden-set\documents\AGR-001.pdf;
#  10. prints what is installed and what is not, with the command for each missing piece.
# It never sends a key or a document anywhere, and never prints a key.

param(
    [switch]$Tests,
    [switch]$Samples,
    [switch]$Heavy,
    [switch]$Observability,
    [switch]$Offline,
    [switch]$All,
    [switch]$NoKeyPrompt,
    [string]$Bundle = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$dps = Split-Path -Parent $PSScriptRoot          # ...\cts-dps\dps
$repo = Split-Path -Parent $dps                  # ...\cts-dps
Set-Location $dps
if ($All) { $Heavy = $true; $Observability = $true; $Tests = $true }
if (-not $Bundle) { $Bundle = Join-Path $repo "offline-bundle" }

# The same model file and hash as the Dockerfile (Phase 2): RapidOCR's PP-OCRv6 small English text model.
$OcrUrl = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.0/onnx/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx"
$OcrSha = "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884"
$DoclingModels = Join-Path $env:USERPROFILE ".cache\docling\models"
$OcrTarget = Join-Path $DoclingModels "RapidOcr\onnx\PP-OCRv6\rec\PP-OCRv6_rec_small.onnx"

function Step($text) { Write-Host ""; Write-Host "=== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "SETUP FAILED: $text" -ForegroundColor Red; exit 1 }
function Warn($text) { Write-Host $text -ForegroundColor Yellow }

# A native program's stderr (pip and docling write progress there) must not stop the script under "Stop" in
# Windows PowerShell 5.1; its exit code is what counts.
function Invoke-Native([scriptblock]$block) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $block | Out-Host } finally { $ErrorActionPreference = $old }  # shown, not returned with the code
    return $LASTEXITCODE
}

if ($dps.Length -gt 80) {
    Warn ("Warning: the folder path is long ($($dps.Length) characters). Windows' 260-character path limit can " +
          "break 'pip install'. Clone into a short path such as C:\cts-dps if the install fails.")
}
if ($Offline -and -not (Test-Path $Bundle)) { Fail "-Offline: no bundle at $Bundle (make it with scripts\make-offline-bundle.ps1)" }

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

function New-Venv([string]$folder) {
    if (-not (Test-Path "$folder\Scripts\python.exe")) {
        $parts = $Python.Split(" ")
        $code = Invoke-Native { & $parts[0] $parts[1..($parts.Length - 1)] -m venv $folder }
        if ($code -ne 0) { Fail "could not create $folder" }
    }
    return (Join-Path $dps "$folder\Scripts\python.exe")
}

function Install-Dps([string]$py, [string]$extras, [string]$wheels) {
    if ($Offline) {
        $code = Invoke-Native { & $py -m pip install --no-index --find-links (Join-Path $Bundle $wheels) -e ".[$extras]" }
    } else {
        $code = Invoke-Native { & $py -m pip install --upgrade pip --quiet }
        if ($code -ne 0) { Fail "pip could not reach the package index. Behind a proxy: pip config set global.proxy http://proxy:port" }
        $code = Invoke-Native { & $py -m pip install -e ".[$extras]" }
    }
    if ($code -ne 0) { Fail "pip install .[$extras] failed (see the lines above). A long path or a proxy are the usual causes." }
}

Step "2. Virtual environment (.venv)"
$py = New-Venv ".venv"

Step "3. Packages (API, worker extras: CrewAI, OCR, PDF/Office readers, OpenTelemetry; test tools)"
Install-Dps $py "dev" "wheelhouse"

Step "4. Local settings (.env)"
if (Test-Path ".env") {
    Write-Host ".env already exists: left as it is"
} else {
    Copy-Item ".env.example" ".env"
    Write-Host ".env created from .env.example (SQLite, local storage, dev sign-in, AI off)"
}

Step "4b. Azure OpenAI key (optional: switches the Document Agent on)"
$envLines = Get-Content ".env"
$keyLine = $envLines | Where-Object { $_ -match '^AZURE_API_KEY=' } | Select-Object -First 1
if ($keyLine -and $keyLine.Trim() -ne 'AZURE_API_KEY=') {
    Write-Host "AZURE_API_KEY is already set in .env: left as it is"
} elseif ($NoKeyPrompt -or -not [Environment]::UserInteractive) {
    Write-Host "not asked (-NoKeyPrompt or no console): AI stays off. Run setup again or edit .env to add the key."
} else {
    $secure = Read-Host "Paste the Azure OpenAI key and press Enter (or just press Enter to keep AI off)" -AsSecureString
    $key = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    if ($key) {
        $envLines = $envLines | Where-Object { $_ -notmatch '^AZURE_API_KEY=' -and $_ -notmatch '^DPS_LLM_ENABLED=' }
        $envLines += "DPS_LLM_ENABLED=true"
        $envLines += "AZURE_API_KEY=$key"
        Set-Content -Path ".env" -Value $envLines -Encoding ASCII
        Write-Host "key saved in .env (this laptop only; .env is never committed); AI switched on" -ForegroundColor Green
    } else {
        Write-Host "no key: AI stays off (rules only). Run setup again or edit .env to add it later."
    }
}

Step "5. Database tables (Alembic)"
$code = Invoke-Native { & (Join-Path $dps ".venv\Scripts\alembic.exe") upgrade head }
if ($code -ne 0) { Fail "alembic upgrade head failed" }

Step "6. OCR model (PP-OCRv6, English)"
function Test-Ocr { return ((Test-Path $OcrTarget) -and ((Get-FileHash -Algorithm SHA256 $OcrTarget).Hash.ToLower() -eq $OcrSha)) }
if (Test-Ocr) {
    Write-Host "already in place: $OcrTarget"
} else {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $OcrTarget) | Out-Null
    $fromBundle = Join-Path $Bundle "models\PP-OCRv6_rec_small.onnx"
    if (Test-Path $fromBundle) {
        Copy-Item $fromBundle $OcrTarget -Force
    } elseif (-not $Offline) {
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -Uri $OcrUrl -OutFile $OcrTarget -UseBasicParsing -TimeoutSec 300
        } catch { Warn "download failed: $($_.Exception.Message)" }
    }
    if (Test-Ocr) {
        Write-Host "OCR model in place (SHA-256 checked): $OcrTarget" -ForegroundColor Green
    } else {
        if (Test-Path $OcrTarget) { Remove-Item $OcrTarget -Force }  # a wrong or partial file is never kept
        Warn ("OCR model NOT installed: scanned pages fall back to RapidOCR's own model (words run together). " +
              "If modelscope.cn is blocked, copy the file into $Bundle\models from a laptop that has it.")
    }
}

if ($Heavy) {
    Step "7. HEAVY reader: Docling in .venv-heavy and its models"
    $pyHeavy = New-Venv ".venv-heavy"
    Install-Dps $pyHeavy "dev,heavy" "wheelhouse-heavy"
    $bundleModels = Join-Path $Bundle "models\docling"
    if (Test-Path $bundleModels) {
        New-Item -ItemType Directory -Force -Path $DoclingModels | Out-Null
        Copy-Item -Path (Join-Path $bundleModels "*") -Destination $DoclingModels -Recurse -Force
        Write-Host "Docling models copied from the bundle"
    } elseif (-not $Offline) {
        $tools = Join-Path $dps ".venv-heavy\Scripts\docling-tools.exe"
        $env:HF_HUB_OFFLINE = "0"
        $code = Invoke-Native { & $tools models download layout tableformer rapidocr -o $DoclingModels }
        if ($code -ne 0) { Warn "Docling model download failed: the HEAVY reader falls back to the OCR reader" }
    }
}

if ($Observability) {
    Step "8. Observability: Langfuse (traces) on this laptop"
    $start = Join-Path $repo "deploy\observability\start-langfuse.ps1"
    $code = Invoke-Native { & powershell -NoProfile -ExecutionPolicy Bypass -File $start }
    if ($code -ne 0) { Warn "Langfuse did not start (see above). DPS works without it; run $start later." }
}

Step "9. Smoke demo: one agreement, rules only"
$agr = Join-Path $repo "golden-set\documents\AGR-001.pdf"
$code = Invoke-Native { & $py scripts\demo_onboard.py $agr }
if ($code -ne 0) { Fail "the demo did not run" }

if ($Samples) {
    Step "Demo files from the architect's list (needs internet)"
    $code = Invoke-Native { & $py scripts\fetch_public_samples.py }
}

if ($Tests) {
    Step "Full test suite (about 10 minutes, coverage must be 90% or more)"
    $code = Invoke-Native { & (Join-Path $dps ".venv\Scripts\pytest.exe") -q }
    if ($code -ne 0) { Fail "tests failed" }
}

Step "10. What is installed"
function Show($name, $ok, $hint) {
    if ($ok) { Write-Host ("  [ok]      " + $name) -ForegroundColor Green }
    else { Write-Host ("  [missing] " + $name + "  ->  " + $hint) -ForegroundColor Yellow }
}
$envText = (Get-Content ".env" -Raw)
Show "DPS packages (.venv)" (Test-Path ".venv\Scripts\python.exe") "run setup.ps1 again"
Show "OCR model PP-OCRv6" (Test-Ocr) "setup.ps1 (needs modelscope.cn) or copy into offline-bundle\models"
Show "HEAVY reader (Docling, .venv-heavy)" (Test-Path ".venv-heavy\Scripts\docling-tools.exe") "setup.ps1 -Heavy"
Show "Docling models" (Test-Path (Join-Path $DoclingModels "docling-project--docling-models")) "setup.ps1 -Heavy"
Show "Azure OpenAI key (AI on)" ($envText -match '(?m)^AZURE_API_KEY=\S+') "setup.ps1 again, paste the key"
Show "Traces to Langfuse (OTEL_* in .env)" ($envText -match '(?m)^OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=') "setup.ps1 -Observability"

Write-Host ""
Write-Host "DPS is installed." -ForegroundColor Green
Write-Host "  API:      .venv\Scripts\uvicorn --factory dps.main:create_app --port 9400   (docs at http://localhost:9400/docs)"
Write-Host "  Worker:   .venv\Scripts\python -m dps.worker          (HEAVY pool: .venv-heavy\Scripts\python -m dps.worker with DPS_WORKER_POOL=HEAVY)"
Write-Host "  Demo:     .venv\Scripts\python scripts\demo_onboard.py <file.pdf> [--trace run.txt] [--json result.json]"
Write-Host "  Traces:   http://localhost:3000 (after -Observability; guide: docs\cts-dps\observability-demo.md)"
