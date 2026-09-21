# 解析依赖并写入/更新 uv.lock。加 -Upgrade 主动升级到允许范围内的新版本。
param(
    [switch] $Upgrade
)

. "$PSScriptRoot/common.ps1"

Assert-Uv

$lockArgs = @('lock')
if ($Upgrade) {
    $lockArgs += '--upgrade'
}

Invoke-Uv -UvArgs $lockArgs

Write-Host 'uv.lock 已更新。' -ForegroundColor Green
