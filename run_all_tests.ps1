# =====================================================
#  run_all_tests.ps1 - 一键跑全部测试
#
#  包含:
#    1. 离线单元测试  (tests/test_batch_policy.py)
#    2. G8 验收测试   (scripts/g8_test.py)   — 22 辆车, 32 事件
#    3. G9 策略对比   (scripts/g9_test.py)   — FIFO vs BATCH
#
#  全部通过 → exit 0
#  任一失败 → exit 1
# =====================================================

$ProjectRoot = $PSScriptRoot
$PythonExe   = ".\.venv\Scripts\python.exe"

Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Charging Pile - All Tests Runner" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host ""

Set-Location $ProjectRoot
$env:PYTHONPATH = "."
$env:PYTHONIOENCODING = "utf-8"

$results = @()

# ---------- 1. 单元测试 ----------
Write-Host "[1/3] 离线单元测试 ..." -ForegroundColor Yellow
Write-Host "----------------------------------------------------------"
& $PythonExe -m pytest tests/test_batch_policy.py -v --tb=short
$r1 = $LASTEXITCODE
$results += @{ Name = "单元测试"; Exit = $r1 }
if ($r1 -eq 0) {
    Write-Host "  [OK] 单元测试全部通过" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] 单元测试失败" -ForegroundColor Red
}
Write-Host ""

# ---------- 2. G8 验收 ----------
Write-Host "[2/3] G8 验收测试 (22 辆车, 32 事件, ratio=2) ..." -ForegroundColor Yellow
Write-Host "----------------------------------------------------------"
& $PythonExe "scripts\g8_test.py" --ratio 2 --port 8001
$r2 = $LASTEXITCODE
$results += @{ Name = "G8 验收"; Exit = $r2 }
if ($r2 -eq 0) {
    Write-Host "  [OK] G8 全部通过" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] G8 有失败项" -ForegroundColor Red
}
Write-Host ""

# ---------- 3. G9 策略对比 ----------
Write-Host "[3/3] G9 策略对比 (BATCH vs FIFO) ..." -ForegroundColor Yellow
Write-Host "----------------------------------------------------------"
& $PythonExe "scripts\g9_test.py" --ratio 12 --port 8002
$r3 = $LASTEXITCODE
$results += @{ Name = "G9 对比"; Exit = $r3 }
if ($r3 -eq 0) {
    Write-Host "  [OK] G9 完成" -ForegroundColor Green
} else {
    Write-Host "  [WARN] G9 退出码非 0" -ForegroundColor Yellow
}

# ---------- 总结 ----------
Write-Host ""
Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  测试结果汇总" -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan
foreach ($r in $results) {
    $status = if ($r.Exit -eq 0) { "[PASS]" } else { "[FAIL]" }
    $color  = if ($r.Exit -eq 0) { "Green" } else { "Red" }
    Write-Host ("  {0}  {1}  (exit={2})" -f $status, $r.Name, $r.Exit) -ForegroundColor $color
}
Write-Host "==========================================" -ForegroundColor Cyan

$anyFail = ($results | Where-Object { $_.Exit -ne 0 }).Count
Read-Host "Press Enter to exit"
exit $anyFail
