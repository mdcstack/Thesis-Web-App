@echo off
title Configure Windows Firewall for BOOKLETLOG
cd /d "%~dp0"
color 0b

echo ==============================================================
echo   BOOKLETLOG - Windows Firewall Configuration Utility
echo ==============================================================
echo.
echo This script opens Port 8000 (HTTPS) and Port 8080 (HTTP) on
echo Windows Firewall across Private and Public network profiles so
echo mobile devices on your Wi-Fi router can connect without errors.
echo.

REM Check administrator privileges
net session >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo [REQUESTING ADMIN ELEVATION]
    echo Please click "Yes" on the prompt to allow administrator access...
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"\"%~dp0allow_firewall.bat\"\" runas' -Verb RunAs"
    exit /b 0
)

echo Adding Windows Firewall rules...
netsh advfirewall firewall delete rule name="BOOKLETLOG LAN Server (Port 8000 HTTPS)" >nul 2>&1
netsh advfirewall firewall delete rule name="BOOKLETLOG LAN Server (Port 8080 HTTP)" >nul 2>&1

netsh advfirewall firewall add rule name="BOOKLETLOG LAN Server (Port 8000 HTTPS)" dir=in action=allow protocol=TCP localport=8000 profile=any >nul 2>&1
netsh advfirewall firewall add rule name="BOOKLETLOG LAN Server (Port 8080 HTTP)" dir=in action=allow protocol=TCP localport=8080 profile=any >nul 2>&1

color 0a
echo.
echo [SUCCESS] Windows Firewall rules added successfully!
echo   • Inbound Port 8000 (HTTPS) -> ALLOWED (All Profiles)
echo   • Inbound Port 8080 (HTTP)  -> ALLOWED (All Profiles)
echo.
echo Mobile devices connected to your router can now connect freely!
echo.
pause

