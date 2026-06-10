# =====================================================
# run_g8.ps1 - G8 test case launcher
# Usage:
#   .\run_g8.ps1                        # default port 8001, ratio 2
#   .\run_g8.ps1 -Port 8002 -Ratio 4    # custom
#   .\run_g8.ps1 -NoReset               # skip DB reset
# =====================================================

param(
    [int]$Port = 8001,
    [double]$Ratio = 2,
    [switch]$NoReset
)

# Use $PSScriptRoot to avoid encoding issues with Chinese paths
$ProjectRoot = $PSScriptRoot
$PythonExe   = ".\.venv\Scripts\python.exe"

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  G8 Charging Pile Acceptance Test" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Project : $ProjectRoot"
Write-Host "  Port    : $Port"
Write-Host "  Ratio   : 1 real sec = $Ratio virtual min"
$estSec = [int](300 / $Ratio + 60)
Write-Host "  ETA     : ~$estSec sec"
Write-Host ""

# 1. cd to script directory
Set-Location $ProjectRoot

# 2. check venv
if (-not (Test-Path $PythonExe)) {
    Write-Host "[ERROR] venv not found: $PythonExe" -ForegroundColor Red
    Write-Host "Run: python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt"
    Read-Host "Press Enter to exit"
    exit 1
}

# 3. env vars (process scope)
$env:PYTHONPATH = "."
$env:PYTHONIOENCODING = "utf-8"

# 4. kill any process holding the target port
Write-Host "[0/3] Checking port $Port ..." -ForegroundColor Yellow
$netstatLines = netstat -ano 2>$null | Select-String ":$Port\s"
if ($netstatLines) {
    $pids = @()
    foreach ($line in $netstatLines) {
        $parts = $line.Line -split '\s+'
        $pidStr = $parts[-1]
        $state = $parts[-2]
        if ($pidStr -match '^\d+$' -and $state -eq 'LISTENING') {
            $pids += [int]$pidStr
        }
    }
    $pids = $pids | Select-Object -Unique
    if ($pids.Count -gt 0) {
        Write-Host "  Port $Port is in use by PID(s): $($pids -join ', ')" -ForegroundColor Magenta
        Write-Host "  Killing old server process ..." -ForegroundColor Yellow
        foreach ($pid in $pids) {
            taskkill /PID $pid /F 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "  Killed PID $pid" -ForegroundColor Green
            }
        }
        Start-Sleep -Seconds 2
    }
}
else {
    Write-Host "  Port $Port is free." -ForegroundColor Green
}

# 4b. also check for any Python processes that might hold charging.db
$pythonProcs = Get-Process -Name "python" -ErrorAction SilentlyContinue
if ($pythonProcs) {
    Write-Host "  Found $($pythonProcs.Count) Python process(es) running:" -ForegroundColor DarkYellow
    foreach ($p in $pythonProcs) {
        $cmdline = (Get-WmiObject Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty CommandLine)
        if (-not $cmdline) { $cmdline = "(unknown)" }
        Write-Host "    PID $($p.Id): $cmdline" -ForegroundColor DarkGray
    }
    Write-Host "  If DB reset fails, close these and re-run." -ForegroundColor DarkYellow
}
Write-Host ""

# 5. reset db
if (-not $NoReset) {
    Write-Host "[1/3] Reset database..." -ForegroundColor Yellow
    & $PythonExe -m scripts.init_db --reset
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] DB reset failed (exit=$LASTEXITCODE)" -ForegroundColor Red
        Write-Host ""
        Write-Host "  Possible causes:" -ForegroundColor Yellow
        Write-Host "  1. Another process is holding charging.db (see Python list above)."
        Write-Host "     Close all Python/terminal windows and re-run."
        Write-Host "  2. Anti-virus or file sync tool locking the file."
        Write-Host ""
        Write-Host "  Or re-run with: run_g8.bat -NoReset"
        Read-Host "Press Enter to exit"
        exit 1
    }
}
else {
    Write-Host "[1/3] Skip DB reset (--NoReset)" -ForegroundColor DarkYellow
}

# 6. browser prompt
Write-Host ""
Write-Host "[2/3] Browser setup" -ForegroundColor Yellow
Write-Host "  When server is up, open:"
Write-Host "    http://127.0.0.1:$Port/admin.html" -ForegroundColor Green
Write-Host "  Press F12 > Console, paste this to login as admin:"
Write-Host @"

    fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: 'admin', password: 'admin123' })
    }).then(r => r.json()).then(d => {
      localStorage.setItem('admin_token', d.access_token);
      location.reload();
    });
"@ -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Tail dispatch logs in another PS window:" -ForegroundColor Cyan
Write-Host "    .\tail_server_log.ps1" -ForegroundColor Cyan
Write-Host ""

# 7. run g8_test
Write-Host "[3/3] Starting g8_test.py..." -ForegroundColor Yellow
Write-Host "----------------------------------------------------------"
& $PythonExe "scripts\g8_test.py" --port $Port --ratio $Ratio
$exit = $LASTEXITCODE

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
if ($exit -eq 0) {
    Write-Host "  Test finished: ALL PASS" -ForegroundColor Green
}
else {
    Write-Host "  Test finished: FAILURES (exit=$exit)" -ForegroundColor Red
}
Write-Host "  Server log: server.log"
Write-Host "==========================================" -ForegroundColor Cyan
Read-Host "Press Enter to close"
exit $exit
