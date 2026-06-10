# =====================================================
# tail_server_log.ps1 - tail backend dispatch log
# Usage:
#   .\tail_server_log.ps1            # filter key events (recommended)
#   .\tail_server_log.ps1 -Full      # full backend stdout/stderr
# =====================================================

param(
    [switch]$Full
)

$LogFile = "D:\大三下作业\SE-chargingpile\server.log"

if (-not (Test-Path $LogFile)) {
    Write-Host "Waiting for server.log..." -ForegroundColor Yellow
    while (-not (Test-Path $LogFile)) {
        Start-Sleep -Milliseconds 500
    }
    Write-Host "server.log ready" -ForegroundColor Green
}

# force UTF-8 console so Chinese in log displays correctly
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
    chcp 65001 > $null
} catch {}

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
if ($Full) {
    Write-Host "  Tailing FULL backend log (Ctrl+C to quit)" -ForegroundColor Cyan
} else {
    Write-Host "  Tailing KEY events (Ctrl+C to quit)" -ForegroundColor Cyan
    Write-Host "  Filter: Dispatch / Clock / fault / Recover / FAULT" -ForegroundColor DarkGray
}
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

if ($Full) {
    Get-Content $LogFile -Wait -Tail 30 -Encoding UTF8
} else {
    Get-Content $LogFile -Wait -Tail 50 -Encoding UTF8 |
        Select-String -Pattern "Dispatch|Clock|fault|Recover|FAULT"
}
