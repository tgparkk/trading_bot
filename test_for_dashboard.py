"""
start_full_dashboard.bat 환경에서 텔레그램 메시지 전송 테스트
"""
import os
import sys
import asyncio
import aiohttp
from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path

# 현재 디렉토리 출력
print(f"현재 실행 디렉토리: {os.getcwd()}")
print(f"현재 스크립트 경로: {__file__}")

# .env 파일 경로 확인
env_path = Path(".env")
print(f".env 파일 존재 여부: {env_path.exists()}")

# 환경 변수 로드
load_dotenv()

# 텔레그램 설정 가져오기
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

print(f"텔레그램 토큰: {TELEGRAM_TOKEN[:10]}... (앞 10자리만 표시)")
print(f"텔레그램 채팅 ID: {TELEGRAM_CHAT_ID}")

# 텔레그램 API URL
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

async def test_telegram_message():
    """텔레그램 메시지 전송 테스트"""
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message = f"""
*대시보드 텔레그램 테스트*
시간: {current_time}

이 메시지가 보이면 start_full_dashboard.bat 환경과 유사한 조건에서 
텔레그램 메시지 전송에 성공한 것입니다.
    """
    
    print("\n[1] 텔레그램 메시지 전송 시도...")
    try:
        # 비동기 HTTP 클라이언트 세션 생성
        async with aiohttp.ClientSession() as session:
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown"
            }
            
            print(f"[2] API 요청 시작: {TELEGRAM_API_URL}")
            async with session.post(TELEGRAM_API_URL, json=payload, timeout=10) as response:
                print(f"[3] 응답 상태 코드: {response.status}")
                
                if response.status == 200:
                    response_json = await response.json()
                    print(f"[4] 응답 내용: {response_json}")
                    
                    if response_json.get("ok"):
                        print("\n✅ 텔레그램 메시지 전송 성공!")
                        return True
                    else:
                        print(f"\n❌ 텔레그램 API 오류: {response_json}")
                        return False
                else:
                    response_text = await response.text()
                    print(f"\n❌ HTTP 오류: {response.status}, {response_text}")
                    return False
    except Exception as e:
        print(f"\n❌ 예외 발생: {str(e)}")
        return False

# 백엔드 서버가 시작되는 것과 유사한 방식으로 프로세스 시작
async def simulate_dashboard_start():
    print("\n=== Dashboard 시작 시뮬레이션 ===")
    print("1. 백엔드 서버 시작 시뮬레이션...")
    
    # 텔레그램 메시지 전송 테스트 (성공하면 True, 실패하면 False 반환)
    telegram_success = await test_telegram_message()
    
    # 성공했는지 출력
    if telegram_success:
        print("\n✅ 전체 테스트 성공!")
        print("텔레그램 메시지 전송이 정상적으로 작동합니다.")
    else:
        print("\n❌ 테스트 실패!")
        print("텔레그램 메시지 전송에 문제가 있습니다.")
    
    return telegram_success

if __name__ == "__main__":
    # 이벤트 루프 실행
    print("=" * 50)
    print("Dashboard 환경 텔레그램 전송 테스트")
    print("=" * 50)
    
    asyncio.run(simulate_dashboard_start()) 