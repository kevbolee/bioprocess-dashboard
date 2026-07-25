@echo off
echo ================================================================
echo  Bioreactor OPC Dashboard -- Prerequisites Installer
echo ================================================================
echo.
echo This will install:
echo   - Python 3 (Miniforge3)
echo   - ODBC Driver 17 for SQL Server
echo   - Python packages (fastapi, uvicorn, pyodbc, etc.)
echo   - Create .env from .env.example
echo.
echo A UAC prompt will appear -- click Yes to allow the installation.
echo.
pause

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File ""%~dp0setup.ps1""' -Verb RunAs -Wait"
