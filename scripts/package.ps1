# 生成便携包（Task 10.3）。
#
# 产物是 dist/LimboWave/ 下一个**自带解释器与依赖**的目录：
#   LimboWave.cmd        双击即用（先跑依赖自检，再启动应用）
#   python/              独立 CPython 解释器（由 uv 下载，不依赖系统 Python）
#   site-packages/       应用与依赖
#   selfcheck.py         运行时依赖检查
#
# **用户资料库不在这个目录里**——它在 %LOCALAPPDATA%\LimboWave。
# 因此「删除这个目录」等于卸载，且**不可能**碰掉用户数据。这是结构性保证，
# 不是靠卸载脚本的自觉。
#
# 兼容 Windows PowerShell 5.1（本机无 pwsh）：UTF-8 BOM + CRLF。

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

. "$PSScriptRoot/common.ps1"

$distRoot = Join-Path $script:RepoRoot 'dist'
$appDir = Join-Path $distRoot 'LimboWave'
$pythonDir = Join-Path $appDir 'python'
$siteDir = Join-Path $appDir 'site-packages'

Write-Host "==> 清理旧产物：$appDir"
if (Test-Path $appDir) { Remove-Item -Recurse -Force $appDir }
New-Item -ItemType Directory -Force -Path $appDir | Out-Null

Assert-Uv

Write-Host '==> 下载独立 Python 运行时（不依赖系统 Python）'
Invoke-Uv -UvArgs @('python', 'install', '3.12', '--install-dir', $pythonDir)

# uv 会把解释器放进版本化子目录（cpython-3.12.x-.../python.exe）。
# 它同时尝试在 $pythonDir 根下建一个便捷入口，但本机已有未受管的
# ~/.local/bin/python3.12.exe 时会拒绝——那不影响我们要的东西，
# 所以这里**直接定位真实解释器**，不去动用户环境。
$pythonExe = Get-ChildItem -Path $pythonDir -Filter 'python.exe' -Recurse -File |
    Where-Object { $_.DirectoryName -notlike '*\Scripts*' } |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $pythonExe) {
    throw "未找到便携解释器（$pythonDir 下没有 python.exe）"
}
Write-Host "    解释器：$pythonExe"

Write-Host '==> 安装应用与运行期依赖'
Invoke-Uv -UvArgs @(
    'pip', 'install',
    '--python', $pythonExe,
    '--target', $siteDir,
    '--no-compile',          # 省体积；首次运行由解释器按需编译
    $script:RepoRoot
)

Write-Host '==> 复制自检脚本（运行时依赖检查的入口）'
Copy-Item (Join-Path $script:RepoRoot 'scripts/selfcheck.py') (Join-Path $appDir 'selfcheck.py') -Force

Write-Host '==> 生成启动器 LimboWave.cmd'
# 启动器里写相对路径：便携包必须能整个复制到别处运行。
# 用 .NET 的相对路径 API，避免正则转义在多层工具链里被吃掉。
$sep = [char]92
$appPrefix = $appDir.TrimEnd($sep)
if (-not $pythonExe.StartsWith($appPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "解释器不在应用目录内，便携包会不自包含：$pythonExe"
}
$pythonRel = $pythonExe.Substring($appPrefix.Length).TrimStart($sep)
$launcher = @"
@echo off
rem LimboWave 便携启动器：先做运行期依赖自检，再启动应用。
chcp 65001 >nul
setlocal
set HERE=%~dp0
set PYTHONPATH=%HERE%site-packages
set PYTHONNOUSERSITE=1

"%HERE%$pythonRel" "%HERE%selfcheck.py" --runtime
if errorlevel 1 (
    echo.
    echo 依赖自检未通过，已中止启动。请查看上面的缺失清单。
    pause
    exit /b 1
)

"%HERE%$pythonRel" -m limbowave %*
endlocal
"@

# 启动器写 **UTF-8 无 BOM**，并在脚本内 chcp 65001：
# 用 ASCII 写会把中文变成问号（实跑发现的）。
$launcherPath = Join-Path $appDir 'LimboWave.cmd'
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText($launcherPath, $launcher, $utf8NoBom)

Write-Host '==> 校验产物'
if (-not (Test-Path (Join-Path $siteDir 'limbowave'))) {
    throw 'site-packages 下没有 limbowave，安装失败'
}

Write-Host ''
Write-Host '便携包已生成：' -NoNewline
Write-Host $appDir -ForegroundColor Green
Write-Host ''
Write-Host '运行方式：双击 LimboWave.cmd，或命令行执行同目录下的 cmd 文件。'
Write-Host '首次运行会要求设置主密码（资料库在 %LOCALAPPDATA%\LimboWave）。'
Write-Host ''
Write-Host '关于卸载：' -ForegroundColor Yellow
Write-Host '  直接删除上面的目录即可。用户资料库不在其中，因此不会被删除——'
Write-Host '  要清除数据需自行删除 %LOCALAPPDATA%\LimboWave（本脚本与启动器都不会做这件事）。'
