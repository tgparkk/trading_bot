@echo off
REM Clean up any existing processes
taskkill /F /IM node.exe 2>nul
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :5050 ^| findstr LISTENING') do taskkill /F /PID %%p 2>nul
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :3001 ^| findstr LISTENING') do taskkill /F /PID %%p 2>nul
if exist telegram_bot.lock (
    del /f telegram_bot.lock
)

REM Save current directory
set BASE_DIR=%CD%

REM Activate virtual environment
call venv\Scripts\activate

REM Start API server
start "API Server" cmd /k "color 0A && cd %BASE_DIR%\backend && python app.py"

REM Wait for API to start (더 긴 대기 시간 부여)
echo Waiting for backend server to initialize (15 seconds)...
timeout /t 15 /nobreak > nul

REM Start frontend
start "Frontend" cmd /k "color 0B && cd %BASE_DIR%\frontend && set PORT=3001 && npm start"

echo.
echo API Server: http://localhost:5050
echo Frontend: http://localhost:3001
echo.
echo Keep both windows open!
echo.
echo Testing API connection...
curl -s http://localhost:5050/api/test
echo. 