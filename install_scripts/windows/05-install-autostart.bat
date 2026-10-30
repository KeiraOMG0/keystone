@echo off
setlocal
set "REPO_DIR=%~dp0..\.."
for %%I in ("%REPO_DIR%") do set "REPO_DIR=%%~fI"

set "STARTUP_DIR=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"

echo Installing autostart shortcuts to:
echo   %STARTUP_DIR%
echo.

powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%STARTUP_DIR%\kick-bot.lnk');" ^
  "$s.TargetPath = '%REPO_DIR%\run_bot.bat';" ^
  "$s.WorkingDirectory = '%REPO_DIR%';" ^
  "$s.WindowStyle = 1;" ^
  "$s.Save()"

powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%STARTUP_DIR%\kick-bot-dashboard.lnk');" ^
  "$s.TargetPath = '%REPO_DIR%\run_dashboard.bat';" ^
  "$s.WorkingDirectory = '%REPO_DIR%';" ^
  "$s.WindowStyle = 1;" ^
  "$s.Save()"

echo.
echo Done. Both the bot and dashboard will now start automatically at login,
echo each in its own console window.
echo.
echo To remove autostart later, delete these two files:
echo   %STARTUP_DIR%\kick-bot.lnk
echo   %STARTUP_DIR%\kick-bot-dashboard.lnk
pause
