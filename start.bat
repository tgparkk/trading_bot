@echo off
echo Starting Trading Bot...

REM 가상환경 활성화
call venv\Scripts\activate

REM 환경 변수 로드
if exist .env (
    echo Loading environment variables...
    for /f "delims== tokens=1,2" %%G in (.env) do set %%G=%%H
) else (
    echo Error: .env file not found!
    echo Please copy .env.example to .env and fill in your API credentials.
    pause
    exit /b 1
)

REM 봇 실행
python main.py

REM 종료 시 일시 정지
pause
