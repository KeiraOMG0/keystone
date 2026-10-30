@echo off
REM sv.bat - control the RUNNING kick-bot supervisor (supervisor.py) without its console.
REM   sv              menu (status + one-key actions)
REM   sv help         every command
REM Commands other than menu/help/logs/start/stop/install are passed to
REM "supervisor.py <command>", which talks to the control API on 127.0.0.1:8767.
REM "sv install" adds a Desktop shortcut and/or puts this folder on your PATH.
setlocal EnableExtensions
cd /d "%~dp0"
set "PY=%~dp0venv\Scripts\python.exe"
set "PYW=%~dp0venv\Scripts\pythonw.exe"
if not exist "%PY%" (
    echo venv not found: %PY%
    exit /b 1
)

set "CMD=%~1"
if "%CMD%"=="" goto menu
if /i "%CMD%"=="menu" goto menu
if /i "%CMD%"=="m" goto menu
if /i "%CMD%"=="help" goto help
if /i "%CMD%"=="h" goto help
if /i "%CMD%"=="-h" goto help
if /i "%CMD%"=="--help" goto help
if /i "%CMD%"=="logs" goto cmd_logs
if /i "%CMD%"=="l" goto cmd_logs
if /i "%CMD%"=="start" goto cmd_start
if /i "%CMD%"=="u" goto cmd_start
if /i "%CMD%"=="stop" goto cmd_stop
if /i "%CMD%"=="x" goto cmd_stop
if /i "%CMD%"=="install" goto cmd_install
"%PY%" supervisor.py %*
exit /b %errorlevel%

:cmd_logs
call :open_log "%~2"
exit /b %errorlevel%

:cmd_start
call :start_supervisor
exit /b %errorlevel%

:cmd_stop
call :stop_supervisor
exit /b %errorlevel%

:cmd_install
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_scripts\windows\07-install-sv.ps1"
exit /b %errorlevel%

REM ---------------------------------------------------------------- help

