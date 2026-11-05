#Requires -Version 5.1
<#
  02-setup-python-env.ps1
  Creates the venv at the repo root and installs both requirement files.
  Also copies .env.example to .env if .env doesn't exist yet, then stops
  so you can fill in the required secrets before continuing.
#>

$ErrorActionPreference = "Stop"

# Resolve the repo root as two levels up from this script
# (scripts/windows/this-file.ps1 -> repo root).
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

Write-Host "== Creating virtual environment ==" -ForegroundColor Cyan
if (Test-Path "venv") {
    Write-Host "venv\ already exists, skipping creation."
} else {
    python -m venv venv
    Write-Host "Created venv\"
}

$Pip = Join-Path $RepoRoot "venv\Scripts\pip.exe"

Write-Host ""
Write-Host "== Installing bot dependencies (requirements.txt) ==" -ForegroundColor Cyan
& $Pip install -r requirements.txt

Write-Host ""
Write-Host "== Installing dashboard backend dependencies (server\requirements.txt) ==" -ForegroundColor Cyan
& $Pip install -r server\requirements.txt

Write-Host ""
Write-Host "== Checking for .env ==" -ForegroundColor Cyan
if (Test-Path ".env") {
    Write-Host ".env already exists, leaving it untouched."
} else {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "ACTION REQUIRED: open .env and fill in at least:" -ForegroundColor Red
    Write-Host "  DISCORD_TOKEN, INTERNAL_API_SECRET, DISCORD_CLIENT_ID," -ForegroundColor Red
    Write-Host "  DISCORD_CLIENT_SECRET, SESSION_COOKIE_SECRET" -ForegroundColor Red
    Write-Host "See docs\ARCHITECTURE.md section 6 for what each variable does." -ForegroundColor Red
    Write-Host ""
    Write-Host "Re-run this script (or just continue to 03-setup-database.ps1) once .env is filled in." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "Python environment ready." -ForegroundColor Green
