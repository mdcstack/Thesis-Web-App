@echo off
title BOOKLETLOG Diagnostic Server Console
cd /d "%~dp0"
color 0b

echo ==============================================================
echo   BOOKLETLOG - Interactive Server Console
echo ==============================================================
echo.

REM Resolve python executable
set PYTHON_BIN=python
where python >nul 2>&1
if %ERRORLEVEL% neq 0 (
    if exist "C:\Users\Matt\AppData\Local\Programs\Python\Python314\python.exe" (
        set PYTHON_BIN="C:\Users\Matt\AppData\Local\Programs\Python\Python314\python.exe"
    ) else (
        set PYTHON_BIN=py
    )
)

%PYTHON_BIN% run_servers.py
echo.
pause
