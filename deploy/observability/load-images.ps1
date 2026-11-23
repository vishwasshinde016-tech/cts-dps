# Loads the Langfuse images saved by save-images.ps1 (offline laptop), after checking the file's SHA-256.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\observability\load-images.ps1

# "Continue", not "Stop": Docker writes its progress to stderr, which Windows PowerShell 5.1 turns into a
# script-stopping error under "Stop". Every docker call is checked with $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$tar = Join-Path $here "images\langfuse-images.tar"
$sum = Join-Path $here "images\langfuse-images.sha256"
$docker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $docker) { $docker = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe" }
if (-not (Test-Path $tar)) { Write-Host "no ${tar}: run save-images.ps1 on a laptop with internet first" -ForegroundColor Red; exit 1 }
if (Test-Path $sum) {
    $want = ((Get-Content $sum) -split "\s+")[0]
    $have = (Get-FileHash -Algorithm SHA256 $tar).Hash
    if ($want -ne $have) { Write-Host "SHA-256 mismatch: the image file is damaged or changed" -ForegroundColor Red; exit 1 }
    Write-Host "SHA-256 ok"
}
& $docker load -i $tar
if ($LASTEXITCODE -ne 0) { Write-Host "docker load failed" -ForegroundColor Red; exit 1 }
Write-Host "images loaded" -ForegroundColor Green
