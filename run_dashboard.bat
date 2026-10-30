@echo off
REM Runs the dashboard backend (server/app.py) on 127.0.0.1:8080 from the project venv.
REM Open http://localhost:8080 (not 127.0.0.1) so the OAuth redirect and cookies match.
REM Needs the bot running (run_bot.bat) for live data. No auto-reload: restart after
REM editing server/*.py. Frontend changes only need `npm run build` in server\frontend.
cd /d "%~dp0"
echo Starting dashboard (uvicorn server.app:app)...
venv\Scripts\python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 8080
pause
