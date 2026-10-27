# Native preview only. Does not launch the application or change user configuration.
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$demo = Join-Path $PSScriptRoot 'voice_oobe_demo.py'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Project virtual environment not found. Set up the LimboWave environment first.'
}
# Hide only the launcher console; the demo creates its own visible native window.
Start-Process -FilePath $python -ArgumentList ('"' + $demo + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden
