# Stops the DPS processes that scripts\start-dps.ps1 started (listed in dps\logs\dps.pids), and nothing else.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop-dps.ps1
$dps = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $dps "logs\dps.pids"
if (-not (Test-Path $pidFile)) { Write-Host "Nothing to stop: no logs\dps.pids (DPS was not started by start-dps.ps1)."; exit 0 }
foreach ($line in Get-Content $pidFile) {
    if ($line -notmatch '^(\d+) (\S+)$') { continue }
    $procId, $name = [int]$Matches[1], $Matches[2]
    $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -like "python*") {
        # the worker's job threads and its parser children end with it
        & taskkill /PID $procId /T /F | Out-Null
        Write-Host ("  stopped {0,-16} pid {1}" -f $name, $procId)
    } elseif ($p) {
        Write-Host ("  left {0} alone: pid {1} is now '{2}', not DPS" -f $name, $procId, $p.ProcessName) -ForegroundColor Yellow
    } else {
        Write-Host ("  {0,-16} pid {1} had already stopped" -f $name, $procId)
    }
}
Remove-Item $pidFile -Force
Write-Host "DPS stopped."
