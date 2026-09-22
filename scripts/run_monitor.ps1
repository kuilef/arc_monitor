param([int]$Cycles = 0)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Create the project virtual environment first.' }
Set-Location -LiteralPath $projectRoot
$logRoot = Join-Path $projectRoot 'reports\monitor'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$logFile = Join-Path $logRoot ('collector-' + (Get-Date -Format 'yyyy-MM-dd') + '.log')
# The application loads .env itself. No credentials are passed in arguments or logs.
# Quota failures intentionally end the run; do not restart in a tight loop.
& $pythonExe -m arc_monitor poll --cycles $Cycles --max-requests 100 --enrich-every 10 2>&1 |
    Tee-Object -FilePath $logFile -Append
exit $LASTEXITCODE
