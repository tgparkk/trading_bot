@echo off
setlocal

REM 새 창에서 자신을 실행하는 코드
SET "ALREADY_STARTED=0"
IF "%~1"=="CHILD_PROCESS" SET "ALREADY_STARTED=1"

REM 이미 새 창에서 실행 중이 아니라면 새 창에서 자신을 다시 실행
IF %ALREADY_STARTED%==0 (
    start "Trading Bot - Main Process" cmd /k "%~f0" CHILD_PROCESS
    exit /b
)

REM 여기서부터 실제 실행 내용
chcp 65001
title Trading Bot - Auto Trading System
echo [Start] Trading Bot Execution (Backend) 
echo. 
echo Setting environment variables... 
set TEST_MODE=False
set USE_FAKE_ACCOUNT=False
set SKIP_WEBSOCKET=True
set LOGGING_LEVEL=DEBUG
set | findstr TEST_MODE 
set | findstr USE_FAKE_ACCOUNT
set | findstr SKIP_WEBSOCKET 
set | findstr LOGGING_LEVEL
echo. 
echo Trading Bot Configuration: 
echo - Test Mode: Active - Runs regardless of market hours
echo - Skip WebSocket: Active - Ignores WebSocket connection errors
echo - Logging Level: Debug - Detailed logs will be printed
echo. 
echo Log Records: 
echo ========================================= 
 
REM 환경 변수 확인 스크립트 실행 
python -c "import os; print('Python Environment Variables:'); print(f\"SKIP_WEBSOCKET Value: '{os.environ.get('SKIP_WEBSOCKET', 'Not Set')}'\" , len(os.environ.get('SKIP_WEBSOCKET', '').strip())); print('='*50)" 
 
REM 작업 디렉토리 설정
cd /d %~dp0

REM 환경 변수 설정 - 큰따옴표 없이 값만 설정
set SKIP_WEBSOCKET=True

REM Python 디버그 모드 끄기
set PYTHONDONTWRITEBYTECODE=1

REM 오늘 날짜 형식 (YYYY-MM-DD) 가져오기
for /f "tokens=2-4 delims=/ " %%a in ('date /t') do (
    set today=%%c-%%a-%%b
)

REM 해당 날짜의 로그 폴더가 이미 있으면 삭제
if exist logs\%today% (
    echo 테스트를 위해 오늘 로그 폴더 삭제 및 재생성...
    rd /s /q logs\%today%
)

REM 로그 폴더 확인
if not exist logs mkdir logs
if not exist logs\%today% mkdir logs\%today%

REM 메인 프로그램 실행 - 업데이트 옵션 없이 실행
python main.py

REM 종료 대기
pause

endlocal 