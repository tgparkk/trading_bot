"""
텔레그램 봇 백그라운드 실행 스크립트
"""
import sys
import asyncio
import signal
import aiohttp
import os
import json
from datetime import datetime
from pathlib import Path
from monitoring.telegram_bot_handler import telegram_bot_handler
from utils.logger import logger
from utils.database import db

# 종료 시그널 처리
shutdown_requested = False

def signal_handler(sig, frame):
    """종료 시그널 처리"""
    global shutdown_requested
    print("종료 요청 받음 (Ctrl+C)")
    shutdown_requested = True

# 시그널 핸들러 등록
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# 프로세스 락 파일 경로
LOCK_FILE = Path("telegram_bot.lock")

def check_bot_running():
    """봇이 이미 실행 중인지 확인하고, 실행 중이면 자동으로 종료"""
    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE, "r") as f:
                data = json.load(f)
                pid = data.get("pid")
                start_time = data.get("start_time")
                
                # PID가 여전히 활성 상태인지 확인
                try:
                    # psutil이 설치되어 있는지 확인
                    import psutil
                    if psutil.pid_exists(pid):
                        print(f"⚠️ 텔레그램 봇이 이미 실행 중입니다 (PID: {pid}, 시작 시간: {start_time})")
                        print("기존 프로세스를 종료하고 새로 시작합니다...")
                        
                        try:
                            # 프로세스 종료 시도
                            process = psutil.Process(pid)
                            process.terminate()  # SIGTERM 신호 전송
                            
                            # 최대 5초 동안 종료될 때까지 대기
                            process.wait(timeout=5)
                            print(f"✅ 이전 텔레그램 봇 프로세스(PID: {pid})가 성공적으로 종료되었습니다.")
                        except psutil.NoSuchProcess:
                            print(f"프로세스(PID: {pid})가 이미 종료되었습니다.")
                        except psutil.TimeoutExpired:
                            print(f"프로세스(PID: {pid}) 종료 시간 초과. 강제 종료를 시도합니다.")
                            try:
                                process.kill()  # SIGKILL 신호 전송 (강제 종료)
                                print(f"✅ 이전 텔레그램 봇 프로세스(PID: {pid})가 강제 종료되었습니다.")
                            except Exception as kill_error:
                                print(f"강제 종료 실패: {str(kill_error)}")
                                print("기존 인스턴스를 수동으로 종료한 후 다시 시도하세요.")
                                return True
                        except Exception as e:
                            print(f"프로세스 종료 중 오류 발생: {str(e)}")
                            print("기존 인스턴스를 수동으로 종료한 후 다시 시도하세요.")
                            return True
                except ImportError:
                    # psutil이 설치되지 않은 경우
                    print("⚠️ psutil 모듈이 설치되지 않아 기존 프로세스를 자동으로 종료할 수 없습니다.")
                    print("pip install psutil 명령으로 psutil을 설치하거나,")
                    print("기존 인스턴스를 수동으로 종료한 후 다시 시도하세요.")
                    return True
        except (json.JSONDecodeError, KeyError) as e:
            print(f"락 파일이 손상되었습니다: {e}")
            
        # 락 파일은 존재하지만 프로세스가 실행 중이 아니거나 종료된 경우, 락 파일 삭제
        LOCK_FILE.unlink(missing_ok=True)
        print("이전 락 파일을 삭제했습니다.")
        
    return False

