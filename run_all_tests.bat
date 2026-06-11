@echo off
REM ============================================
REM  run_all_tests.bat - 一键跑全部测试
REM  Usage: 双击运行
REM  包含: 单元测试 + G8 验收 + G9 策略对比
REM ============================================
chcp 65001 > nul
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_all_tests.ps1" %*
pause
