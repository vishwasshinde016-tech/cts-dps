# Saves the Langfuse images into one file for a laptop without internet or with Docker Hub blocked (office laptop).
# Run on a laptop with internet; copy deploy\observability\images\langfuse-images.tar with the project. On the other
# laptop start-langfuse.ps1 loads it by itself (or run load-images.ps1).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File deploy\observability\save-images.ps1

# "Continue", not "Stop": Docker writes its progress to stderr, which Windows PowerShell 5.1 turns into a
# script-stopping error under "Stop". Every docker call is checked with $LASTEXITCODE instead.
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$compose = Join-Path $here "docker-compose.langfuse.yml"
$out = Join-Path $here "images"
$docker = (Get-Command docker -ErrorAction SilentlyContinue).Source
if (-not $docker) { $docker = Join-Path $env:LOCALAPPDATA "Programs\DockerDesktop\resources\bin\docker.exe" }

# The image list from the compose file itself, so the bundle always matches it. Dummy secrets: only the names matter.
$dummy = @{ POSTGRES_PASSWORD = "x"; SALT = "x"; ENCRYPTION_KEY = "x"; CLICKHOUSE_PASSWORD = "x"; MINIO_ROOT_PASSWORD = "x";
            REDIS_AUTH = "x"; NEXTAUTH_SECRET = "x"; LANGFUSE_PUBLIC_KEY = "x"; LANGFUSE_SECRET_KEY = "x";
            LANGFUSE_USER_EMAIL = "x"; LANGFUSE_USER_PASSWORD = "x" }
foreach ($k in $dummy.Keys) { Set-Item -Path "env:$k" -Value $dummy[$k] }
$images = & $docker compose -f $compose config --images
if ($LASTEXITCODE -ne 0 -or -not $images) { Write-Host "could not read the image list" -ForegroundColor Red; exit 1 }

New-Item -ItemType Directory -Force -Path $out | Out-Null
foreach ($image in $images) {
    Write-Host "pulling $image"
    & $docker pull $image
    if ($LASTEXITCODE -ne 0) { Write-Host "pull failed: $image" -ForegroundColor Red; exit 1 }
}
$tar = Join-Path $out "langfuse-images.tar"
Write-Host "saving $($images.Count) images to $tar ..."
& $docker save -o $tar @images
if ($LASTEXITCODE -ne 0) { Write-Host "docker save failed" -ForegroundColor Red; exit 1 }
$size = [math]::Round((Get-Item $tar).Length / 1GB, 2)
$hash = (Get-FileHash -Algorithm SHA256 $tar).Hash
Set-Content -Path (Join-Path $out "langfuse-images.sha256") -Value "$hash  langfuse-images.tar" -Encoding ASCII
Write-Host "done: $tar ($size GB), SHA-256 $hash" -ForegroundColor Green
