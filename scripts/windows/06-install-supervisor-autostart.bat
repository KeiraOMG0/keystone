@echo off
REM Registers a Task Scheduler task that runs supervisor.py (no console window) at
REM logon. Remove with:  schtasks /Delete /TN "kick-bot supervisor" /F
REM Do not combine with 05-install-autostart.bat or start_all.bat (double start).
set "ROOT=%~dp0..\.."
pushd "%ROOT%"
set "ROOT=%CD%"
popd
schtasks /Create /TN "kick-bot supervisor" /SC ONLOGON /RL LIMITED /F ^
  /TR "\"%ROOT%\venv\Scripts\pythonw.exe\" \"%ROOT%\supervisor.py\""
if errorlevel 1 (echo Failed to create task.) else (echo Task created. It starts at your next logon.)
pause
