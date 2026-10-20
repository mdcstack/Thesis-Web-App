@echo off
title BOOKLETLOG - Database and Workspace Reset Utility
color 0c
cd /d "%~dp0"

echo ==============================================================
echo   BOOKLETLOG - COMPLETE DATABASE AND WORKSPACE RESET
echo ==============================================================
echo.
echo   WARNING: This operation will completely wipe all database
echo   tables, batches, enrollments, booklets, and uploaded templates!
echo.
echo   After reset, only a single Administrator account will remain:
echo     - Username: admin
echo     - Password: admin123
echo     - Role:     Admin (All Departments)
echo.
echo   System settings will return to Day 0 (Unconfigured Period).
echo ==============================================================
echo.

set CONFIRM=
if /i "%~1"=="/y" set CONFIRM=y
if /i "%~1"=="-y" set CONFIRM=y
if /i "%~1"=="--yes" set CONFIRM=y
if /i "%~1"=="yes" set CONFIRM=y

if not "%CONFIRM%"=="y" (
    set /p CONFIRM="Are you sure you want to proceed with FULL RESET? (y/N): "
)

if /i not "%CONFIRM%"=="y" (
    echo.
    echo [CANCELLED] Database reset aborted. No changes were made.
    echo.
    if "%~1"=="" pause
    exit /b 0
)

color 0b
echo.
echo Executing full clean reset...
echo.

REM Detect Python launcher (py, python, or direct path)
set PYTHON_CMD=
where py >nul 2>&1
if %ERRORLEVEL% equ 0 (
    set PYTHON_CMD=py
) else (
    where python >nul 2>&1
    if %ERRORLEVEL% equ 0 (
        set PYTHON_CMD=python
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" (
        set PYTHON_CMD="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set PYTHON_CMD="%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    ) else if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
        set PYTHON_CMD="%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    )
)

if "%PYTHON_CMD%"=="" (
    color 0c
    echo [ERROR] Python was not found on your system!
    echo Please ensure Python is installed and added to PATH.
    echo.
    if "%~1"=="" pause
    exit /b 1
)

%PYTHON_CMD% manage_db.py --full-reset --yes
if %ERRORLEVEL% neq 0 (
    color 0c
    echo.
    echo [FAILED] Database reset encountered an error!
) else (
    color 0a
    echo.
    echo [SUCCESS] Full clean database reset completed successfully!
)

echo.
if "%~1"=="" pause
