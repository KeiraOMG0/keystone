@echo off
setlocal EnableDelayedExpansion

echo ============================================================
echo  kick-bot  ^|  PyInstaller Build Script
echo ============================================================

:: Resolve paths relative to this script's location
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "VENV=%ROOT%\venv"
set "PYTHON=%VENV%\Scripts\python.exe"
set "PYINSTALLER=%VENV%\Scripts\pyinstaller.exe"
set "DIST=%ROOT%\dist\Bot"
set "WORK=%ROOT%\build"

:: ── Preflight ────────────────────────────────────────────────
if not exist "%PYTHON%" (
    echo [ERROR] venv Python not found at: %PYTHON%
    echo         Create the venv first:  python -m venv venv
    exit /b 1
)
if not exist "%PYINSTALLER%" (
    echo [ERROR] PyInstaller not found. Install it:
    echo         venv\Scripts\pip.exe install pyinstaller
    exit /b 1
)

:: ── Clean previous output ────────────────────────────────────
echo.
echo [1/4] Cleaning previous build output...
if exist "%DIST%" (
    rmdir /s /q "%DIST%"
    echo       Removed: %DIST%
)
if exist "%WORK%\main" (
    rmdir /s /q "%WORK%\main"
    echo       Removed: %WORK%\main
)
if exist "%ROOT%\main.spec" (
    del /f /q "%ROOT%\main.spec"
    echo       Removed: main.spec
)

:: ── Run PyInstaller ─────────────────────────────────────────
echo.
echo [2/4] Running PyInstaller (onedir)...

"%PYINSTALLER%" ^
    --noconfirm ^
    --onedir ^
    --name main ^
    --distpath "%DIST%" ^
    --workpath "%WORK%\main" ^
    --specpath "%ROOT%" ^
    --hidden-import "cogs.kick" ^
    --hidden-import "cogs.template_commands" ^
    --hidden-import "cogs.gateway" ^
    --hidden-import "cogs.event_commands" ^
    --hidden-import "cogs.managed_commands" ^
    --hidden-import "cogs.tickets" ^
    --hidden-import "cogs.advanced_commands" ^
    --hidden-import "cogs.reaction_roles" ^
    --hidden-import "cogs.help_commands" ^
    --hidden-import "cogs.qotd" ^
    --hidden-import "cogs.birthdays" ^
    --hidden-import "cogs.birthday_config" ^
    --hidden-import "cogs.birthday_override" ^
    --hidden-import "cogs.birthday_io" ^
    --hidden-import "cogs.music" ^
    --hidden-import "core.logging_setup" ^
    --hidden-import "core.event_bus" ^
    --hidden-import "core.component_builder" ^
    --hidden-import "core.placeholder_engine" ^
    --hidden-import "core.renderer" ^
    --hidden-import "core.sync_queue" ^
    --hidden-import "core.validator" ^
    --hidden-import "core.placeholders" ^
    --hidden-import "database.connection" ^
    --hidden-import "database.migrations.runner" ^
    --hidden-import "database.repositories.advanced_repo" ^
    --hidden-import "database.repositories.audit_repo" ^
    --hidden-import "database.repositories.base" ^
    --hidden-import "database.repositories.birthday_repo" ^
    --hidden-import "database.repositories.embed_repo" ^
    --hidden-import "database.repositories.event_repo" ^
    --hidden-import "database.repositories.guild_repo" ^
    --hidden-import "database.repositories.managed_repo" ^
    --hidden-import "database.repositories.qotd_repo" ^
    --hidden-import "database.repositories.reaction_role_repo" ^
    --hidden-import "database.repositories.template_repo" ^
    --hidden-import "database.repositories.ticket_repo" ^
    --hidden-import "database.repositories.version_repo" ^
    --hidden-import "services.audit_service" ^
    --hidden-import "services.autorole_service" ^
    --hidden-import "services.birthday_service" ^
    --hidden-import "services.event_service" ^
    --hidden-import "services.managed_message_service" ^
    --hidden-import "services.permission_service" ^
    --hidden-import "services.qotd_service" ^
    --hidden-import "services.scheduler_service" ^
    --hidden-import "services.template_service" ^
    --hidden-import "services.ticket_service" ^
    --hidden-import "ui.confirm" ^
    --hidden-import "ui.embed_editor" ^
    --hidden-import "ui.paginator" ^
    --hidden-import "config" ^
    --hidden-import "discord" ^
    --hidden-import "discord.ext.commands" ^
    --hidden-import "discord.ext.tasks" ^
    --hidden-import "aiohttp" ^
    --hidden-import "dotenv" ^
    --hidden-import "sqlite3" ^
    --add-data "%ROOT%\cogs;cogs" ^
    --add-data "%ROOT%\core;core" ^
    --add-data "%ROOT%\database;database" ^
    --add-data "%ROOT%\services;services" ^
    --add-data "%ROOT%\ui;ui" ^
    --add-data "%ROOT%\config.py;." ^
    "%ROOT%\main.py"

if errorlevel 1 (
    echo.
    echo [ERROR] PyInstaller failed. See output above.
    exit /b 1
)

:: ── Copy runtime files ───────────────────────────────────────
echo.
echo [3/4] Copying runtime files into dist folder...

set "DEST=%DIST%\main"

for %%F in (bot.db .env kicks.log) do (
    if exist "%ROOT%\%%F" (
        copy /y "%ROOT%\%%F" "%DEST%\%%F" >nul
        echo       Copied: %%F
    ) else (
        echo       Skipped ^(not found^): %%F
    )
)

:: Copy start.bat and README.txt to the DEST folder (next to main.exe)
for %%F in (start.bat README.txt) do (
    if exist "%ROOT%\%%F" (
        copy /y "%ROOT%\%%F" "%DEST%\%%F" >nul
        echo       Copied: %%F
    ) else (
        echo       Skipped ^(not found^): %%F
    )
)

:: ── Done ─────────────────────────────────────────────────────
echo.
echo [4/4] Build complete!
echo       Output: %DEST%
echo.
echo  To run the bot:  cd "%DEST%" ^&^& start.bat
echo ============================================================
endlocal
