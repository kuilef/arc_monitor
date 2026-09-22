$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) { throw 'Create .venv first: py -3 -m venv .venv' }
& $Python -m pip install -e '.[dev]'
if ($LASTEXITCODE -ne 0) { throw 'Installation failed.' }
& $Python -m pytest --cov=arc_monitor --cov-report=term-missing
exit $LASTEXITCODE
