@echo off
REM install-all.bat
REM Double-click-friendly wrapper. Runs the same steps as install-all.ps1
REM by invoking PowerShell under the hood -- edit the .ps1 files if you need
REM to change behavior, not this file.

setlocal
set SCRIPT_DIR=%~dp0

powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%install-all.ps1"
set EXIT_CODE=%ERRORLEVEL%

echo.
echo Press any key to close this window...
pause >nul

exit /b %EXIT_CODE%
