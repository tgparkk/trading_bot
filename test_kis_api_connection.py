"""
KIS API 접속 테스트 스크립트
"""
import asyncio
import sys
from datetime import datetime
from core.api_client import api_client
from monitoring.telegram_bot_handler import telegram_bot_handler
from utils.logger import logger

async def test_kis_api_connection():
    """KIS API 접속 테스트"""
    print("="*50)
    print("KIS API 접속 테스트 시작")
    print("="*50)
    
    try:
        # 텔레그램 봇 핸들러 준비 대기
        print("텔레그램 봇 핸들러 준비 대기 중...")
        await telegram_bot_handler.wait_until_ready(timeout=10)
        print("텔레그램 봇 핸들러 준비 완료")
        
        # KIS API 접속 시도 전 메시지 전송
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pre_message = f"""
        *KIS API 접속 시도* 🔄
        시도 시간: {current_time}
        
        한국투자증권 API 서버에 접속을 시도합니다.
        """
        
        print("KIS API 접속 시도 전 메시지 전송 중...")
        await telegram_bot_handler.send_message(pre_message)
        print("KIS API 접속 시도 전 메시지 전송 완료")
        
        # 접속 시도 시간 기록을 위해 1초 대기
        await asyncio.sleep(1)
        
        # KIS API 접속 시도
        print("KIS API 접속 시도 (계좌 잔고 조회)...")
        result = api_client.get_account_balance()
        print(f"KIS API 응답 결과: {result.get('rt_cd')}")
        
        # 현재 시간 갱신
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 결과 확인 및 메시지 전송
        if result and result.get("rt_cd") == "0":
            # 성공 메시지
            print("KIS API 접속 성공")
            success_message = f"""
            *KIS API 접속 성공* ✅
            접속 시간: {current_time}
            
            한국투자증권 API 서버에 성공적으로 접속했습니다.
            응답 메시지: {result.get("msg1", "정상")}
            """
            
            print("KIS API 접속 성공 메시지 전송 중...")
            await telegram_bot_handler.send_message(success_message)
            print("KIS API 접속 성공 메시지 전송 완료")
            return True
        else:
            # 실패 메시지
            error_msg = result.get("msg1", "알 수 없는 오류") if result else "응답 없음"
            print(f"KIS API 접속 실패: {error_msg}")
            
            fail_message = f"""
            *KIS API 접속 실패* ❌
            시도 시간: {current_time}
            
            한국투자증권 API 서버 접속에 실패했습니다.
            오류: {error_msg}
            """
            
            print("KIS API 접속 실패 메시지 전송 중...")
            await telegram_bot_handler.send_message(fail_message)
            print("KIS API 접속 실패 메시지 전송 완료")
            return False
            
    except Exception as e:
        # 예외 발생 시 메시지
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"오류 발생: {str(e)}")
        logger.log_error(e, "KIS API 접속 중 예외 발생")
        
        error_message = f"""
        *KIS API 접속 중 오류 발생* ❌
        시도 시간: {current_time}
        
        한국투자증권 API 서버 접속 중 예외가 발생했습니다.
        오류 내용: {str(e)}
        """
        
        try:
            print("KIS API 접속 오류 메시지 전송 중...")
            await telegram_bot_handler.send_message(error_message)
            print("KIS API 접속 오류 메시지 전송 완료")
        except Exception as msg_error:
            print(f"텔레그램 메시지 전송 실패: {str(msg_error)}")
            logger.log_error(msg_error, "KIS API 접속 오류 메시지 전송 실패")
        
        return False

async def main():
    """메인 실행 함수"""
    # 텔레그램 봇 폴링 시작
    print("텔레그램 봇 폴링 시작...")
    telegram_task = asyncio.create_task(telegram_bot_handler.start_polling())
    
    # 잠시 대기하여 텔레그램 봇이 초기화될 시간을 줍니다
    await asyncio.sleep(2)
    
    # KIS API 접속 테스트
    result = await test_kis_api_connection()
    
    # 결과 출력
    if result:
        print("\n✅ KIS API 접속 테스트 성공!")
    else:
        print("\n❌ KIS API 접속 테스트 실패!")
    
    # 모든 작업이 완료될 때까지 잠시 대기
    await asyncio.sleep(5)
    
    # 프로그램 종료
    print("테스트 종료")
    return 0

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("사용자에 의해 중단됨")
        sys.exit(1)
    except Exception as e:
        print(f"오류 발생: {str(e)}")
        sys.exit(1) 