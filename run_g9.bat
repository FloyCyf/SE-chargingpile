@echo off
REM ============================================
REM  run_g9.bat - 只跑 G9 策略对比实验
REM  Usage: 双击运行
REM ============================================
chcp 65001 > nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_g9.ps1" %*
pause
