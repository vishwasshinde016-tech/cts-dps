# Starts the whole DPS on this laptop, after setup.ps1: the API and both worker pools, in the background.
#
#   cd C:\cts-dps\dps
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\start-dps.ps1            # API + STANDARD + HEAVY workers
#   ... scripts\start-dps.ps1 -NoHeavy        # without the HEAVY worker (scans and long documents then wait)
#   ... scripts\start-dps.ps1 -Port 9500      # another API port
#
# What it starts (each a hidden process; output in dps\logs\):
#   API              .venv python -m uvicorn --factory dps.main:create_app  (http://127.0.0.1:<Port>)
#   STANDARD worker  .venv python -m dps.worker  (DPS_WORKER_THREADS jobs at once, default 3)
#   HEAVY worker     .venv-heavy python -m dps.worker with DPS_WORKER_POOL=HEAVY (one Docling job at a time)
# Then it waits for /health/live and prints where things are. Stop them with scripts\stop-dps.ps1.
# DPS requires Azure OpenAI: AZURE_API_KEY must be set in .env or in the environment (setup.ps1 asks for it).
param(
    [switch]$NoHeavy,
    [int]$Port = 9400
)
$ErrorActionPreference = "Stop"
$dps = Split-Path -Parent $PSScriptRoot
Set-Location $dps
$logs = Join-Path $dps "logs"
$pidFile = Join-Path $logs "dps.pids"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Fail($text) { Write-Host "START FAILED: $text" -ForegroundColor Red; exit 1 }

$py = Join-Path $dps ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { Fail "no .venv: run scripts\setup.ps1 first" }
if (-not (Test-Path ".env")) { Fail "no .env: run scripts\setup.ps1 first" }
$keyInEnvFile = Select-String -Path ".env" -Pattern '^AZURE_API_KEY=\S' -Quiet
if (-not $keyInEnvFile -and -not $env:AZURE_API_KEY) {
    Fail ("AZURE_API_KEY is not set (.env or environment). DPS requires Azure OpenAI. " +
          "Run scripts\setup.ps1 again to enter it, or set it in .env.")
}
if (Test-Path $pidFile) {
    $alive = Get-Content $pidFile | Where-Object { $_ -match '^\d+' } |
        ForEach-Object { Get-Process -Id ([int]($_ -split ' ')[0]) -ErrorAction SilentlyContinue }
    if ($alive) { Fail "DPS is already running (see $pidFile). Run scripts\stop-dps.ps1 first." }
}
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) { Fail "port $Port is in use. Stop that program or use -Port." }

function Start-Part([string]$name, [string]$python, [string[]]$arguments, [hashtable]$envVars) {
    $saved = @{}
    foreach ($k in $envVars.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); Set-Item "env:$k" $envVars[$k] }
    try {
        $p = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $dps -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logs "$name.log") -RedirectStandardError (Join-Path $logs "$name.err.log") -PassThru
    } finally {
        foreach ($k in $envVars.Keys) {
            if ($null -eq $saved[$k]) { Remove-Item "env:$k" -ErrorAction SilentlyContinue } else { Set-Item "env:$k" $saved[$k] }
        }
    }
    Add-Content -Path $pidFile -Value "$($p.Id) $name"
    Write-Host ("  started {0,-16} pid {1}" -f $name, $p.Id)
}

Set-Content -Path $pidFile -Value "# DPS processes started by start-dps.ps1 (pid name)"
Write-Host "Starting DPS from $dps"
Start-Part "api" $py @("-m", "uvicorn", "--factory", "dps.main:create_app", "--host", "127.0.0.1", "--port", "$Port") @{}
Start-Part "worker-standard" $py @("-m", "dps.worker") @{ DPS_WORKER_POOL = "STANDARD"; DPS_WORKER_ID = "local-standard" }
$pyHeavy = Join-Path $dps ".venv-heavy\Scripts\python.exe"
if (-not $NoHeavy) {
    if (Test-Path $pyHeavy) {
        Start-Part "worker-heavy" $pyHeavy @("-m", "dps.worker") @{ DPS_WORKER_POOL = "HEAVY"; DPS_WORKER_ID = "local-heavy" }
    } else {
        Write-Host "  HEAVY worker not started: no .venv-heavy (run scripts\setup.ps1 -Full). Scans that need it will wait." -ForegroundColor Yellow
    }
}

Write-Host "Waiting for the API (first start loads the AI libraries, up to 2 minutes)..."
$url = "http://127.0.0.1:$Port"
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri "$url/health/live" -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { }
    $apiPid = [int]((Get-Content $pidFile | Where-Object { $_ -match ' api$' }) -split ' ')[0]
    if (-not (Get-Process -Id $apiPid -ErrorAction SilentlyContinue)) { break }
}
if (-not $ok) {
    Write-Host "The API did not come up. Last lines of logs\api.err.log:" -ForegroundColor Red
    Get-Content (Join-Path $logs "api.err.log") -Tail 25 -ErrorAction SilentlyContinue
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "stop-dps.ps1") | Out-Null
    exit 1
}
Start-Sleep -Seconds 3
$dead = Get-Content $pidFile | Where-Object { $_ -match '^\d+' } | Where-Object {
    -not (Get-Process -Id ([int]($_ -split ' ')[0]) -ErrorAction SilentlyContinue) }
foreach ($d in $dead) { Write-Host "  stopped early: $d (see logs\$((($d -split ' ')[1])).err.log)" -ForegroundColor Yellow }

Write-Host ""
Write-Host "DPS is running." -ForegroundColor Green
Write-Host "  API        $url   (docs: $url/docs, health: $url/health)"
Write-Host "  Logs       $logs"
Write-Host "  Try a job  .venv\Scripts\python scripts\submit_job.py ..\golden-set\documents\AGR-001.pdf"
Write-Host "  Stop       powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-dps.ps1"
