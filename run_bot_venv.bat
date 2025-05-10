@echo off
setlocal EnableDelayedExpansion

REM ============================================
REM      Trading Bot Launcher with venv
REM ============================================

REM Run in new window if not already in child process
if not "%~1"=="CHILD_PROCESS" (
    start "Trading Bot - Main Process" cmd /k "%~f0" CHILD_PROCESS
    exit /b
)

REM Initial setup
chcp 65001 >nul 2>&1
title Trading Bot - Auto Trading System
cd /d %~dp0

echo.
echo [START] Trading Bot Execution
echo ========================================

REM Virtual environment directory
set VENV_DIR=venv

REM Create virtual environment if it doesn't exist
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo.
    echo [SETUP] Creating virtual environment...
    python -m venv %VENV_DIR%
    if !errorlevel! neq 0 (
        echo [ERROR] Failed to create virtual environment
        pause
        exit /b 1
    )
    echo [DONE] Virtual environment created
    
    REM Activate virtual environment
    call %VENV_DIR%\Scripts\activate.bat
    
    REM Upgrade pip
    echo [SETUP] Upgrading pip to latest version...
    python -m pip install --upgrade pip
    
    REM Install requirements
    if exist requirements.txt (
        echo [INSTALL] Installing packages from requirements.txt...
        pip install -r requirements.txt
        if !errorlevel! neq 0 (
            echo [ERROR] Failed to install packages
            pause
            exit /b 1
        )
        echo [DONE] Package installation complete
    ) else (
        echo [WARNING] requirements.txt not found
        echo [INSTALL] Installing essential packages only...
        pip install python-dotenv requests pandas numpy pyjwt websocket-client websockets PyYAML loguru
    )
) else (
    echo [INFO] Virtual environment already exists
    
    REM Activate virtual environment
    call %VENV_DIR%\Scripts\activate.bat
)

REM Set environment variables
set TEST_MODE=False
set USE_FAKE_ACCOUNT=False
set SKIP_WEBSOCKET=True
set LOGGING_LEVEL=DEBUG
set PYTHONDONTWRITEBYTECODE=1

echo.
echo [CONFIG] Environment variables set:
echo - Python: %VENV_DIR%\Scripts\python.exe
echo - TEST_MODE: %TEST_MODE%
echo - USE_FAKE_ACCOUNT: %USE_FAKE_ACCOUNT%
echo - SKIP_WEBSOCKET: %SKIP_WEBSOCKET%
echo - LOGGING_LEVEL: %LOGGING_LEVEL%

REM Get current date
for /f "tokens=*" %%I in ('powershell -Command "$d = Get-Date; Write-Host $d.ToString('yyyy-MM-dd')"') do set today=%%I

if "%today%"=="" (
    for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /format:list') do set datetime=%%I
    set year=!datetime:~0,4!
    set month=!datetime:~4,2!
    set day=!datetime:~6,2!
    set today=!year!-!month!-!day!
)

echo.
echo [INFO] Today's date: %today%

REM Prepare log directory
if not exist logs mkdir logs
if not exist "logs\%today%" (
    mkdir "logs\%today%"
    echo [INFO] Created today's log directory: logs\%today%
) else (
    echo [INFO] Using existing log directory: logs\%today%
)

REM Display Python and package versions
echo.
echo [INFO] Execution environment:
python --version
pip --version
echo.

REM Run main program
echo [RUN] Starting Trading Bot...
echo ========================================
echo.

python main.py

endlocal