@echo off
rem ============================================================
rem  NGA crawler - Windows launcher
rem  The interactive menu lives in menu.py (UTF-8 safe).
rem  Double-click this file, or run: python menu.py
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY="
where python >nul 2>nul
if not errorlevel 1 set "PY=python"
if not defined PY (
    where py >nul 2>nul
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo [ERROR] Python 3.8+ not found. Install Python and add it to PATH.
    echo         https://www.python.org/downloads/
    pause
    exit /b 1
)

%PY% menu.py
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo menu exited with code %RC%.
    pause
)
endlocal & exit /b %RC%
