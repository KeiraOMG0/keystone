#Requires -Version 5.1
<#
  01-check-tools.ps1
  Checks for Python, Node.js, and ffmpeg. Installs any that are missing via
  winget (Windows Package Manager, built into Windows 10 1809+/Windows 11).
  Run this first, from a PowerShell prompt with permission to install
  software (winget will prompt for elevation itself if needed).
#>

$ErrorActionPreference = "Stop"

function Test-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Ensure-Winget {
    if (-not (Test-Command "winget")) {
        Write-Host "winget was not found. It ships with Windows 10 (1809+) and Windows 11." -ForegroundColor Red
        Write-Host "Install 'App Installer' from the Microsoft Store, then re-run this script." -ForegroundColor Red
        exit 1
    }
}

Write-Host "== Checking for Python ==" -ForegroundColor Cyan
if (Test-Command "python") {
    $pyVersion = (python --version) 2>&1
    Write-Host "Found: $pyVersion"
} else {
    Write-Host "Python not found. Installing via winget (Python 3.12)..." -ForegroundColor Yellow
    Ensure-Winget
    winget install --id Python.Python.3.12 -e --source winget
    Write-Host "Python installed. You may need to restart this terminal for PATH changes to take effect." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "== Checking for Node.js ==" -ForegroundColor Cyan
if (Test-Command "node") {
    $nodeVersion = (node --version)
    Write-Host "Found: node $nodeVersion"
} else {
    Write-Host "Node.js not found. Installing via winget (Node.js LTS)..." -ForegroundColor Yellow
    Ensure-Winget
    winget install --id OpenJS.NodeJS.LTS -e --source winget
    Write-Host "Node.js installed. You may need to restart this terminal for PATH changes to take effect." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "== Checking for ffmpeg ==" -ForegroundColor Cyan
if (Test-Command "ffmpeg") {
    Write-Host "Found: ffmpeg"
} else {
    Write-Host "ffmpeg not found. Installing via winget..." -ForegroundColor Yellow
    Ensure-Winget
    winget install --id Gyan.FFmpeg -e --source winget
    Write-Host "ffmpeg installed. You may need to restart this terminal for PATH changes to take effect." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Tool check complete." -ForegroundColor Green
Write-Host "If anything was just installed, close and reopen your terminal before running 02-setup-python-env.ps1," -ForegroundColor Yellow
Write-Host "so this session picks up the updated PATH." -ForegroundColor Yellow