:help
echo.
echo  sv - kick-bot supervisor control           (sv = this folder's sv.bat)
echo.
echo   sv                          menu: status plus one-key actions
echo   sv status            (s)    readable status of supervisor, bot, dashboard, tunnel
echo   sv json              (j)    the same status as raw JSON
echo   sv health            (c)    HTTP check of bot, dashboard and the public site
echo   sv restart ^<t^> [why] (r)    t = bot^|dashboard^|tunnel^|all  (or b^|d^|t^|a)
echo   sv rotate [why]      (o)    archive the logs now
echo   sv tail ^<svc^> [n]    (t)    last n output lines of bot^|dashboard^|tunnel
echo   sv follow ^<log^>      (f)    live log in THIS window (supervisor^|bot^|dashboard^|tunnel)
echo   sv logs ^<log^>        (l)    the same in a NEW window (close it to stop)
echo   sv start             (u)    start the supervisor in the background (no window)
echo   sv stop              (x)    stop the supervisor AND bot, dashboard, tunnel (asks first)
echo   sv pid                      print the supervisor's pid
echo   sv install                  Desktop shortcut and/or put this folder on PATH
echo   sv help              (h)    this text
echo.
echo  Examples:  sv r d dashboard froze     sv f bot     sv t tunnel 100
echo  Logs on disk: logs\supervisor\  (supervisor.log = crashes, restarts, health checks)
echo.
exit /b 0

REM ---------------------------------------------------------------- menu

:menu
cls
echo.
echo  kick-bot supervisor                                  (sv help = all commands)
echo  ============================================================================
"%PY%" supervisor.py summary
echo.
echo   [S] Refresh        [C] Health check   [R] Restart...    [O] Rotate logs
echo   [L] Live log...    [T] Recent output  [U] Start sup.    [X] Stop everything
echo   [H] Help           [Q] Quit
echo.
choice /c SCROLTUXHQ /n /m " Choose: "
set "K=%errorlevel%"
if "%K%"=="1" goto menu
if "%K%"=="2" goto menu_health
if "%K%"=="3" goto menu_restart
if "%K%"=="4" goto menu_rotate
if "%K%"=="5" goto menu_logs
if "%K%"=="6" goto menu_tail
if "%K%"=="7" goto menu_start
if "%K%"=="8" goto menu_stop
if "%K%"=="9" goto menu_help
exit /b 0

:menu_health
echo.
"%PY%" supervisor.py health
goto menu_back

:menu_restart
echo.
echo   Restart which?   [B] Bot   [D] Dashboard   [T] Tunnel   [A] All   [Q] Back
choice /c BDTAQ /n /m " > "
set "K=%errorlevel%"
if "%K%"=="5" goto menu
for /f "tokens=%K%" %%t in ("bot dashboard tunnel all") do set "TARGET=%%t"
call :ask_reason
"%PY%" supervisor.py restart %TARGET% "%REASON%"
goto menu_back

:menu_rotate
echo.
choice /c YN /m "  Archive all supervisor logs now"
if errorlevel 2 goto menu
call :ask_reason
"%PY%" supervisor.py rotate-logs "%REASON%"
goto menu_back

:menu_logs
echo.
echo   Live log (opens a new window)?   [S] supervisor   [B] bot   [D] dashboard   [T] tunnel   [Q] Back
choice /c SBDTQ /n /m " > "
set "K=%errorlevel%"
if "%K%"=="5" goto menu
for /f "tokens=%K%" %%t in ("supervisor bot dashboard tunnel") do call :open_log %%t
goto menu

:menu_tail
echo.
echo   Recent output of?   [B] bot   [D] dashboard   [T] tunnel   [Q] Back
choice /c BDTQ /n /m " > "
set "K=%errorlevel%"
if "%K%"=="4" goto menu
for /f "tokens=%K%" %%t in ("bot dashboard tunnel") do set "TARGET=%%t"
echo.
"%PY%" supervisor.py tail %TARGET% 40
goto menu_back

:menu_start
echo.
call :start_supervisor
goto menu_back

:menu_stop
echo.
call :stop_supervisor
goto menu_back

:menu_help
call :help
goto menu_back

:menu_back
echo.
pause
goto menu

REM ---------------------------------------------------------------- helpers

:ask_reason
set "REASON="
set /p "REASON=  Reason (optional, Enter to skip): "
REM quotes would break the quoted argument below
if defined REASON set "REASON=%REASON:"=%"
exit /b 0

:open_log
set "LOG=%~1"
if "%LOG%"=="" set "LOG=supervisor"
if /i "%LOG%"=="s" set "LOG=supervisor"
if /i "%LOG%"=="b" set "LOG=bot"
if /i "%LOG%"=="d" set "LOG=dashboard"
if /i "%LOG%"=="dash" set "LOG=dashboard"
if /i "%LOG%"=="t" set "LOG=tunnel"
start "kick-bot log: %LOG%" "%PY%" supervisor.py follow %LOG%
exit /b 0

:start_supervisor
"%PY%" supervisor.py pid >nul 2>&1
if not errorlevel 1 (
    echo  The supervisor is already running.
    exit /b 0
)
echo  Starting the supervisor in the background (no window)...
start "" /d "%~dp0" "%PYW%" supervisor.py
for /l %%i in (1,1,20) do (
    timeout /t 1 /nobreak >nul
    "%PY%" supervisor.py pid >nul 2>&1 && goto start_ok
)
echo  Its control API did not answer within 20 s. Check logs\supervisor\supervisor.log
echo  (another copy may be running, or the control secret is missing from .env).
exit /b 1
:start_ok
echo  Supervisor is up; it starts the bot, then the dashboard and the tunnel.
exit /b 0

:stop_supervisor
set "SVPID="
for /f %%p in ('call "%PY%" supervisor.py pid 2^>nul') do set "SVPID=%%p"
if not defined SVPID (
    echo  The supervisor's control API is not answering, so nothing was stopped.
    echo  To find a stray copy:  Get-CimInstance Win32_Process ^| Where-Object CommandLine -like '*supervisor.py*'
    exit /b 1
)
echo  This stops the supervisor (pid %SVPID%) AND the bot, dashboard and tunnel it runs.
echo  Nothing restarts them until you run "sv start" (or log in again).
choice /c YN /m "  Stop everything"
if errorlevel 2 (
    echo  Cancelled.
    exit /b 1
)
taskkill /PID %SVPID% /T /F
exit /b %errorlevel%
