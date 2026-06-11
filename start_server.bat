@echo off
REM ============================================
REM  start_server.bat - 启动后端 (多 PC 访问)
REM  Usage: 双击运行 或 start_server.bat [port]
REM  默认端口 8000, 监听 0.0.0.0 (允许局域网访问)
REM ============================================
chcp 65001 > nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_server.ps1" %*
pause
