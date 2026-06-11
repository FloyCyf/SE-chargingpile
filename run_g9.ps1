# =====================================================
#  run_g9.ps1 - G9 扩展调度策略端到端测试 (照 G8 模式)
#
#  Usage:
#    .\run_g9.ps1                     # 默认 (ratio=1, port=8000, 自动开浏览器)
#    .\run_g9.ps1 -Ratio 4            # 加快 (1秒=4虚拟分钟)
#    .\run_g9.ps1 -Port 8080          # 自定义端口
#    .\run_g9.ps1 -NoBrowser          # 不自动开浏览器
#    .\run_g9.ps1 -MaxRounds 60       # 调度循环最多跑 60 轮 (120s)
#
#  流程 (同 G8):
#    1) 杀端口 + 重置 DB
#    2) 后台启动 uvicorn
#    3) 打印 URL + 自动开浏览器
#    4) 阻塞等用户按 Enter
#    5) 用户: 打开浏览器 → 登录 → 切换"扩展调度策略"→ 应用
#    6) G9: 自动提交 11 辆测试车
#    7) 启动时钟 + 循环触发调度 (用用户选的策略) 直到等候区清空
#    8) 打印报告 + 服务器保留 (Ctrl+C 退出)
# =====================================================

# 强制 UTF-8 输出 (避免中文乱码)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 | Out-Null

param(
    [double]$Ratio = 2.0,
    [int]$Port = 8000,
    [int]$MaxDispatchRounds = 20,
    [int]$MaxWaitRounds = 40,
    [switch]$NoBrowser
)

$ProjectRoot = $PSScriptRoot
$PythonExe   = ".\.venv\Scripts\python.exe"

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  G9 扩展调度策略测试" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Ratio    : 1 real sec = $Ratio virtual min"
Write-Host "  Port     : $Port"
Write-Host "  MaxDispatch: $MaxDispatchRounds 轮 (= $([int]($MaxDispatchRounds * 3))s)"
Write-Host "  MaxWait    : $MaxWaitRounds 轮 (= $([int]($MaxWaitRounds * 3))s)"
Write-Host "  ETA        : ~3~6 min (调度 + 全部充电完毕)"
Write-Host ""

Set-Location $ProjectRoot
$env:PYTHONPATH       = "."
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"

# 构造参数列表
$argList = @("--ratio", $Ratio, "--port", $Port,
             "--max-dispatch-rounds", $MaxDispatchRounds,
             "--max-wait-rounds", $MaxWaitRounds)
if ($NoBrowser) { $argList += "--no-browser" }

& $PythonExe "scripts\g9_test.py" @argList
$exit = $LASTEXITCODE

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
if ($exit -eq 0) {
    Write-Host "  G9 完成 (退出码 0)" -ForegroundColor Green
} else {
    Write-Host "  G9 异常 (exit=$exit)" -ForegroundColor Red
}
Write-Host "  报告: scripts\g9_results.json" -ForegroundColor DarkGray
Write-Host "  日志: server_g9.log" -ForegroundColor DarkGray
Write-Host "==========================================" -ForegroundColor Cyan
exit $exit
