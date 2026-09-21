# 提交前基线：静态检查 + 全部测试。两者都过才算通过。
. "$PSScriptRoot/common.ps1"

& "$PSScriptRoot/lint.ps1"
& "$PSScriptRoot/test.ps1"

Write-Host ''
Write-Host '质量基线通过。' -ForegroundColor Green
