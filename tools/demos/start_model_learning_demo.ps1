# Review only. No production onboarding, configuration or network side effects.
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$demo = Join-Path $PSScriptRoot 'model_learning_demo.py'
if (-not (Test-Path -LiteralPath $python)) { throw 'Project virtual environment not found.' }
Start-Process -FilePath $python -ArgumentList ('"' + $demo + '"') -WorkingDirectory $projectRoot -WindowStyle Hidden
