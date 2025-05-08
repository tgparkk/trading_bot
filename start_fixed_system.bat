@echo off
echo ===================================================
echo   Trading Bot Enhanced System Launcher
echo ===================================================
echo.

echo This script will start the main trading bot, backend API server, and frontend.
echo.

REM Kill existing processes
echo Terminating existing processes...
taskkill /F /IM node.exe 2>nul
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :5050 ^| findstr LISTENING') do (
    taskkill /F /PID %%p 2>nul
)
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :3001 ^| findstr LISTENING') do (
    taskkill /F /PID %%p 2>nul
)
for /f "tokens=2" %%p in ('tasklist ^| findstr python.exe') do (
    taskkill /F /PID %%p 2>nul
)
if exist telegram_bot.lock (
    del /f telegram_bot.lock
)

REM Current directory
set ORIGINAL_DIR=%CD%

REM Activate virtual environment
echo Activating virtual environment...
call venv\Scripts\activate

REM Start main trading bot in a new window (for automatic scanning and trading)
echo.
echo Starting main trading bot in a new window (automatic scanning and trading)...
start "Trading Bot Main System" cmd /k "color 0E && cd %ORIGINAL_DIR% && python main.py"

echo Waiting for main trading bot to initialize (20 seconds)...
echo This wait is needed for the main bot to initialize and connect to KIS API.
timeout /t 20 /nobreak > nul

REM Start original backend server in a new window
echo.
echo Starting fixed backend server in a new window...
start "Trading Bot Backend Server" cmd /k "color 0A && cd %ORIGINAL_DIR%\backend && python app.py"

echo Waiting for backend server to initialize (15 seconds)...
echo This longer wait is needed for the full backend to initialize properly.
timeout /t 15 /nobreak > nul

REM Start frontend in a new window
echo.
echo Starting frontend in a new window (on port 3001)...
start "Trading Bot Frontend" cmd /k "color 0B && cd %ORIGINAL_DIR%\frontend && set PORT=3001 && npm start"

echo.
echo All systems have been started in separate windows.
echo.
echo Main Trading Bot: Running (automatic scanning and trading)
echo Backend Server: http://localhost:5050
echo Frontend: http://localhost:3001
echo.
echo IMPORTANT: Keep all command windows open!
echo.
echo Testing backend API connection...
curl -s http://localhost:5050/api/test
echo.
echo.
echo If backend is working correctly, you should see a JSON response above.
echo If not, check the backend console window for error messages.
echo.
echo NOTICE: The system is now set up for automatic scanning and trading!
echo         You can monitor activities in the main trading bot window.
echo         Use the web interface to visualize data and manage trades.
echo.
pause 