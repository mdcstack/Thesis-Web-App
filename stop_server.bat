@echo off
title Stop BOOKLETLOG Servers
cd /d "%~dp0"
color 0e

REM Run the dedicated bulletproof PowerShell termination script
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_server.ps1"

REM Extra safeguard fallback directly in CMD
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 :8080"') do (
    if not "%%a"=="0" (
        taskkill /F /T /PID %%a >nul 2>&1
    )
)

color 0a
echo.
echo Server termination process finished. Closing in 2 seconds...
ping 127.0.0.1 -n 3 >nul
exit /b 0
