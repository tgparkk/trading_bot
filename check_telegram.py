"""
텔레그램 봇 상태 진단 스크립트
텔레그램 봇의 기본 기능을 테스트합니다.
"""
import asyncio
import sys
import os
import json
import aiohttp
from datetime import datetime
from pathlib import Path
from config.settings import config
from utils.dotenv_helper import dotenv_helper
from utils.logger import logger
from monitoring.telegram_bot_handler import telegram_bot_handler

# 결과를 저장할 리스트
test_results = []

def print_step(step, passed=None):
    """테스트 단계 출력"""
    if passed is None:
        result = "⏳ 진행 중"
        color = "\033[93m"  # 노랑
    elif passed:
        result = "✅ 성공"
        color = "\033[92m"  # 초록
    else:
        result = "❌ 실패"
        color = "\033[91m"  # 빨강

    reset = "\033[0m"
    print(f"{color}[{result}]{reset} {step}")
    
    if passed is not None:
        test_results.append({"step": step, "passed": passed})

async def check_config():
    """설정 확인"""
    step = "텔레그램 설정 확인"
    print_step(step)
    
    token = config["alert"].telegram_token
    chat_id = config["alert"].telegram_chat_id
    
    # 토큰이 기본값이거나 비어 있으면 환경 변수에서 직접 읽기 시도
    if token == "your_telegram_bot_token" or not token:
        env_token = os.getenv("TELEGRAM_TOKEN")
        if env_token:
            token = env_token
            print("- 환경 변수에서 텔레그램 토큰을 로드했습니다.")
    
    # 채팅 ID가 기본값이거나 비어 있으면 환경 변수에서 직접 읽기 시도
    if chat_id == "your_chat_id" or not chat_id:
        env_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if env_chat_id:
            chat_id = env_chat_id
            print("- 환경 변수에서 텔레그램 채팅 ID를 로드했습니다.")
    
    token_ok = token and token != "your_telegram_bot_token"
    chat_id_ok = chat_id and chat_id != "your_chat_id"
    
    if token_ok:
        print(f"- 텔레그램 토큰: {token[:4]}...{token[-4:]}")
    else:
        print("- 텔레그램 토큰이 설정되지 않았습니다.")
    
    if chat_id_ok:
        print(f"- 텔레그램 채팅 ID: {chat_id}")
    else:
        print("- 텔레그램 채팅 ID가 설정되지 않았습니다.")
    
    print_step(step, token_ok and chat_id_ok)
    return token_ok and chat_id_ok, token, chat_id

async def check_api(token, chat_id):
    """텔레그램 API 확인"""
    step = "텔레그램 API 연결 확인"
    print_step(step)
    
    base_url = f"https://api.telegram.org/bot{token}"
    
    try:
        async with aiohttp.ClientSession() as session:
            # getMe로 봇 정보 조회
            print("- 봇 정보 조회 중...")
            async with session.get(f"{base_url}/getMe") as response:
                if response.status != 200:
                    print(f"- API 응답 오류: {response.status}")
                    print_step(step, False)
                    return False
                
                result = await response.json()
                if not result.get("ok"):
                    print(f"- API 응답 실패: {result}")
                    print_step(step, False)
                    return False
                
                bot_info = result.get("result", {})
                print(f"- 봇 이름: {bot_info.get('first_name')}")
                print(f"- 봇 사용자명: @{bot_info.get('username')}")
    
            # 테스트 메시지 전송
            print("- 테스트 메시지 전송 중...")
            test_message = f"📊 봇 진단 테스트 메시지\n테스트 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            
            params = {
                "chat_id": chat_id,
                "text": test_message,
                "parse_mode": "HTML"
            }
            
            async with session.post(f"{base_url}/sendMessage", json=params) as response:
                if response.status != 200:
                    print(f"- 메시지 전송 오류: {response.status}")
                    print_step(step, False)
                    return False
                
                result = await response.json()
                if not result.get("ok"):
                    print(f"- 메시지 전송 실패: {result}")
                    print_step(step, False)
                    return False
                
                print("- 테스트 메시지 전송 성공!")
        
        print_step(step, True)
        return True
    except Exception as e:
        print(f"- API 연결 중 오류: {str(e)}")
        print_step(step, False)
        return False

