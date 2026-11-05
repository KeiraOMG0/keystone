#Requires -Version 5.1
<#
  03-setup-database.ps1
  Applies the dashboard backend's Alembic migrations (server\dashboard.db).
  The bot's own database (bot.db) does NOT need a manual step here -- it
  migrates itself automatically the first time main.py runs (see
  docs\ARCHITECTURE.md section 7).
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

if (-not (Test-Path ".env")) {
    Write-Host ".env not found. Run 02-setup-python-env.ps1 first and fill in .env." -ForegroundColor Red
    exit 1
}

$PythonExe = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) {
    Write-Host "venv not found. Run 02-setup-python-env.ps1 first." -ForegroundColor Red
    exit 1
}

Write-Host "== Applying dashboard database migrations (Alembic) ==" -ForegroundColor Cyan
Write-Host "This must run from the repo root -- alembic.ini's script_location is relative to it."
& $PythonExe -m alembic -c server\alembic.ini upgrade head

Write-Host ""
Write-Host "Dashboard database ready (server\dashboard.db)." -ForegroundColor Green
Write-Host "The bot's own database (bot.db) will be created and migrated automatically" -ForegroundColor Green
Write-Host "the first time you run main.py -- no manual step needed for it." -ForegroundColor Green