def create_lock_file():
    """프로세스 락 파일 생성"""
    data = {
        "pid": os.getpid(),
        "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    
    with open(LOCK_FILE, "w") as f:
        json.dump(data, f)
    
    print(f"프로세스 락 파일 생성: {LOCK_FILE}")

def remove_lock_file():
    """프로세스 락 파일 제거"""
    if LOCK_FILE.exists():
        LOCK_FILE.unlink()
        print(f"프로세스 락 파일 제거: {LOCK_FILE}")

async def reset_telegram_webhook():
    """텔레그램 웹훅 초기화 - 409 충돌 문제 해결"""
    try:
        print("텔레그램 웹훅 초기화 중...")
        token = telegram_bot_handler.token
        base_url = f"https://api.telegram.org/bot{token}"
        
        async with aiohttp.ClientSession() as session:
            # 웹훅 삭제
            async with session.get(f"{base_url}/deleteWebhook") as response:
                data = await response.json()
                success = data.get("ok", False)
                
                if success:
                    print("✅ 웹훅 초기화 성공")
                    # 업데이트 초기화 (오프셋 리셋)
                    async with session.get(f"{base_url}/getUpdates", params={"offset": -1, "limit": 1}) as reset_response:
                        reset_data = await reset_response.json()
                        print(f"업데이트 초기화 결과: {reset_data}")
                        return True
                else:
                    print(f"❌ 웹훅 초기화 실패: {data}")
                    return False
                    
    except Exception as e:
        print(f"❌ 웹훅 초기화 중 오류: {str(e)}")
        logger.log_error(e, "텔레그램 웹훅 초기화 중 오류")
        return False

async def shutdown():
    """봇 종료 처리"""
    print("텔레그램 봇 종료 중...")
    telegram_bot_handler.bot_running = False
    # 활성 세션이 모두 정리될 때까지 5초 대기
    await asyncio.sleep(5)
    print("텔레그램 봇 종료 완료")

async def status_update():
    """주기적인 상태 업데이트 (60초마다)"""
    interval = 60
    counter = 0
    while not shutdown_requested:
        await asyncio.sleep(1)
        counter += 1
        if counter >= interval:
            counter = 0
            try:
                db.update_system_status("RUNNING", "텔레그램 봇 정상 실행 중")
                logger.log_system(f"상태 업데이트: 텔레그램 봇 정상 실행 중 ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})")
            except Exception as e:
                logger.log_error(e, "상태 업데이트 중 오류")

async def main():
    """메인 함수"""
    print("="*60)
    print("텔레그램 봇 백그라운드 실행 시작")
    print("="*60)
    
    # 이미 실행 중인지 확인
    if check_bot_running():
        return 1
    
    # 락 파일 생성
    create_lock_file()
    
    try:
        # 웹훅 초기화 (409 충돌 문제 해결)
        await reset_telegram_webhook()
        
        # 상태 업데이트 태스크
        status_task = asyncio.create_task(status_update())
        
        # 봇 시작
        polling_task = asyncio.create_task(telegram_bot_handler.start_polling())
        
        # 봇이 준비될 때까지 대기
        try:
            await telegram_bot_handler.wait_until_ready(timeout=30)
            print("✅ 텔레그램 봇 준비 완료")
            
            # 환영 메시지 전송
            welcome_message = f"""
*텔레그램 봇 서비스 시작* 🚀
시작 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

텔레그램 봇 서비스가 시작되었습니다.
사용 가능한 명령어 목록을 보려면 /help를 입력하세요.
"""
            await telegram_bot_handler.send_message(welcome_message)
            
        except asyncio.TimeoutError:
            print("⚠️ 텔레그램 봇 준비 시간 초과. 계속 진행합니다.")
        
        # 시스템 상태 업데이트
        db.update_system_status("RUNNING", "텔레그램 봇 서비스 시작됨")
        
        print("텔레그램 봇이 백그라운드에서 실행 중입니다. 종료하려면 Ctrl+C를 누르세요.")
        
        # 종료 요청이 있을 때까지 실행
        while not shutdown_requested:
            await asyncio.sleep(1)
            
        # 종료 처리
        print("종료 요청을 처리합니다...")
        status_task.cancel()
        await shutdown()
        
    except Exception as e:
        logger.log_error(e, "텔레그램 봇 실행 중 오류 발생")
        print(f"❌ 오류 발생: {str(e)}")
        return 1
    finally:
        # 락 파일 제거
        remove_lock_file()
    
    print("텔레그램 봇 서비스가 정상적으로 종료되었습니다.")
    return 0

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("사용자에 의해 중단됨")
        remove_lock_file()  # 종료 시 락 파일 제거
        sys.exit(0)
    except Exception as e:
        print(f"오류 발생: {str(e)}")
        remove_lock_file()  # 종료 시 락 파일 제거
        sys.exit(1) 