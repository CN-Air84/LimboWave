# Review only: inherits the shared proposals; does not launch or configure LimboWave.
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$demo = Join-Path $PSScriptRoot 'oobe_joint_demo.py'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Project virtual environment not found.'
}
Start-Process -FilePath $python -ArgumentList ('"' + $demo + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden
