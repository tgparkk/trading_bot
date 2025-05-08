"""
텔레그램 명령어 테스트 스크립트
"""
import asyncio
import time
import aiohttp
from datetime import datetime
from monitoring.telegram_bot_handler import telegram_bot_handler
from utils.logger import logger
from config.settings import config

async def test_webhook_reset():
    """텔레그램 웹훅 초기화 직접 테스트"""
    print("="*60)
    print("텔레그램 웹훅 초기화 테스트")
    print("="*60)
    
    try:
        token = telegram_bot_handler.token
        base_url = f"https://api.telegram.org/bot{token}"
        
        # 세션 생성
        async with aiohttp.ClientSession() as session:
            print("웹훅 상태 확인 중...")
            async with session.get(f"{base_url}/getWebhookInfo") as response:
                data = await response.json()
                print(f"웹훅 상태: {data}")
                
            print("웹훅 삭제 중...")
            async with session.get(f"{base_url}/deleteWebhook") as response:
                data = await response.json()
                print(f"웹훅 삭제 결과: {data}")
                
            print("웹훅 상태 재확인 중...")
            async with session.get(f"{base_url}/getWebhookInfo") as response:
                data = await response.json()
                print(f"웹훅 상태 (삭제 후): {data}")
        
        print("웹훅 초기화 테스트 완료")
        return True
    except Exception as e:
        print(f"❌ 웹훅 초기화 테스트 중 오류 발생: {str(e)}")
        logger.log_error(e, "웹훅 초기화 테스트 중 오류 발생")
        return False

# 봇 종료 처리를 위한 콜백 함수
async def shutdown_bot_callback():
    """텔레그램 봇 종료 콜백 함수"""
    print("봇 종료 콜백 함수 실행")
    print("시스템을 안전하게 종료하는 중...")
    await asyncio.sleep(2)  # 종료 작업 시뮬레이션
    print("시스템 종료 완료")

