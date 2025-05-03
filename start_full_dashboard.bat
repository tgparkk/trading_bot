@echo off
echo Starting Trading Bot Dashboard (Backend, Frontend, and Telegram Bot)...

REM 현재 디렉토리 저장
set ORIGINAL_DIR=%CD%

REM 가상환경 활성화
call env\Scripts\activate

REM Node.js가 설치되어 있는지 확인
where node >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Error: Node.js is not installed or not in PATH
    echo Please install Node.js from https://nodejs.org/
    pause
    exit /b 1
)

REM 텔레그램 봇을 별도의 프로세스로 시작
echo Starting Telegram bot service...
start cmd /k "cd %ORIGINAL_DIR% && python start_telegram_bot.py"

REM 잠시 대기
timeout /t 2 /nobreak > nul

REM 백엔드 서버를 별도의 프로세스로 시작
echo Starting backend server at http://localhost:5050...
start cmd /k "cd %ORIGINAL_DIR%\backend && python app.py"

REM 백엔드 서버가 시작될 때까지 잠시 대기
echo Waiting for backend server to start...
timeout /t 5 /nobreak > nul

REM frontend 디렉토리로 이동하여 프론트엔드만 실행
cd %ORIGINAL_DIR%\frontend
echo Starting frontend at http://localhost:3000...

REM 종속성이 설치되어 있는지 확인
if not exist node_modules (
    echo Installing frontend dependencies...
    call npm install
)

REM 프론트엔드만 실행 (백엔드는 이미 별도 창에서 실행 중)
npm start

cd %ORIGINAL_DIR%
pause 