param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
$Python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (-not (Test-Path $Python)) {
    & py -3 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create venv. Install Python 3.11 or newer.' }
}
& $Python -m pip install -e .
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
Write-Host "Open http://127.0.0.1:$Port ; Ctrl+C stops the server. Synthetic data only."
& $Python -m arc_monitor --db data/demo.db --mode demo demo --serve --port $Port
exit $LASTEXITCODE
