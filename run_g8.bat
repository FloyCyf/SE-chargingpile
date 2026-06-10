@echo off
REM ============================================
REM  run_g8.bat - double-click launcher
REM  bypasses PowerShell execution policy
REM ============================================
chcp 65001 > nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_g8.ps1" %*
pause
