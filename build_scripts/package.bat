@echo off
echo ============================================================
echo  kick-bot  ^|  Package Script
echo ============================================================

set "ROOT=%~dp0"
set "DIST=%ROOT%dist\Bot"
set "PYTHON=%ROOT%venv\Scripts\python.exe"
set "ZIP_OUT=%ROOT%Bot.zip"
set "HELPER=%ROOT%_package_helper.py"

echo ROOT = %ROOT%
echo DIST = %DIST%

if not exist "%DIST%" (
    echo [ERROR] dist\Bot folder not found. Run build.bat first.
    pause
    exit /b 1
)
if not exist "%PYTHON%" (
    echo [ERROR] venv Python not found.
    pause
    exit /b 1
)
if not exist "%HELPER%" (
    echo [ERROR] _package_helper.py not found next to package.bat.
    pause
    exit /b 1
)

echo.
echo [1/3] Removing old archive if present...
if exist "%ZIP_OUT%" (
    del /f /q "%ZIP_OUT%"
    echo       Deleted: Bot.zip
) else (
    echo       No old archive found.
)

echo.
echo [2/3] Creating Bot.zip from dist\Bot...
"%PYTHON%" "%HELPER%" "%DIST%" "%ZIP_OUT%"

if errorlevel 1 (
    echo.
    echo [ERROR] Packaging failed.
    pause
    exit /b 1
)

echo.
echo [3/3] Done!
echo       Archive ready: %ZIP_OUT%
echo ============================================================
pause
