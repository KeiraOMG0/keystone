#Requires -Version 5.1
<#
  install-all.ps1
  Runs all four setup steps in order. If .env didn't exist yet, step 2 will
  create it from .env.example and stop here so you can fill in secrets --
  just re-run this script once that's done.
#>

$ErrorActionPreference = "Stop"
$ScriptDir = $PSScriptRoot

& "$ScriptDir\01-check-tools.ps1"
& "$ScriptDir\02-setup-python-env.ps1"

$RepoRoot = Resolve-Path (Join-Path $ScriptDir "..\..")
if (-not (Test-Path (Join-Path $RepoRoot ".env"))) {
    # 02-setup-python-env.ps1 already printed the "fill in .env" instructions.
    exit 0
}

& "$ScriptDir\03-setup-database.ps1"
& "$ScriptDir\04-build-frontend.ps1"

Write-Host ""
Write-Host "==================================================================" -ForegroundColor Green
Write-Host " Install complete. Start the bot and dashboard in separate windows:" -ForegroundColor Green
Write-Host "==================================================================" -ForegroundColor Green
Write-Host "  venv\Scripts\python.exe main.py"
Write-Host "  venv\Scripts\python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 8080"
Write-Host ""
Write-Host "See docs\ARCHITECTURE.md sections 5 and 11 for more detail."
