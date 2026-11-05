@echo off
REM Runs the bot (main.py) from the project venv. This one process also hosts the
REM internal API (127.0.0.1:8766) and spawns the PO-token server for music (:4416).
REM Start this BEFORE run_dashboard.bat. Closing this window stops the bot.
cd /d "%~dp0..\.."
echo Starting kick-bot (main.py)...
venv\Scripts\python.exe main.py
pause
