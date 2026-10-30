@echo off
REM Starts the full stack on boot/login: main bot (+ embedded internal API + db),
REM the dashboard backend, and the cloudflare tunnel — each in its own window.
REM Close a window to stop that process. Can be placed in shell:startup to run at login
REM (install_scripts\windows\05-install-autostart.bat instead adds run_bot.bat and
REM run_dashboard.bat shortcuts, without the tunnel).

cd /d "%~dp0"

if not exist ".env" (
    echo ERROR: .env not found in %cd%
    echo Copy .env.example to .env and fill in the values first.
    pause
    exit /b 1
)

echo Starting bot (main.py: Discord bot + internal API + db) ...
start "kick-bot - main" cmd /k "venv\Scripts\python.exe main.py"

echo Waiting for internal API to come up...
timeout /t 5 /nobreak >nul

echo Starting dashboard (uvicorn server.app:app on :8080) ...
start "kick-bot - dashboard" cmd /k "venv\Scripts\python.exe -m uvicorn server.app:app --host 127.0.0.1 --port 8080"

echo Starting cloudflare tunnel (dash.idlesys.xyz -> :8080) ...
start "kick-bot - tunnel" cmd /k "server\start_tunnel.bat"

echo.
echo All three processes launched in separate windows.
