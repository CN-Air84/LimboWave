# 建立或修复 .venv，并按 uv.lock 安装全部依赖。可重复执行。
. "$PSScriptRoot/common.ps1"

Assert-Uv
Invoke-Uv -UvArgs @('sync')

Write-Host ''
Write-Host '--- 环境自检 ---' -ForegroundColor Cyan
Invoke-Uv -UvArgs @('run', 'python', 'scripts/selfcheck.py')

Write-Host ''
Write-Host 'bootstrap 完成。' -ForegroundColor Green
