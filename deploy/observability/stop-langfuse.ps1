# Stops Langfuse on this laptop. The traces stay in the Docker volumes for the next start.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\observability\stop-langfuse.ps1
#   ... -Purge     also delete every stored trace and the Langfuse databases (asks first)

param([switch]$Purge)

# "Continue", not "Stop": Docker writes its progress to stderr, which Windows PowerShell 5.1 turns into a
# script-stopping error under "Stop". Every docker call is checked with $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$compose = Join-Path $here "docker-compose.langfuse.yml"
$envFile = Join-Path $here ".env"
$docker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $docker) { $docker = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe" }
if (-not (Test-Path $envFile)) { Write-Host "no deploy\observability\.env: Langfuse was never started here"; exit 0 }

if ($Purge) {
    $answer = Read-Host "Delete ALL Langfuse traces and databases on this laptop? Type DELETE to confirm"
    if ($answer -ne "DELETE") { Write-Host "nothing deleted"; exit 0 }
    & $docker compose -f $compose --env-file $envFile down -v
    Write-Host "Langfuse stopped and its data deleted. deploy\observability\.env is kept (delete it for new secrets)."
} else {
    & $docker compose -f $compose --env-file $envFile down
    Write-Host "Langfuse stopped; traces kept for the next start."
}
