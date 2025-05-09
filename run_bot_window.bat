@echo off
setlocal EnableDelayedExpansion

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
echo "[Start] Trading Bot Execution (Backend)"
echo.
echo "Setting environment variables..."
set TEST_MODE=False
set USE_FAKE_ACCOUNT=False
set SKIP_WEBSOCKET=True
set LOGGING_LEVEL=DEBUG
set | findstr TEST_MODE 
set | findstr USE_FAKE_ACCOUNT
set | findstr SKIP_WEBSOCKET 
set | findstr LOGGING_LEVEL
echo.
echo "Trading Bot Configuration:"
echo "- Test Mode: Active - Runs regardless of market hours"
echo "- Skip WebSocket: Active - Ignores WebSocket connection errors"
echo "- Logging Level: Debug - Detailed logs will be printed"
echo.
echo "Log Records:"
echo "========================================="
 
REM 환경 변수 확인 스크립트 실행 
python -c "import os; print('Python Environment Variables:'); print(f\"SKIP_WEBSOCKET Value: '{os.environ.get('SKIP_WEBSOCKET', 'Not Set')}'\" , len(os.environ.get('SKIP_WEBSOCKET', '').strip())); print('='*50)" 
 
REM 작업 디렉토리 설정
cd /d %~dp0

REM 환경 변수 설정 - 큰따옴표 없이 값만 설정
set SKIP_WEBSOCKET=True

REM Python 디버그 모드 끄기
set PYTHONDONTWRITEBYTECODE=1

REM 패키지 설치를 위한 임시 Python 스크립트 생성
echo "Creating temporary install script..."
echo import sys > install_packages.py
echo import subprocess >> install_packages.py
echo import os >> install_packages.py
echo print('Requirements file detected. Installing packages...') >> install_packages.py
echo. >> install_packages.py
echo try: >> install_packages.py
echo     with open('requirements.txt', 'r', encoding='utf-8') as f: >> install_packages.py
echo         packages = [line.strip() for line in f if line.strip() and not line.startswith('#')] >> install_packages.py
echo     print(f'Found {len(packages)} packages to install') >> install_packages.py
echo     for pkg in packages: >> install_packages.py
echo         print(f'Installing {pkg}...') >> install_packages.py
echo         subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--user', pkg]) >> install_packages.py
echo     print('Successfully installed all packages from requirements.txt') >> install_packages.py
echo except Exception as e: >> install_packages.py
echo     print(f'Error reading requirements.txt: {e}') >> install_packages.py
echo     print('Installing essential packages directly...') >> install_packages.py
echo     essential_pkgs = ['python-dotenv', 'requests', 'pandas', 'numpy', 'pyjwt', 'websocket-client', 'websockets', 'PyYAML', 'loguru'] >> install_packages.py
echo     for pkg in essential_pkgs: >> install_packages.py
echo         print(f'Installing {pkg}...') >> install_packages.py
echo         subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--user', pkg]) >> install_packages.py
echo     print('Successfully installed essential packages') >> install_packages.py

REM 임시 Python 스크립트 실행
echo "Installing required packages..."
python install_packages.py

REM 임시 스크립트 삭제
echo "Cleaning up..."
if exist install_packages.py del install_packages.py

REM 날짜 형식 (YYYY-MM-DD) 설정
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /format:list') do set datetime=%%I
set year=%datetime:~0,4%
set month=%datetime:~4,2%
set day=%datetime:~6,2%
set today=%year%-%month%-%day%
echo "Today's date: !today!"

REM 해당 날짜의 로그 폴더가 이미 있으면 유지, 없으면 생성
if exist "logs\!today!" (
    echo "Today's log folder (!today!) already exists. Using existing folder..."
) else (
    echo "Creating new log folder for today (!today!)..."
)

REM 로그 폴더 확인
if not exist logs mkdir logs
if not exist "logs\!today!" mkdir "logs\!today!"

REM 메인 프로그램 실행 - 업데이트 옵션 없이 실행
python main.py

REM 종료 대기
pause

endlocal 