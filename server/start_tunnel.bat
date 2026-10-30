@echo off
REM Starts the Cloudflare Tunnel ("idlesys") so dash.idlesys.xyz reaches
REM the dashboard backend on localhost:8080. If it crashes, the window
REM stays open so you can read the error.
cd /d "%~dp0"

echo Starting Cloudflare Tunnel "idlesys" (dash.idlesys.xyz -> localhost:8080) ...
echo (this window must stay open - closing it stops the tunnel)
echo.

cloudflared tunnel run idlesys

echo.
echo ============================================
echo Tunnel stopped or crashed (see above for errors).
echo ============================================
pause
