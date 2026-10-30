@echo off
setlocal
set SCRIPT_DIR=%~dp0
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%04-build-frontend.ps1"
set EXIT_CODE=%ERRORLEVEL%
echo.
echo Press any key to close this window...
pause >nul
exit /b %EXIT_CODE%
