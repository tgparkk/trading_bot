"""
주식 자동매매 프로그램 메인
"""
import asyncio
import signal
import sys
import os
from pathlib import Path
from datetime import datetime, time, timedelta
from typing import List
from utils.dotenv_helper import dotenv_helper
from config.settings import config
from core.api_client import api_client
from core.websocket_client import ws_client
from core.order_manager import order_manager
from core.stock_explorer import stock_explorer
from strategies.scalping_strategy import scalping_strategy
from utils.logger import logger
from utils.database import db
from monitoring.alert_system import alert_system
from monitoring.telegram_bot_handler import telegram_bot_handler
from flask import Flask, render_template, jsonify

# .env 파일 로드
dotenv_helper.load_env()

# KIS API 계정 정보 로드 확인
print("Loaded KIS_ACCOUNT_NO:", dotenv_helper.get_value("KIS_ACCOUNT_NO", "NOT_SET"))

# 설정 로드 후 로거 초기화
logger.initialize_with_config()

class TradingBot:
    """자동매매 봇"""
    
    def __init__(self):
        self.running = False
        self.trading_config = config["trading"]
        
    async def initialize(self):
        """초기화"""
        try:
            logger.log_system("Initializing trading bot...")
            
            # DB 초기화
            db.update_system_status("INITIALIZING")
            
            # 주문 관리자 초기화
            await order_manager.initialize()
            
            # 웹소켓 연결
            await ws_client.connect()
            
            # 시스템 상태 업데이트
            db.update_system_status("RUNNING")
            
            # 시작 알림
            await alert_system.notify_system_status(
                "RUNNING", 
                "Trading bot initialized successfully"
            )
            
            logger.log_system("Trading bot initialized successfully")
            
        except Exception as e:
            logger.log_error(e, "Failed to initialize trading bot")
            await self.shutdown(error=str(e))
            raise
    
    async def run(self):
        """실행"""
        try:
            self.running = True
            await self.initialize()
            
            # 거래 가능 종목 조회
            symbols = await self._get_tradable_symbols()
            logger.log_system(f"Found {len(symbols)} tradable symbols")
            
            # 전략 시작
            await scalping_strategy.start(symbols[:50])  # 상위 50종목만
            
            # 마지막 종목 탐색 시간
            last_symbol_search = datetime.now()
            # 장 시작 후 경과 시간 체크용
            market_open_time = None
            
            # 메인 루프
            while self.running:
                current_time = datetime.now().time()
                current_datetime = datetime.now()
                
                # 장 시간 체크
                if self._is_market_open(current_time):
                    # 장 오픈 시간 기록
                    if market_open_time is None:
                        market_open_time = current_datetime
                        logger.log_system("Market opened, setting initial market open time")
                    
                    # 장 시작 직후 2분 동안은 더 자주 업데이트
                    market_open_elapsed = (current_datetime - market_open_time).total_seconds()
                    is_market_opening_period = market_open_elapsed < 120  # 장 시작 2분 이내
                    
                    # 장 시작 직후 2분 간격, 이후 3분 간격으로 종목 재탐색
                    time_since_last_search = (current_datetime - last_symbol_search).total_seconds()
                    search_interval = 120 if is_market_opening_period else 180  # 2분 또는 3분
                    
                    if time_since_last_search >= search_interval:
                        logger.log_system(f"Updating symbols (interval: {search_interval}s)")
                        new_symbols = await self._get_tradable_symbols()
                        if new_symbols:
                            symbols = new_symbols
                            await scalping_strategy.update_symbols(symbols[:50])
                            last_symbol_search = current_datetime
                            logger.log_system(f"Updated tradable symbols: {len(symbols)}")
                    
                    # 포지션 체크
                    await order_manager.check_positions()
                    
                    # 시스템 상태 업데이트
                    db.update_system_status("RUNNING")
                    
                else:
                    # 장 마감 처리
                    if current_time > self.trading_config.market_close:
                        await self._handle_market_close()
                    # 장이 닫히면 market_open_time 초기화
                    market_open_time = None
                
                await asyncio.sleep(10)  # 10초 대기
                
        except Exception as e:
            logger.log_error(e, "Trading bot error")
            await self.shutdown(error=str(e))
    
    async def _get_tradable_symbols(self) -> List[str]:
        """거래 가능 종목 조회 (필터링 포함)"""
        try:
            current_minute = datetime.now().minute
            
            # 홀수 분에는 코스피, 짝수 분에는 코스닥에서 종목 가져오기
            if current_minute % 2 == 0:
                market_type = "KOSDAQ"
            else:
                market_type = "KOSPI"
                
            logger.log_system(f"Searching tradable symbols from {market_type}")
            
            # 지정된 시장에서 종목 가져오기
            symbols = await stock_explorer.get_tradable_symbols(market_type=market_type)
            
            # 종목이 충분히 많지 않으면 다른 시장에서도 가져오기
            if len(symbols) < 20:
                other_market = "KOSPI" if market_type == "KOSDAQ" else "KOSDAQ"
                logger.log_system(f"Not enough symbols ({len(symbols)}), adding from {other_market}")
                additional_symbols = await stock_explorer.get_tradable_symbols(market_type=other_market)
                symbols = list(set(symbols + additional_symbols))  # 중복 제거
            
            return symbols
            
        except Exception as e:
            logger.log_error(e, "Error in _get_tradable_symbols")
            return []
    
    def _is_market_open(self, current_time: time) -> bool:
        """장 시간 확인"""
        return (
            self.trading_config.market_open <= current_time <= 
            self.trading_config.market_close
        )
    
    async def _handle_market_close(self):
        """장 마감 처리"""
        try:
            # 일일 요약
            summary = await order_manager.get_daily_summary()
            
            # 성과 기록
            db.save_performance(summary)
            
            # 일일 리포트 전송
            await alert_system.send_daily_report(summary)
            
            # 데이터베이스 백업
            db.backup_database()
            
            logger.log_system("Market closed. Daily process completed")
            
        except Exception as e:
            logger.log_error(e, "Market close handling error")
    
    async def shutdown(self, error: str = None):
        """종료"""
        logger.log_system(f"Shutdown called. Error: {error}")
        try:
            self.running = False
            logger.log_system("Stopping scalping strategy...")
            await scalping_strategy.stop()
            logger.log_system("Closing WebSocket connection...")
            await ws_client.close()

            shutdown_message = ""
            message_type = ""

            # 시스템 상태 업데이트 및 메시지 준비
            if error:
                logger.log_system(f"Updating system status to ERROR: {error}")
                db.update_system_status("ERROR", error)

                message_type = "오류 종료"
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                shutdown_message = f"""
                *주식 자동매매 프로그램 비정상 종료*
                종료 시간: {current_time}
                오류 내용: {error}

                프로그램이 오류로 인해 종료되었습니다.
                문제를 확인해주세요.
                """
            else:
                logger.log_system("Updating system status to STOPPED")
                db.update_system_status("STOPPED")

                message_type = "정상 종료"
                current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                shutdown_message = f"""
                *주식 자동매매 프로그램 정상 종료*
                종료 시간: {current_time}

                프로그램이 정상적으로 종료되었습니다.
                오늘도 수고하셨습니다! 👍
                """

            # 텔레그램 알림 시도
            if shutdown_message:
                logger.log_system(f"{message_type} 알림 전송 시도...")
                try:
                    # 텔레그램 핸들러 준비 대기 (최대 5초)
                    logger.log_system("종료 알림 전송 전 텔레그램 핸들러 준비 대기...")
                    await telegram_bot_handler.wait_until_ready(timeout=5)

                    # 텔레그램 메시지 전송 및 DB 저장 (await 추가)
                    logger.log_system(f"{message_type} 알림 전송 실행...")
                    await telegram_bot_handler.send_message(shutdown_message)
                    logger.log_system(f"{message_type} 알림 전송 완료 (DB 저장 확인 필요)")
                except asyncio.TimeoutError:
                     logger.log_warning("텔레그램 핸들러 준비 대기 시간 초과, 알림 전송 건너뜀.")
                except Exception as e:
                    logger.log_error(e, f"{message_type} 알림 전송 실패")

            logger.log_system("Trading bot shutdown process completed.")
            return

        except Exception as e:
            logger.log_error(e, "Error during shutdown process")
            return

