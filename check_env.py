#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os

print("=== 환경 변수 확인 ===")
print(f"TEST_MODE: {os.environ.get('TEST_MODE', '설정되지 않음')}")
print(f"KIS_ACCOUNT_NO: {os.environ.get('KIS_ACCOUNT_NO', '설정되지 않음')}")
print(f"KIS_APP_KEY: {os.environ.get('KIS_APP_KEY', '설정되지 않음')}")
print(f"KIS_APP_SECRET: {'설정됨(보안상 표시하지 않음)' if os.environ.get('KIS_APP_SECRET') else '설정되지 않음'}")
print(f"KIS_BASE_URL: {os.environ.get('KIS_BASE_URL', '설정되지 않음')}")

# .env 파일에서도 확인
print("\n=== .env 파일에서 환경 변수 확인 ===")
from dotenv import load_dotenv
from pathlib import Path

env_path = Path(".") / ".env"
load_dotenv(dotenv_path=env_path)

print(f"TEST_MODE: {os.getenv('TEST_MODE', '설정되지 않음')}")
print(f"KIS_ACCOUNT_NO: {os.getenv('KIS_ACCOUNT_NO', '설정되지 않음')}")
print(f"KIS_APP_KEY: {os.getenv('KIS_APP_KEY', '설정되지 않음')}")
print(f"KIS_APP_SECRET: {'설정됨(보안상 표시하지 않음)' if os.getenv('KIS_APP_SECRET') else '설정되지 않음'}")
print(f"KIS_BASE_URL: {os.getenv('KIS_BASE_URL', '설정되지 않음')}")

# TEST_MODE를 False로 설정하고 api_client 테스트
print("\n=== TEST_MODE=False로 설정하고 테스트 ===")
os.environ["TEST_MODE"] = "False"
print(f"TEST_MODE 환경 변수: {os.environ.get('TEST_MODE')}")

from core.api_client import api_client
api_client._get_access_token()  # 토큰 재발급
print("토큰이 정상적으로 발급되었습니다.")

# API 호출 테스트
result = api_client.get_account_balance()
print(f"응답 코드: {result.get('rt_cd', 'N/A')}")
print(f"응답 메시지: {result.get('msg1', 'N/A')}") 