# 运行测试。透传参数，例如：.\scripts\test.ps1 tests/unit -k bootstrap
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Rest = @()
)

. "$PSScriptRoot/common.ps1"

Assert-Uv
Invoke-Uv -UvArgs (@('run', 'pytest') + $Rest)
