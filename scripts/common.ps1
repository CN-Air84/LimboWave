# 公共前置：严格模式、UTF-8 控制台、仓库根定位、uv 可用性检查。
# 兼容 Windows PowerShell 5.1（本机未安装 PowerShell 7）。

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# 中文路径与中文输出：统一 UTF-8，不要 BOM。
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8NoBom
[Console]::InputEncoding = $utf8NoBom
$OutputEncoding = $utf8NoBom

$script:RepoRoot = Split-Path -Parent $PSScriptRoot

function Assert-Uv {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw '未找到 uv。安装方式见 https://docs.astral.sh/uv/getting-started/installation/'
    }
}

function Invoke-Uv {
    param(
        [Parameter(Mandatory = $true)]
        [string[]] $UvArgs
    )

    Push-Location $script:RepoRoot
    try {
        & uv @UvArgs
        if ($LASTEXITCODE -ne 0) {
            throw "uv $($UvArgs -join ' ') 失败，退出码 $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}