async def main():
    """메인 함수"""
    bot = TradingBot()
    telegram_task = None
    exit_code = 0

    try:
        # 텔레그램 봇 핸들러 설정 및 시작
        telegram_task = asyncio.create_task(telegram_bot_handler.start_polling())

        # 텔레그램 봇 핸들러가 준비될 때까지 최대 10초 대기
        logger.log_system("텔레그램 봇 핸들러 준비 대기...")
        await telegram_bot_handler.wait_until_ready(timeout=10)

        # 프로그램 시작 알림 보내기
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        start_message = f"""
        *주식 자동매매 프로그램 시작*
        시작 시간: {current_time}

        자동매매 프로그램이 시작되었습니다.
        이제부터 거래 및 주요 이벤트에 대한 알림을 받게 됩니다.
        """

        try:
            # alert_system 호출 제거 확인!
            logger.log_system("프로그램 시작 알림 전송 시도...")
            await telegram_bot_handler.send_message(start_message)
            logger.log_system("프로그램 시작 알림 전송 완료 (DB 저장 확인 필요)")
        except Exception as e:
            logger.log_error(e, "Failed to send start notification")

        # KIS API 접속 시도 전 알림 메시지 전송
        try:
            kis_api_message = f"""
            *KIS API 접속 시도*
            시도 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            
            한국투자증권 API 서버에 접속을 시도합니다.
            """
            await telegram_bot_handler.send_message(kis_api_message)
            logger.log_system("KIS API 접속 시도 알림 전송 완료")
            
            # 알림 메시지가 확실히 전송될 수 있도록 짧은 대기 추가
            await asyncio.sleep(1)
        except Exception as e:
            logger.log_error(e, "KIS API 접속 시도 알림 전송 실패")

        # 메인 봇 실행 (API 초기화 시도)
        logger.log_system("Starting main bot execution...")
        try:
            # API 접속 시도 (initialize 메소드 호출)
            await bot.initialize()
            
            # API 접속 성공 알림
            kis_success_message = f"""
            *KIS API 접속 성공* ✅
            접속 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            
            한국투자증권 API 서버에 성공적으로 접속했습니다.
            """
            await telegram_bot_handler.send_message(kis_success_message)
            logger.log_system("KIS API 접속 성공 알림 전송 완료")
            
            # 봇 실행 계속
            await bot.run()
            
        except Exception as e:
            # API 접속 실패 알림
            kis_fail_message = f"""
            *KIS API 접속 실패* ❌
            시도 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            
            한국투자증권 API 서버 접속에 실패했습니다.
            오류: {str(e)}
            """
            await telegram_bot_handler.send_message(kis_fail_message)
            logger.log_system("KIS API 접속 실패 알림 전송 완료")
            
            # 메시지가 확실히 전송될 수 있도록 짧은 대기 추가
            await asyncio.sleep(2)
            
            raise  # 예외를 다시 발생시켜 정상적인 오류 처리 흐름 유지
            
        logger.log_system("Main bot execution finished normally.")
        # 정상 종료 시에도 shutdown 호출하여 정리 및 알림
        logger.log_system("Normal termination. Initiating shutdown...")
        await bot.shutdown()
        
        # 종료 메시지가 확실히 전송될 수 있도록 대기 추가
        await asyncio.sleep(2)
        
        exit_code = 0

    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received. Initiating shutdown...")
        if bot:
            await bot.shutdown() # 여기서 shutdown 완료까지 기다림
            # 종료 메시지가 확실히 전송될 수 있도록 대기 추가
            await asyncio.sleep(2)
        exit_code = 0
    except Exception as e:
        logger.log_error(e, "Unexpected error in main loop")
        if bot:
            logger.log_system("Attempting shutdown due to unexpected error...")
            await bot.shutdown(error=str(e)) # 여기서 shutdown 완료까지 기다림
            # 종료 메시지가 확실히 전송될 수 있도록 대기 추가
            await asyncio.sleep(2)
        exit_code = 1
    finally:
        logger.log_system("Main function finally block entered.")
        # 텔레그램 태스크 정리 (최대 5초 대기)
        if telegram_task and not telegram_task.done():
            logger.log_system("Cancelling Telegram polling task...")
            # 메시지 전송 작업을 완료할 수 있는 짧은 시간 제공
            await asyncio.sleep(3)
            telegram_task.cancel()
            try:
                # 종료될 때까지 최대 5초 대기
                await asyncio.wait_for(telegram_task, timeout=5)
                logger.log_system("Telegram polling task successfully cancelled.")
            except asyncio.CancelledError:
                logger.log_system("Telegram polling task cancellation confirmed.")
            except asyncio.TimeoutError:
                logger.log_warning("Telegram polling task cancellation timed out, but proceeding anyway.")
            except Exception as e:
                logger.log_error(e, "Error during Telegram task cancellation")

        logger.log_system(f"Exiting application with code {exit_code}.")
        sys.exit(exit_code)

if __name__ == "__main__":
    # 환경 변수 체크
    required_env_vars = [
        "KIS_BASE_URL",
        "KIS_APP_KEY",
        "KIS_APP_SECRET",
        "KIS_ACCOUNT_NO"
    ]
    
    # dotenv_helper를 사용해 필수 환경 변수 체크
    missing_vars = dotenv_helper.check_required_keys(required_env_vars)
    
    if missing_vars:
        print(f"Missing required environment variables: {', '.join(missing_vars)}")
        print("Please set these environment variables in .env file before running the bot.")
        
        # .env 파일이 없으면 생성 제안
        if not Path('.env').exists():
            create_env = input("Create sample .env file? (y/n): ")
            if create_env.lower() == 'y':
                dotenv_helper.create_sample_env()
                print("Sample .env file created. Please fill in the required values and run again.")
        
        sys.exit(1)
    
    # 이벤트 루프 실행
    asyncio.run(main())