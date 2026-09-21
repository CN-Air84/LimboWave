# 静态质量门：ruff 检查、格式化一致性、mypy 严格类型检查。
. "$PSScriptRoot/common.ps1"

Assert-Uv

Invoke-Uv -UvArgs @('run', 'ruff', 'check', '.')
Invoke-Uv -UvArgs @('run', 'ruff', 'format', '--check', '.')
Invoke-Uv -UvArgs @('run', 'mypy')

Write-Host '静态检查全部通过。' -ForegroundColor Green
