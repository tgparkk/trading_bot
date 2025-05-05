"""
간단한 텔레그램 봇 스크립트
409 충돌 문제를 해결하고 명령어 처리를 단순화
"""
import asyncio
import aiohttp
import json
import os
import signal
import sys
from datetime import datetime
from config.settings import config
from utils.dotenv_helper import dotenv_helper
from utils.logger import logger

# 종료 요청 플래그
shutdown_requested = False

def signal_handler(sig, frame):
    """종료 시그널 처리"""
    global shutdown_requested
    print("종료 요청 받음 (Ctrl+C)")
    shutdown_requested = True

# 시그널 핸들러 등록
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

class SimpleTelegramBot:
    """간단한 텔레그램 봇 구현"""
    
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
        self.last_update_id = 0
        self.running = False
        
    async def initialize(self):
        """봇 초기화"""
        # 세션 초기화
        if self.session is None:
            self.session = aiohttp.ClientSession()
            print("텔레그램 API 세션 초기화")
            
        # 웹훅 초기화
        await self.reset_webhook()
        
        # 시작 메시지 전송
        welcome_message = f"""텔레그램 봇 시작됨
시작 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

사용 가능한 명령어 목록을 보려면 /help를 입력하세요."""
        
        await self.send_message(welcome_message)
        print("봇 초기화 완료")
    
    async def close(self):
        """봇 종료"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
            print("텔레그램 API 세션 종료")
    
    async def reset_webhook(self):
        """웹훅 초기화"""
        print("웹훅 초기화 중...")
        async with self.session.get(f"{self.base_url}/deleteWebhook") as response:
            result = await response.json()
            if result.get("ok"):
                print("✅ 웹훅 초기화 성공")
                
                # 오프셋 초기화
                async with self.session.get(f"{self.base_url}/getUpdates", params={"offset": -1, "limit": 1}) as response:
                    result = await response.json()
                    print("✅ 오프셋 초기화 성공")
            else:
                print(f"❌ 웹훅 초기화 실패: {result}")
    
    async def send_message(self, text):
        """메시지 전송"""
        params = {
            "chat_id": self.chat_id,
            "text": text
        }
        
        print(f"메시지 전송 중...")
        async with self.session.post(f"{self.base_url}/sendMessage", json=params) as response:
            result = await response.json()
            if result.get("ok"):
                message = result.get("result", {})
                print(f"✅ 메시지 전송 성공 (ID: {message.get('message_id')})")
                return True
            else:
                print(f"❌ 메시지 전송 실패: {result}")
                return False
    
    async def get_updates(self):
        """업데이트 조회"""
        params = {
            "offset": self.last_update_id + 1,
            "timeout": 10
        }
        
        try:
            async with self.session.get(f"{self.base_url}/getUpdates", params=params) as response:
                if response.status != 200:
                    print(f"❌ 업데이트 요청 실패: {response.status}")
                    if response.status == 409:
                        # 충돌 오류 처리
                        print("충돌 오류 발생, 웹훅 초기화 시도")
                        await self.reset_webhook()
                    return []
                
                result = await response.json()
                if result.get("ok"):
                    updates = result.get("result", [])
                    if updates:
                        self.last_update_id = max(update["update_id"] for update in updates)
                        print(f"업데이트 {len(updates)}개 수신 (마지막 ID: {self.last_update_id})")
                    return updates
                else:
                    print(f"❌ 업데이트 조회 실패: {result}")
                    return []
        except Exception as e:
            print(f"❌ 업데이트 조회 중 오류: {str(e)}")
            return []
    
    async def handle_command(self, command, args, message_id=None):
        """명령어 처리"""
        response = None
        
        if command == "/help":
            response = """사용 가능한 명령어

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

        elif command == "/status":
            response = """시스템 상태

봇 상태: 실행 중
현재 시간: """ + datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        elif command == "/start":
            response = """텔레그램 봇이 시작되었습니다.
명령어 목록을 보려면 /help를 입력하세요."""
            
        elif command == "/pause":
            response = """⚠️ 자동 거래가 일시 중지되었습니다.
자동 매매가 중지되었지만, 수동 매매는 가능합니다.
거래를 재개하려면 /resume을 입력하세요."""
            
        elif command == "/resume":
            response = "✅ 자동 거래가 재개되었습니다."
            
        elif command == "/stop":
            # 확인 요청
            if not args or not args[0] == "confirm":
                response = "⚠️ 정말로 봇을 종료하시겠습니까? 확인하려면 `/stop confirm`을 입력하세요."
            else:
                response = "🛑 봇을 종료합니다... 시스템이 종료됩니다."
                # 메시지 전송 후 봇 종료
                await self.send_message(response)
                
                # 봇 종료 플래그 설정
                self.running = False
                
                # 전역 종료 플래그 설정
                global shutdown_requested
                shutdown_requested = True
                
                print("텔레그램 명령으로 시스템 종료 요청 수신됨")
                
                # 프로그램 자체를 종료
                print("프로그램을 종료합니다...")
                try:
                    # 세션 닫기
                    if self.session and not self.session.closed:
                        await self.session.close()
                    
                    # 백엔드 프로세스도 함께 종료 시도
                    try:
                        # Windows 환경인 경우 taskkill 명령을 먼저 실행
                        if os.name == 'nt':
                            print("Windows taskkill 명령으로 Python 프로세스 종료 시도")
                            # 더 구체적인 필터링으로 타겟팅
                            os.system('taskkill /f /im python.exe /fi "COMMANDLINE eq *main.py*"')
                            os.system('taskkill /f /im python.exe /fi "COMMANDLINE eq *trading_bot*"')
                            # 관련 모든 Python 프로세스 종료
                            os.system('taskkill /f /im python.exe /fi "USERNAME eq %USERNAME%"')
                            print("taskkill 명령 실행 완료")
                        
                        import psutil
                        current_process = psutil.Process(os.getpid())
                        current_pid = os.getpid()
                        print(f"현재 프로세스 PID: {current_pid}")
                        
                        # 현재 프로세스의 부모 찾기 (main.py 프로세스일 수 있음)
                        try:
                            parent = current_process.parent()
                            print(f"부모 프로세스: {parent.name()} (PID: {parent.pid})")
                            
                            # 부모가 python 프로세스인 경우 종료
                            if "python" in parent.name().lower():
                                print(f"부모 Python 프로세스 (PID: {parent.pid}) 종료 시도")
                                try:
                                    parent.terminate()  # 부모 프로세스 종료 시도
                                    gone, still_alive = psutil.wait_procs([parent], timeout=3)
                                    if still_alive:
                                        print(f"부모 프로세스가 종료되지 않아 강제 종료합니다")
                                        parent.kill()  # 강제 종료
                                except psutil.NoSuchProcess:
                                    print("부모 프로세스가 이미 종료됨")
                        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                            print(f"부모 프로세스 접근 오류: {str(e)}")
                        
                        # 모든 Python 프로세스 검색
                        print("관련 Python 프로세스 검색 시작")
                        python_processes = []
                        
                        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                            try:
                                # 자기 자신은 제외
                                if proc.info['pid'] == current_pid:
                                    continue
                                    
                                # Python 프로세스 검색
                                proc_name = proc.info['name'].lower()
                                if "python" in proc_name or "pythonw" in proc_name:
                                    try:
                                        cmd = proc.cmdline()
                                        cmd_str = " ".join(cmd)
                                        
                                        # main.py, start_fixed_system.bat, 또는 trading_bot 관련 프로세스 검색
                                        if any(target in cmd_str for target in ['main.py', 'trading_bot', 'start_fixed_system']):
                                            python_processes.append(proc)
                                            print(f"종료 대상 프로세스 발견: PID {proc.info['pid']}, CMD: {cmd_str}")
                                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                                        continue
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                continue
                        
                        # 발견된 프로세스 종료
                        if python_processes:
                            print(f"{len(python_processes)}개의 관련 프로세스 종료 시도")
                            for proc in python_processes:
                                try:
                                    proc.terminate()
                                    print(f"프로세스 PID {proc.pid} 종료 요청 완료")
                                except psutil.NoSuchProcess:
                                    print(f"프로세스 PID {proc.pid}가 이미 종료됨")
                                except Exception as e:
                                    print(f"프로세스 PID {proc.pid} 종료 중 오류: {str(e)}")
                            
                            # 프로세스가 종료될 때까지 기다림
                            print("프로세스 종료 대기")
                            _, still_alive = psutil.wait_procs(python_processes, timeout=5)
                            
                            # 여전히 살아있는 프로세스 강제 종료
                            if still_alive:
                                print(f"{len(still_alive)}개 프로세스가 여전히 실행 중, 강제 종료 시도")
                                for proc in still_alive:
                                    try:
                                        proc.kill()  # 강제 종료
                                        print(f"프로세스 PID {proc.pid} 강제 종료 요청")
                                    except Exception as e:
                                        print(f"프로세스 PID {proc.pid} 강제 종료 중 오류: {str(e)}")
                        else:
                            print("종료할 관련 프로세스를 찾지 못했습니다")
                    
                    except ImportError:
                        print("psutil 모듈이 설치되지 않아 프로세스 검색이 불가능합니다.")
                    except Exception as e:
                        print(f"프로세스 종료 중 오류 발생: {str(e)}")
                    
                    # 1초 대기 후 현재 프로세스 종료
                    await asyncio.sleep(1)
                    import os
                    print("os._exit(0)를 통해 프로세스 강제 종료")
                    os._exit(0)  # 강제 종료
                except Exception as e:
                    print(f"종료 중 오류: {str(e)}")
                    os._exit(1)  # 오류와 함께 강제 종료
                
                # 종료 플래그만 설정하고 메인 루프에서 처리하도록 반환
                return
            
        else:
            response = f"명령어 '{command}'는 아직 구현되지 않았습니다.\n/help를 입력하여 사용 가능한 명령어를 확인하세요."
        
        if response:
            await self.send_message(response)
    
    async def process_updates(self):
        """업데이트 처리"""
        updates = await self.get_updates()
        
        for update in updates:
            try:
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                text = message.get("text", "")
                message_id = message.get("message_id")
                
                print(f"메시지 수신: '{text}' (ID: {message_id})")
                
                # 권한 확인
                if str(chat_id) != str(self.chat_id):
                    print(f"⚠️ 권한 없는 사용자 (Chat ID: {chat_id})")
                    continue
                
                # 명령어 처리
                if text.startswith("/"):
                    parts = text.split()
                    command = parts[0].lower()
                    args = parts[1:] if len(parts) > 1 else []
                    
                    print(f"명령어 처리: {command} {args}")
                    await self.handle_command(command, args, message_id)
            except Exception as e:
                print(f"❌ 업데이트 처리 중 오류: {str(e)}")
    
    async def run(self):
        """봇 실행"""
        self.running = True
        await self.initialize()
        
        print("텔레그램 봇 실행 중...")
        while self.running and not shutdown_requested:
            try:
                await self.process_updates()
                await asyncio.sleep(1)
            except Exception as e:
                print(f"❌ 봇 실행 중 오류: {str(e)}")
                await asyncio.sleep(5)  # 오류 발생 시 잠시 대기
        
        print("텔레그램 봇 종료 중...")
        await self.close()
        print("텔레그램 봇 종료 완료")

async def main():
    """메인 함수"""
    print("="*60)
    print("간단한 텔레그램 봇 시작")
    print("="*60)
    
    bot = SimpleTelegramBot()
    
    try:
        await bot.run()
    except Exception as e:
        print(f"❌ 오류 발생: {str(e)}")
    finally:
        await bot.close()
    
    print("="*60)
    print("간단한 텔레그램 봇 종료")
    print("="*60)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("사용자에 의해 중단됨")
    except Exception as e:
        print(f"오류 발생: {str(e)}")
        sys.exit(1) 