#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
# TEST_MODE 환경 변수를 False로 설정
os.environ["TEST_MODE"] = "False"
print(f"TEST_MODE 환경 변수: {os.environ.get('TEST_MODE')}")

from monitoring.telegram_bot_handler import TelegramBotHandler
from core.api_client import api_client
import asyncio
import json
from utils.logger import logger

async def main():
    try:
        print("\n=== API 클라이언트 직접 호출 테스트 ===")
        # 토큰 재발급
        api_client._get_access_token()
        print("토큰이 새로 발급되었습니다.")
        
        # API 클라이언트 직접 호출
        account_info = await api_client.get_account_info()
        print(f"API 응답 코드: {account_info.get('rt_cd', 'N/A')}")
        print(f"API 응답 메시지: {account_info.get('msg1', 'N/A')}")
        print(f"API 응답 데이터: {json.dumps(account_info, indent=2, ensure_ascii=False)[:500]}...")
        
        print("\n=== 텔레그램 봇 핸들러 테스트 ===")
        bot = TelegramBotHandler()
        balance_msg = await bot.get_balance([])
        print("=== 계좌 정보 테스트 결과 ===")
        print(balance_msg)
        print("===========================")
    except Exception as e:
        print(f"오류 발생: {e}")

if __name__ == "__main__":
    asyncio.run(main()) 