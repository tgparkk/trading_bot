@echo off
echo 주식 자동매매 프로그램 시작 스크립트 v1.0
echo =================================================

:: Python 환경 설정
set PYTHONPATH=%~dp0
set PYTHONUNBUFFERED=1

:: 프로그램 실행
python main.py

:: 종료 대기
pause