async def check_webhook(token):
    """웹훅 상태 확인"""
    step = "웹훅 상태 확인"
    print_step(step)
    
    base_url = f"https://api.telegram.org/bot{token}"
    
    try:
        async with aiohttp.ClientSession() as session:
            # 웹훅 정보 조회
            print("- 웹훅 정보 조회 중...")
            async with session.get(f"{base_url}/getWebhookInfo") as response:
                if response.status != 200:
                    print(f"- API 응답 오류: {response.status}")
                    print_step(step, False)
                    return False
                
                result = await response.json()
                if not result.get("ok"):
                    print(f"- API 응답 실패: {result}")
                    print_step(step, False)
                    return False
                
                webhook_info = result.get("result", {})
                url = webhook_info.get("url", "")
                
                if url:
                    print(f"- 웹훅 URL: {url}")
                    print("- 웹훅이 설정되어 있습니다. 이로 인해 폴링 모드에서 409 충돌이 발생할 수 있습니다.")
                    print("- 웹훅 초기화를 진행합니다...")
                    
                    # 웹훅 삭제
                    async with session.get(f"{base_url}/deleteWebhook") as del_response:
                        del_result = await del_response.json()
                        if del_result.get("ok"):
                            print("- 웹훅 초기화 성공")
                            print_step(step, True)
                            return True
                        else:
                            print(f"- 웹훅 초기화 실패: {del_result}")
                            print_step(step, False)
                            return False
                else:
                    print("- 웹훅이 설정되어 있지 않습니다.")
                    print_step(step, True)
                    return True
    except Exception as e:
        print(f"- 웹훅 확인 중 오류: {str(e)}")
        print_step(step, False)
        return False

async def check_simple_poll(token, chat_id):
    """간단한 폴링 테스트"""
    step = "폴링 모드 테스트"
    print_step(step)
    
    base_url = f"https://api.telegram.org/bot{token}"
    
    try:
        async with aiohttp.ClientSession() as session:
            # 업데이트 요청
            print("- 최근 메시지 폴링 중...")
            
            params = {
                "offset": -1,
                "timeout": 3,
                "limit": 5
            }
            
            async with session.get(f"{base_url}/getUpdates", params=params) as response:
                if response.status != 200:
                    print(f"- API 응답 오류: {response.status}")
                    print_step(step, False)
                    return False
                
                result = await response.json()
                if not result.get("ok"):
                    print(f"- API 응답 실패: {result}")
                    print_step(step, False)
                    return False
                
                updates = result.get("result", [])
                if updates:
                    print(f"- {len(updates)}개의 최근 메시지를 확인했습니다.")
                    last_update = updates[-1]
                    if "message" in last_update:
                        message = last_update["message"]
                        from_user = message.get("from", {}).get("first_name", "알 수 없음")
                        text = message.get("text", "")
                        print(f"- 마지막 메시지: {from_user}님의 \"{text}\"")
                else:
                    print("- 최근 메시지가 없습니다.")
        
        print_step(step, True)
        return True
    except Exception as e:
        print(f"- 폴링 테스트 중 오류: {str(e)}")
        print_step(step, False)
        return False

async def check_handler_init():
    """텔레그램 봇 핸들러 초기화 테스트"""
    step = "봇 핸들러 초기화 테스트"
    print_step(step)
    
    try:
        # ready_event 초기화
        telegram_bot_handler.ready_event = asyncio.Event()
        
        # 상태 초기화
        telegram_bot_handler.bot_running = True
        
        # 세션 초기화
        if telegram_bot_handler._session is None or telegram_bot_handler._session.closed:
            telegram_bot_handler._session = aiohttp.ClientSession()
            print("- 새 aiohttp 세션을 생성했습니다.")
        
        # 웹훅 초기화
        print("- 웹훅 초기화 중...")
        try:
            async with telegram_bot_handler._session.get(f"{telegram_bot_handler.base_url}/deleteWebhook") as response:
                result = await response.json()
                if result.get("ok"):
                    print("- 웹훅 초기화 성공")
                else:
                    print(f"- 웹훅 초기화 실패: {result}")
                    print_step(step, False)
                    return False
        except Exception as e:
            print(f"- 웹훅 초기화 중 오류: {str(e)}")
            print_step(step, False)
            return False
        
        # 테스트 메시지 전송
        print("- 테스트 메시지 전송 중...")
        test_message = f"📊 봇 핸들러 초기화 테스트\n테스트 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        message_id = await telegram_bot_handler._send_message(test_message)
        
        if message_id:
            print(f"- 메시지 전송 성공 (ID: {message_id})")
            print_step(step, True)
            return True
        else:
            print("- 메시지 전송 실패")
            print_step(step, False)
            return False
            
    except Exception as e:
        print(f"- 봇 핸들러 초기화 중 오류: {str(e)}")
        print_step(step, False)
        return False
    finally:
        # 테스트 종료 시 세션 정리
        if telegram_bot_handler._session and not telegram_bot_handler._session.closed:
            await telegram_bot_handler._session.close()
            telegram_bot_handler._session = None
            print("- 세션 정리 완료")

