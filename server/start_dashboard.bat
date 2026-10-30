@echo off
REM Starts the dashboard backend (server/app.py) in this window.
REM If it crashes, the window stays open so you can read the error.
cd /d "%~dp0.."

echo Starting dashboard backend on http://localhost:8080 ...
echo (this window must stay open - closing it stops the dashboard)
echo.

"venv\Scripts\python.exe" -m uvicorn server.app:app --host 127.0.0.1 --port 8080

echo.
echo ============================================
echo Dashboard backend stopped or crashed (see above for errors).
echo ============================================
pause