async def test_telegram_commands():
    """텔레그램 명령어 테스트"""
    print("="*60)
    print("텔레그램 명령어 테스트 시작")
    print("="*60)
    
    try:
        # 웹훅 초기화 테스트
        print("0. 웹훅 초기화 테스트")
        await test_webhook_reset()
        
        # 종료 콜백 설정
        print("1. 종료 콜백 함수 설정")
        telegram_bot_handler.set_shutdown_callback(shutdown_bot_callback)
        print("✅ 종료 콜백 함수 설정 완료")
        
        # 텔레그램 봇 핸들러 폴링 시작
        print("2. 텔레그램 봇 폴링 시작")
        polling_task = asyncio.create_task(telegram_bot_handler.start_polling())
        
        # 봇이 준비될 때까지 대기
        print("3. 텔레그램 봇 준비 대기 중...")
        await asyncio.wait_for(telegram_bot_handler.ready_event.wait(), timeout=10)
        print("✅ 텔레그램 봇 준비 완료")
        
        # 테스트 시작 메시지 전송
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        start_message = f"""
*텔레그램 명령어 테스트 시작* 🧪
테스트 시간: {current_time}

이 테스트는 텔레그램 봇이 명령어에 응답하는지 확인합니다.
"""
        
        print("4. 테스트 시작 메시지 전송 중...")
        await telegram_bot_handler._send_message(start_message)
        print("✅ 테스트 시작 메시지 전송 완료")
        
        # 잠시 대기
        await asyncio.sleep(2)
        
        # 도움말 명령어 테스트
        print("5. 도움말 명령어 테스트")
        help_message = await telegram_bot_handler.get_help([])
        await telegram_bot_handler._send_message(help_message)
        print("✅ 도움말 명령어 테스트 완료")
        
        # 잠시 대기
        await asyncio.sleep(2)
        
        # 상태 명령어 테스트
        print("6. 상태 명령어 테스트")
        status_message = await telegram_bot_handler.get_status([])
        await telegram_bot_handler._send_message(status_message)
        print("✅ 상태 명령어 테스트 완료")
        
        # 잠시 대기
        await asyncio.sleep(2)

        # 일시정지 명령어 테스트
        print("7. 일시정지(/pause) 명령어 테스트")
        pause_message = await telegram_bot_handler.pause_trading([])
        await telegram_bot_handler._send_message(pause_message)
        print("✅ 일시정지 명령어 테스트 완료")
        
        # 상태 확인하여 일시정지 상태인지 확인
        await asyncio.sleep(2)
        status_after_pause = await telegram_bot_handler.get_status([])
        await telegram_bot_handler._send_message(f"일시정지 후 상태:\n{status_after_pause}")
        
        # 잠시 대기
        await asyncio.sleep(2)
        
        # 재개 명령어 테스트
        print("8. 재개(/resume) 명령어 테스트")
        resume_message = await telegram_bot_handler.resume_trading([])
        await telegram_bot_handler._send_message(resume_message)
        print("✅ 재개 명령어 테스트 완료")
        
        # 상태 확인하여 일시정지 해제 상태인지 확인
        await asyncio.sleep(2)
        status_after_resume = await telegram_bot_handler.get_status([])
        await telegram_bot_handler._send_message(f"재개 후 상태:\n{status_after_resume}")
        
        # 잠시 대기
        await asyncio.sleep(2)
        
        # 종료 명령어 테스트 (실제로 종료하지는 않고 메시지만 보여줌)
        print("9. 종료(/stop) 명령어 테스트 - confirm 없이 호출")
        stop_confirmation = await telegram_bot_handler.stop_bot([])
        await telegram_bot_handler._send_message(f"종료 명령 확인 메시지:\n{stop_confirmation}")
        print("✅ 종료 명령어 확인 메시지 테스트 완료")
        
        # 잠시 대기
        await asyncio.sleep(2)
        
        # 테스트 완료 메시지
        test_end_message = f"""
*텔레그램 명령어 테스트 완료* ✅
완료 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}

다음 명령어들이 테스트되었습니다:
- /help - 도움말
- /status - 시스템 상태
- /pause - 거래 일시정지
- /resume - 거래 재개
- /stop (확인 메시지만)

이제 텔레그램 앱에서 직접 명령어를 테스트해 보세요.
"""
        print("10. 테스트 완료 메시지 전송 중...")
        await telegram_bot_handler._send_message(test_end_message)
        print("✅ 테스트 완료 메시지 전송 완료")
        
        # 봇이 폴링을 계속하도록 30초 대기 (이 시간 동안 사용자가 직접 테스트 가능)
        print("11. 30초 동안 대기 중 (이 시간 동안 텔레그램 앱에서 명령어 테스트 가능)...")
        for i in range(30, 0, -1):
            print(f"\r남은 시간: {i}초", end="")
            await asyncio.sleep(1)
        print("\r대기 완료!                ")
        
        # 봇 폴링 종료
        print("12. 텔레그램 봇 폴링 종료")
        telegram_bot_handler.bot_running = False
        # 폴링 태스크가 종료될 때까지 대기
        try:
            await asyncio.wait_for(polling_task, timeout=5)
        except asyncio.TimeoutError:
            print("! 폴링 태스크가 5초 내에 종료되지 않아 강제 취소됨")
            polling_task.cancel()
        
        print("="*60)
        print("텔레그램 명령어 테스트 완료!")
        print("="*60)
        
    except Exception as e:
        print(f"❌ 오류 발생: {str(e)}")
        logger.log_error(e, "텔레그램 명령어 테스트 중 오류 발생")
        return False
    
    return True

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(test_telegram_commands())
        if exit_code:
            print("테스트 성공")
        else:
            print("테스트 실패")
    except KeyboardInterrupt:
        print("사용자에 의해 중단됨")
    except Exception as e:
        print(f"오류 발생: {str(e)}") 