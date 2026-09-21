# 启动应用。透传参数，例如：.\scripts\run.ps1 --smoke
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Rest = @()
)

. "$PSScriptRoot/common.ps1"

Assert-Uv
Invoke-Uv -UvArgs (@('run', 'python', '-m', 'limbowave') + $Rest)
