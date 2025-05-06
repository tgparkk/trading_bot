"""
간단한 텔레그램 메시지 전송 테스트
최소한의 의존성으로 텔레그램 API를 직접 호출하여 메시지를 전송합니다.
"""
import asyncio
import aiohttp
import os
import json
from datetime import datetime
from config.settings import config
from utils.dotenv_helper import dotenv_helper

# 환경 변수 로드
dotenv_helper.load_env()

async def test_telegram():
    """텔레그램 메시지 전송 테스트"""
    print("텔레그램 메시지 전송 테스트 시작...")
    
    # 토큰과 채팅 ID 가져오기
    token = config["alert"].telegram_token
    chat_id = config["alert"].telegram_chat_id
    
    # 환경 변수에서 직접 로드 (필요한 경우)
    if token == "your_telegram_bot_token" or not token:
        token = os.getenv("TELEGRAM_TOKEN")
        print("환경 변수에서 토큰을 로드했습니다.")
    
    if chat_id == "your_chat_id" or not chat_id:
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        print("환경 변수에서 채팅 ID를 로드했습니다.")
    
    if not token or not chat_id:
        print("오류: 토큰 또는 채팅 ID가 설정되지 않았습니다.")
        return False
    
    # 모든 설정 출력
    print(f"토큰: {token[:4]}...{token[-4:]}")
    print(f"채팅 ID: {chat_id}")
    
    # API URL
    base_url = f"https://api.telegram.org/bot{token}"
    
    try:
        # 세션 생성
        async with aiohttp.ClientSession() as session:
            # 봇 정보 확인
            print("봇 정보 확인 중...")
            async with session.get(f"{base_url}/getMe") as response:
                if response.status != 200:
                    print(f"봇 정보 확인 실패: HTTP 상태 코드 {response.status}")
                    return False
                
                me_data = await response.json()
                if not me_data.get("ok"):
                    print(f"봇 정보 확인 실패: {me_data}")
                    return False
                
                bot_info = me_data.get("result", {})
                print(f"봇 사용자명: @{bot_info.get('username')}")
                print(f"봇 이름: {bot_info.get('first_name')}")
            
            # 웹훅 상태 확인
            print("웹훅 상태 확인 중...")
            async with session.get(f"{base_url}/getWebhookInfo") as response:
                if response.status != 200:
                    print(f"웹훅 상태 확인 실패: HTTP 상태 코드 {response.status}")
                else:
                    webhook_data = await response.json()
                    webhook_url = webhook_data.get("result", {}).get("url", "")
                    
                    if webhook_url:
                        print(f"웹훅 설정됨: {webhook_url}")
                        
                        # 웹훅 삭제
                        print("웹훅 삭제 중...")
                        async with session.get(f"{base_url}/deleteWebhook") as del_response:
                            del_data = await del_response.json()
                            if del_data.get("ok"):
                                print("웹훅 삭제 성공")
                            else:
                                print(f"웹훅 삭제 실패: {del_data}")
                    else:
                        print("웹훅이 설정되어 있지 않습니다.")
            
            # 메시지 전송 테스트
            print("테스트 메시지 전송 중...")
            test_message = f"""
<b>텔레그램 봇 테스트 메시지</b>

테스트 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
이 메시지가 보이면 텔레그램 봇이 정상적으로 작동하는 것입니다.

<i>HTML 형식 테스트</i>
<code>코드 블록 테스트</code>
<pre>프리포맷 테스트</pre>

✅ 테스트 성공!
"""
            
            params = {
                "chat_id": chat_id,
                "text": test_message,
                "parse_mode": "HTML"
            }
            
            async with session.post(f"{base_url}/sendMessage", json=params) as response:
                if response.status != 200:
                    print(f"메시지 전송 실패: HTTP 상태 코드 {response.status}")
                    response_text = await response.text()
                    print(f"응답: {response_text}")
                    return False
                
                send_data = await response.json()
                if not send_data.get("ok"):
                    print(f"메시지 전송 실패: {send_data}")
                    return False
                
                message_id = send_data.get("result", {}).get("message_id")
                print(f"메시지 전송 성공 (메시지 ID: {message_id})")
            
            # 메시지 수신 테스트
            print("메시지 수신 테스트 중...")
            params = {
                "offset": -1,
                "limit": 1,
                "timeout": 5
            }
            
            async with session.get(f"{base_url}/getUpdates", params=params) as response:
                if response.status != 200:
                    print(f"메시지 수신 테스트 실패: HTTP 상태 코드 {response.status}")
                    if response.status == 409:
                        print("409 충돌 오류 - 웹훅이 설정되어 있을 수 있습니다. 웹훅 설정을 확인하세요.")
                else:
                    updates_data = await response.json()
                    if updates_data.get("ok"):
                        updates = updates_data.get("result", [])
                        if updates:
                            print(f"{len(updates)}개의 업데이트를 받았습니다.")
                        else:
                            print("업데이트가 없습니다.")
                    else:
                        print(f"메시지 수신 테스트 실패: {updates_data}")
        
        print("텔레그램 메시지 전송 테스트 완료!")
        return True
    except Exception as e:
        print(f"오류 발생: {str(e)}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    asyncio.run(test_telegram()) 