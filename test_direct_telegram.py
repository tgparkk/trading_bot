"""
텔레그램 API 직접 테스트 스크립트
웹훅 초기화 및 텔레그램 봇과의 직접 통신 테스트
"""
import asyncio
import aiohttp
import json
from datetime import datetime
from config.settings import config
from utils.dotenv_helper import dotenv_helper
from utils.logger import logger

class TelegramDirectTest:
    """텔레그램 API 직접 테스트"""
    
    def __init__(self):
        # 설정에서 토큰과 채팅 ID 로드
        self.token = config["alert"].telegram_token
        self.chat_id = config["alert"].telegram_chat_id
        
        # 하드코딩된 기본값이거나 비어있는 경우 환경 변수에서 직접 로드
        if self.token == "your_telegram_bot_token" or not self.token:
            env_token = dotenv_helper.get_value("TELEGRAM_TOKEN")
            if env_token:
                self.token = env_token
                print(f"환경변수에서 텔레그램 토큰을 로드했습니다: {self.token[:10]}...")
        
        if self.chat_id == "your_chat_id" or not self.chat_id:
            env_chat_id = dotenv_helper.get_value("TELEGRAM_CHAT_ID")
            if env_chat_id:
                self.chat_id = env_chat_id
                print(f"환경변수에서 텔레그램 채팅 ID를 로드했습니다: {self.chat_id}")
        
        # 로그 남기기
        print(f"텔레그램 설정 - 토큰: {self.token[:10]}..., 채팅 ID: {self.chat_id}")
        
        # 텔레그램 API 기본 URL 설정
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.session = None
    
    async def initialize(self):
        """세션 초기화"""
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session
    
    async def close(self):
        """세션 종료"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
    
    async def reset_webhook(self):
        """웹훅 초기화"""
        await self.initialize()
        
        print("웹훅 상태 확인 중...")
        async with self.session.get(f"{self.base_url}/getWebhookInfo") as response:
            webhook_info = await response.json()
            print(f"웹훅 상태: {json.dumps(webhook_info, indent=2)}")
        
        print("웹훅 삭제 중...")
        async with self.session.get(f"{self.base_url}/deleteWebhook") as response:
            result = await response.json()
            print(f"웹훅 삭제 결과: {json.dumps(result, indent=2)}")
        
        print("오프셋 초기화 중...")
        async with self.session.get(f"{self.base_url}/getUpdates", params={"offset": -1, "limit": 1}) as response:
            result = await response.json()
            print(f"오프셋 초기화 결과: {json.dumps(result, indent=2)}")
    
    async def get_updates(self, offset=0, limit=100, timeout=30):
        """업데이트 조회"""
        await self.initialize()
        
        params = {
            "offset": offset,
            "limit": limit,
            "timeout": timeout
        }
        
        print(f"업데이트 요청: offset={offset}, limit={limit}")
        async with self.session.get(f"{self.base_url}/getUpdates", params=params) as response:
            result = await response.json()
            if result.get("ok"):
                updates = result.get("result", [])
                if updates:
                    print(f"업데이트 {len(updates)}개 수신:")
                    for update in updates:
                        print(f"- Update ID: {update.get('update_id')}")
                        message = update.get("message", {})
                        if message:
                            print(f"  Message ID: {message.get('message_id')}")
                            print(f"  From: {message.get('from', {}).get('first_name')} (ID: {message.get('from', {}).get('id')})")
                            print(f"  Text: {message.get('text')}")
                    
                    # 다음 오프셋 반환
                    return max(update["update_id"] for update in updates) + 1
                else:
                    print("수신된 업데이트 없음")
            else:
                print(f"업데이트 요청 실패: {result}")
            
            return offset
    
    async def send_message(self, text):
        """메시지 전송"""
        await self.initialize()
        
        params = {
            "chat_id": self.chat_id,
            "text": text,
        }
        
        print(f"메시지 전송 중: {text[:50]}...")
        async with self.session.post(f"{self.base_url}/sendMessage", json=params) as response:
            result = await response.json()
            if result.get("ok"):
                message = result.get("result", {})
                print(f"메시지 전송 성공 (ID: {message.get('message_id')})")
                return True
            else:
                print(f"메시지 전송 실패: {result}")
                return False
    
    async def test_help_command(self):
        """도움말 명령어 테스트"""
        help_text = """사용 가능한 명령어

조회 명령어
/status - 시스템 상태 조회
/positions - 보유 종목 조회
/balance - 계좌 잔고 조회
/performance - 성과 조회
/price - 종목 현재가 조회

거래 명령어
/buy - 종목 매수
/sell - 종목 매도
/close_all - 모든 포지션 청산
/scan - 종목 탐색 실행

제어 명령어
/pause - 자동 거래 일시정지
/resume - 자동 거래 재개
/stop - 프로그램 종료
/help - 도움말"""
        
        return await self.send_message(help_text)

async def run_direct_test():
    """텔레그램 직접 테스트 실행"""
    print("="*60)
    print("텔레그램 API 직접 테스트 시작")
    print("="*60)
    
    tester = TelegramDirectTest()
    
    try:
        # 웹훅 초기화
        print("\n1. 웹훅 초기화")
        await tester.reset_webhook()
        
        # 도움말 전송 테스트
        print("\n2. 도움말 메시지 전송 테스트")
        await tester.test_help_command()
        
        # 실시간 메시지 처리 테스트
        print("\n3. 실시간 메시지 처리 테스트 (60초)")
        print("텔레그램에서 봇에게 /help 등의 명령어를 보내보세요.")
        offset = 0
        start_time = datetime.now()
        end_time = start_time.timestamp() + 60  # 60초 동안 실행
        
        while datetime.now().timestamp() < end_time:
            offset = await tester.get_updates(offset=offset)
            print(f"대기 중... (남은 시간: {int(end_time - datetime.now().timestamp())}초)")
            await asyncio.sleep(5)  # 5초마다 업데이트 확인
        
        print("\n테스트 완료!")
        
    except Exception as e:
        print(f"테스트 중 오류 발생: {e}")
    finally:
        await tester.close()
        print("="*60)
        print("텔레그램 API 직접 테스트 종료")
        print("="*60)

if __name__ == "__main__":
    try:
        asyncio.run(run_direct_test())
    except KeyboardInterrupt:
        print("사용자에 의해 중단됨")
    except Exception as e:
        print(f"오류 발생: {str(e)}") 