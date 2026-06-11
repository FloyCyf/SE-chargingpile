# =====================================================
#  start_server.ps1 - 一键启动后端服务 (多 PC 访问)
#
#  Usage:
#    .\start_server.ps1                # 默认端口 8000
#    .\start_server.ps1 -Port 8001     # 自定义端口
#    .\start_server.ps1 -NoReset       # 不重置数据库
#
#  启动后:
#    本机访问:  http://127.0.0.1:8000/admin.html
#    局域网:    http://<本机IP>:8000/admin.html
# =====================================================

param(
    [int]$Port = 8000,
    [switch]$NoReset
)

$ProjectRoot = $PSScriptRoot
$PythonExe   = ".\.venv\Scripts\python.exe"
$LocalIP     = (Get-NetIPAddress -AddressFamily IPv4 `
                 | Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.*" } `
                 | Select-Object -First 1 -ExpandProperty IPAddress)

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Charging Pile Server Launcher" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Project : $ProjectRoot"
Write-Host "  Port    : $Port"
Write-Host "  Local   : http://127.0.0.1:$Port/admin.html"
if ($LocalIP) {
    Write-Host "  LAN     : http://${LocalIP}:$Port/admin.html" -ForegroundColor Green
}
Write-Host ""

Set-Location $ProjectRoot

# 1. 检查 venv
if (-not (Test-Path $PythonExe)) {
    Write-Host "[ERROR] venv not found: $PythonExe" -ForegroundColor Red
    Write-Host "Run: python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt"
    Read-Host "Press Enter to exit"
    exit 1
}

# 2. 杀旧进程
Write-Host "[1/4] Killing any process on port $Port ..." -ForegroundColor Yellow
$netstatLines = netstat -ano 2>$null | Select-String ":$Port\s"
if ($netstatLines) {
    foreach ($line in $netstatLines) {
        $parts = $line.Line -split '\s+'
        $pidStr = $parts[-1]
        $state  = $parts[-2]
        if ($pidStr -match '^\d+$' -and $state -eq 'LISTENING') {
            Write-Host "  Killing PID $pidStr ..." -ForegroundColor DarkYellow
            taskkill /PID $pidStr /F 2>$null | Out-Null
        }
    }
    Start-Sleep -Seconds 2
}
Write-Host "  Port $Port is free." -ForegroundColor Green

# 3. 重置 DB
if (-not $NoReset) {
    Write-Host ""
    Write-Host "[2/4] Resetting database ..." -ForegroundColor Yellow
    & $PythonExe -m scripts.init_db --reset
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[ERROR] DB reset failed (exit=$LASTEXITCODE)" -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
} else {
    Write-Host ""
    Write-Host "[2/4] Skip DB reset (-NoReset)" -ForegroundColor DarkYellow
}

# 4. 设置环境变量
$env:PYTHONPATH       = "."
$env:PYTHONIOENCODING = "utf-8"

# 5. 启动后端 (前台运行, 看到日志)
Write-Host ""
Write-Host "[3/4] Starting uvicorn on 0.0.0.0:$Port ..." -ForegroundColor Yellow
Write-Host "  局域网其他 PC 可通过上面的 LAN URL 访问" -ForegroundColor Cyan
Write-Host "  按 Ctrl+C 停止服务" -ForegroundColor DarkGray
Write-Host "----------------------------------------------------------"

& $PythonExe -m uvicorn src.main:app --host 0.0.0.0 --port $Port
