@echo off
echo Starting Trading Bot Dashboard...

REM 가상환경 활성화
call env\Scripts\activate

REM 백엔드 실행
cd backend
python app.py 