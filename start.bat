@echo off
setlocal enabledelayedexpansion
echo ============================================================
echo  Bioreactor OPC Dashboard
echo ============================================================
cd /d "%~dp0"

REM Use the mambaforge/conda Python which already has all packages
set PYTHON=C:\ProgramData\mambaforge\python.exe
if not exist "%PYTHON%" set PYTHON=python

"%PYTHON%" --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found at %PYTHON%
    echo Please check the PYTHON path at the top of this file.
    pause & exit /b 1
)

echo Checking/installing dependencies...
"%PYTHON%" -m pip install -r backend\requirements.txt -q 2>nul

REM Show accessible URLs
echo.
echo This dashboard is accessible at:
echo   http://localhost:8000          (this PC)
for /f "tokens=2 delims=:" %%a in ('ipconfig 2^>nul ^| findstr /i "IPv4"') do (
    set RAWIP=%%a
    set RAWIP=!RAWIP: =!
    if not "!RAWIP!"=="" echo   http://!RAWIP!:8000      (network access)
)
echo.
echo Press Ctrl+C to stop the server.
echo ============================================================
echo.

"%PYTHON%" -m uvicorn backend.app:app --host 0.0.0.0 --port 8000

pause
