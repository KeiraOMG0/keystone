#Requires -Version 5.1
<#
  04-build-frontend.ps1
  Installs frontend npm dependencies and builds the production bundle
  (server\frontend\dist), which server\app.py serves statically.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$FrontendDir = Join-Path $RepoRoot "server\frontend"

if (-not (Get-Command "npm" -ErrorAction SilentlyContinue)) {
    Write-Host "npm not found. Run 01-check-tools.ps1 first (installs Node.js, which includes npm)." -ForegroundColor Red
    exit 1
}

Set-Location $FrontendDir

Write-Host "== Installing frontend dependencies ==" -ForegroundColor Cyan
npm install

Write-Host ""
Write-Host "== Building production frontend (tsc -b && vite build) ==" -ForegroundColor Cyan
npm run build

Set-Location $RepoRoot

Write-Host ""
Write-Host "Frontend built to server\frontend\dist\." -ForegroundColor Green