async def check_lock_file():
    """락 파일 확인"""
    step = "텔레그램 봇 락 파일 확인"
    print_step(step)
    
    lock_file = Path("telegram_bot.lock")
    
    if lock_file.exists():
        try:
            with open(lock_file, "r") as f:
                data = json.load(f)
                pid = data.get("pid")
                start_time = data.get("start_time")
                
                print(f"- 락 파일이 존재합니다.")
                print(f"- 프로세스 ID: {pid}")
                print(f"- 시작 시간: {start_time}")
                
                # 프로세스가 여전히 실행 중인지 확인
                try:
                    import psutil
                    if psutil.pid_exists(pid):
                        print(f"- 해당 프로세스가 여전히 실행 중입니다.")
                        print_step(step, False)
                        return False
                    else:
                        print(f"- 해당 프로세스는 더 이상 실행되지 않습니다.")
                        print("- 오래된 락 파일을 삭제합니다.")
                        lock_file.unlink()
                        print_step(step, True)
                        return True
                except ImportError:
                    print("- psutil 모듈이 설치되지 않아 프로세스 상태를 확인할 수 없습니다.")
                    print("- 락 파일을 수동으로 삭제해야 할 수 있습니다.")
                    print_step(step, False)
                    return False
        except (json.JSONDecodeError, KeyError) as e:
            print(f"- 락 파일이 손상되었습니다: {e}")
            print("- 손상된 락 파일을 삭제합니다.")
            lock_file.unlink(missing_ok=True)
            print_step(step, True)
            return True
    else:
        print("- 락 파일이 존재하지 않습니다.")
        print_step(step, True)
        return True

async def test_pause_resume():
    """pause/resume 명령 테스트"""
    step = "pause/resume 명령 테스트"
    print_step(step)
    
    # 필요한 임포트 구문
    from strategies.combined_strategy import pause, resume
    
    try:
        # pause 테스트
        print("- pause() 함수 호출 중...")
        pause_result = await pause()
        
        if pause_result:
            print("- pause() 성공!")
        else:
            print("- pause() 실패")
            print_step(step, False)
            return False
        
        # resume 테스트
        print("- resume() 함수 호출 중...")
        resume_result = await resume()
        
        if resume_result:
            print("- resume() 성공!")
        else:
            print("- resume() 실패")
            print_step(step, False)
            return False
        
        print_step(step, True)
        return True
    except Exception as e:
        print(f"- pause/resume 테스트 중 오류: {str(e)}")
        print_step(step, False)
        return False

async def main():
    """메인 함수"""
    print("="*60)
    print("텔레그램 봇 진단 도구")
    print("="*60)
    print("이 도구는 텔레그램 봇의 기본 기능을 테스트합니다.")
    print("="*60)
    
    try:
        # 설정 확인
        config_ok, token, chat_id = await check_config()
        if not config_ok:
            print("\n❌ 텔레그램 설정이 올바르지 않습니다. 설정을 확인하세요.")
            return 1
        
        # API 연결 확인
        api_ok = await check_api(token, chat_id)
        if not api_ok:
            print("\n❌ 텔레그램 API 연결에 실패했습니다.")
            return 1
        
        # 웹훅 상태 확인
        webhook_ok = await check_webhook(token)
        if not webhook_ok:
            print("\n❌ 웹훅 상태 확인에 실패했습니다.")
            return 1
        
        # 폴링 테스트
        poll_ok = await check_simple_poll(token, chat_id)
        if not poll_ok:
            print("\n❌ 폴링 테스트에 실패했습니다.")
            return 1
        
        # 락 파일 확인
        lock_ok = await check_lock_file()
        
        # 핸들러 초기화 테스트
        handler_ok = await check_handler_init()
        if not handler_ok:
            print("\n❌ 봇 핸들러 초기화에 실패했습니다.")
            return 1
        
        # pause/resume 테스트
        pause_resume_ok = await test_pause_resume()
        
        # 결과 요약
        print("\n"+"="*60)
        print("테스트 결과 요약")
        print("="*60)
        
        all_passed = True
        for result in test_results:
            status = "✅ 성공" if result["passed"] else "❌ 실패"
            print(f"{status} - {result['step']}")
            if not result["passed"]:
                all_passed = False
        
        if all_passed:
            print("\n✅ 모든 테스트가 성공적으로 완료되었습니다!")
            print("텔레그램 봇이 정상적으로 작동합니다.")
        else:
            print("\n⚠️ 일부 테스트에 실패했습니다.")
            print("문제를 해결한 후 다시 시도하세요.")
        
        return 0 if all_passed else 1
    except Exception as e:
        print(f"\n❌ 테스트 중 오류가 발생했습니다: {str(e)}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n사용자에 의해 테스트가 중단되었습니다.")
        sys.exit(130) 