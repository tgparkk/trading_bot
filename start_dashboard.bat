@echo off
echo Starting Trading Bot Dashboard...

REM 현재 디렉토리 저장
set ORIGINAL_DIR=%CD%

REM 가상환경 활성화
call venv\Scripts\activate

REM Node.js가 설치되어 있는지 확인
where node >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Error: Node.js is not installed or not in PATH
    echo Please install Node.js from https://nodejs.org/
    pause
    exit /b 1
)

REM 기존 프로세스 종료 (포트 5050을 사용 중인 프로세스)
echo Checking for existing processes on port 5050...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr :5050 ^| findstr LISTENING') do (
    echo Killing process: %%p
    taskkill /F /PID %%p > nul 2>&1
)

REM 텔레그램 봇 관련 잠금 파일 확인 및 제거
if exist telegram_bot.lock (
    echo Removing telegram_bot.lock file...
    del /f telegram_bot.lock
)

REM 하나의 창에서 백엔드만 실행 (텔레그램 통합 포함)
echo Starting backend server at http://localhost:5050...
start cmd /k "cd %ORIGINAL_DIR%\backend && python app.py"

REM 백엔드 서버가 시작될 때까지 잠시 대기
echo Waiting for backend server to start...
timeout /t 5 /nobreak > nul

REM frontend 디렉토리로 이동하여 프론트엔드 실행
cd %ORIGINAL_DIR%\frontend
echo Starting frontend at http://localhost:3000...

REM 종속성이 설치되어 있는지 확인
if not exist node_modules (
    echo Installing frontend dependencies...
    call npm install
)

REM 프론트엔드 실행
echo.
echo ==========================================================
echo Front-end will start now. Please open http://localhost:3000 in your browser.
echo Backend API is at http://localhost:5050
echo ==========================================================
echo.

npm start

cd %ORIGINAL_DIR%
pause 