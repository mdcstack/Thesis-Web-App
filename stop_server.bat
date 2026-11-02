@echo off
title Stop BOOKLETLOG Servers
cd /d "%~dp0"
color 0e
echo ==============================================================
echo   BOOKLETLOG - Stop Background Servers
echo ==============================================================
echo.
echo Stopping any running Uvicorn and Python server processes...

REM Kill by process commandline pattern
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*run_servers.py*' -or $_.CommandLine -like '*app.main:app*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

color 0a
echo.
echo [SUCCESS] All BOOKLETLOG server processes have been stopped.
echo.
timeout /t 3 >nul

