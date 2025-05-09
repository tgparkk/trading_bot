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
from strategies.combined_strategy import combined_strategy
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

# 테스트 모드 설정 (True: 항상 장 시간으로 간주, False: 실제 장 시간만 작동)
TEST_MODE = True
logger.log_system(f"시스템 시작: 테스트 모드 = {TEST_MODE} (True: 항상 장 시간으로 간주)")

# 플라스크 앱 초기화 (API 서버용)
app = Flask(__name__)

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
            
            # 웹소켓 연결 - 재시도 추가
            websocket_connected = False
            retry_count = 0
            max_retries = 3
            
            while not websocket_connected and retry_count < max_retries:
                retry_count += 1
                try:
                    logger.log_system(f"웹소켓 연결 시도... ({retry_count}/{max_retries})")
                    await ws_client.connect()
                    websocket_connected = ws_client.is_connected()
                    if websocket_connected:
                        logger.log_system("웹소켓 연결 성공!")
                        break
                except Exception as e:
                    logger.log_error(e, f"웹소켓 연결 실패 ({retry_count}/{max_retries})")
                
                if retry_count < max_retries:
                    await asyncio.sleep(2)  # 재시도 전 2초 대기
            
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
            
            # 백엔드 서버 준비 완료 로그
            logger.log_system("=== 백엔드 서버 준비 완료 - 트레이딩 프로세스 시작 ===")
            
            # 메인 루프
            logger.log_system("메인 루프 시작 - 10초마다 상태 확인, 주기적 종목 스캔 실행")

            # 강제로 자동 종목 스캔 실행 (루프 시작 전)
            logger.log_system("=== 최초 자동 종목 스캔 강제 실행 ===")
            logger.log_trade(
                action="FORCE_AUTO_SCAN_START",
                symbol="SYSTEM",
                price=0,
                quantity=0,
                reason=f"루프 시작 전 최초 자동 종목 스캔 강제 실행",
                time=datetime.now().strftime("%H:%M:%S"),
                status="RUNNING"
            )

            try:
                # 종목 스캔 실행
                force_symbols = await self._get_tradable_symbols()
                if force_symbols:
                    # 스캔 결과 로그
                    logger.log_system(f"[OK] 강제 자동 종목 스캔 완료: 총 {len(force_symbols)}개 종목 발견")
                    logger.log_system(f"상위 종목 10개: {', '.join(force_symbols[:10])}")
                    
                    # 종목 업데이트 및 전략 시작
                    await combined_strategy.update_symbols(force_symbols[:50])
                    
                    # 전략 시작 (이미 시작된 경우 무시됨)
                    if not combined_strategy.running:
                        logger.log_system("=== 통합 전략 시작 ===")
                        await combined_strategy.start(force_symbols[:50])
                        logger.log_system("=== 통합 전략 시작 완료 ===")
                        logger.log_trade(
                            action="STRATEGY_START",
                            symbol="SYSTEM",
                            price=0,
                            quantity=len(force_symbols[:50]),
                            reason=f"통합 전략 시작 완료",
                            watched_symbols=len(force_symbols[:50]),
                            time=datetime.now().strftime("%H:%M:%S"),
                            status="SUCCESS"
                        )
                    
                    last_symbol_search = datetime.now()  # 마지막 스캔 시간 업데이트
                    
                    logger.log_trade(
                        action="FORCE_AUTO_SCAN_COMPLETE",
                        symbol="SYSTEM",
                        price=0,
                        quantity=len(force_symbols[:50]),
                        reason=f"루프 시작 전 강제 자동 종목 스캔 완료",
                        top_symbols=", ".join(force_symbols[:10]) if force_symbols else "",
                        time=datetime.now().strftime("%H:%M:%S"),
                        status="SUCCESS"
                    )
                else:
                    logger.log_system(f"❌ 강제 자동 종목 스캔 실패 - 거래 가능 종목이 없습니다.")
            except Exception as e:
                logger.log_error(e, "강제 자동 종목 스캔 중 오류 발생")
            logger.log_system("=== 강제 자동 종목 스캔 작업 종료 ===")
            
            # 장 시작 후 경과 시간 체크용
            market_open_time = None
            
            # 첫 번째 루프 실행 여부를 추적하는 플래그
            first_loop_run = True
            
            # 메인 루프
            while self.running:
                current_time = datetime.now().time()
                current_datetime = datetime.now()
                
                # 장 시간 체크 - 명확한 로그 추가
                market_open = self._is_market_open(current_time)
                logger.log_system(f"메인 루프 체크 - 현재 시간: {current_time}, 장 시간 여부: {market_open}, 테스트 모드: {TEST_MODE}, 첫 루프: {first_loop_run}")
                
                if market_open:
                    # 장 오픈 시간 기록
                    if market_open_time is None:
                        market_open_time = current_datetime
                        logger.log_system("장이 열렸습니다. 초기 장 오픈 시간을 설정합니다.")
                    
                    # 장 시작 직후 2분 동안은 더 자주 업데이트
                    market_open_elapsed = (current_datetime - market_open_time).total_seconds()
                    is_market_opening_period = market_open_elapsed < 120  # 장 시작 2분 이내
                    
                    # 장 시작 직후 1분 간격, 이후 2분 간격으로 종목 재탐색 (주기 단축)
                    time_since_last_search = (current_datetime - last_symbol_search).total_seconds()
                    search_interval = 60 if is_market_opening_period else 120  # 1분 또는 2분
                    
                    # 매 루프마다 더 명확한 디버깅 로그 추가
                    logger.log_system(f"자동 종목 스캔 체크 - 마지막 스캔 이후 {int(time_since_last_search)}초 경과, 스캔 간격: {search_interval}초, 남은 시간: {max(0, search_interval-time_since_last_search)}초")
                    
                    # 첫 번째 루프이거나 주기적인 스캔 시간이 되었을 때 종목 스캔 실행
                    if first_loop_run or time_since_last_search >= search_interval:
                        # 확실하게 로그 추가
                        logger.log_system(f"=======================================")
                        if first_loop_run:
                            logger.log_system(f"🔄 첫 번째 루프에서 자동 종목 스캔 강제 실행 - 현재 시간: {current_time}")
                        else:
                            logger.log_system(f"🔄 자동 종목 스캔 실행 시작 - 간격: {search_interval}초, 현재 시간: {current_time}")
                        logger.log_system(f"=======================================")
                        
                        # 거래 로그에도 스캔 시작 기록
                        logger.log_trade(
                            action="AUTO_SCAN_START",
                            symbol="SYSTEM",
                            price=0,
                            quantity=0,
                            reason=f"자동 종목 스캔 시작 ({first_loop_run and '첫 번째 루프 강제 실행' or f'간격: {search_interval}초'})",
                            time=current_datetime.strftime("%H:%M:%S"),
                            status="RUNNING"
                        )
                        
                        # 첫 번째 루프 실행 후 flag 해제
                        first_loop_run = False
                        
                        try:
                            new_symbols = await self._get_tradable_symbols()
                            if new_symbols:
                                # combined_strategy에 업데이트된 종목 목록 전달
                                logger.log_system(f"[OK] 자동 종목 스캔 성공 - {len(new_symbols)}개 종목이 발견되었습니다.")
                                logger.log_system(f"상위 종목 10개: {', '.join(new_symbols[:10])}")
                                await combined_strategy.update_symbols(new_symbols[:50])
                                last_symbol_search = current_datetime
                                
                                # 확실하게 로그 추가
                                logger.log_system(f"=======================================")
                                logger.log_system(f"[OK] 자동 종목 스캔 완료 - 총 {len(new_symbols)}개 종목, 상위 50개 선택")
                                logger.log_system(f"=======================================")
                                
                                # 기록 강화 - trade.log에 스캔 결과 자세히 기록
                                top_symbols = ", ".join(new_symbols[:10]) if new_symbols else ""
                                logger.log_trade(
                                    action="AUTO_SCAN_COMPLETE",
                                    symbol="SYSTEM",
                                    price=0,
                                    quantity=len(new_symbols[:50]),
                                    reason=f"자동 종목 스캔 완료",
                                    scan_interval=f"{search_interval}초",
                                    market_phase=market_open_elapsed < 120 and "장 초반" or "장 중",
                                    top_symbols=top_symbols,
                                    time=current_datetime.strftime("%H:%M:%S"),
                                    status="SUCCESS"
                                )
                            else:
                                logger.log_system(f"❌ 자동 종목 스캔 실패 - 거래 가능 종목이 없습니다.")
                                # 다음 스캔 시간 설정 (실패 시 빨리 재시도)
                                last_symbol_search = current_datetime - timedelta(seconds=search_interval - 30)
                                logger.log_system(f"종목 스캔 실패로 30초 후 재시도 예정")
                                
                                # 거래 로그에 실패 기록
                                logger.log_trade(
                                    action="AUTO_SCAN_FAILED",
                                    symbol="SYSTEM",
                                    price=0,
                                    quantity=0,
                                    reason="거래 가능 종목 없음",
                                    time=current_datetime.strftime("%H:%M:%S"),
                                    status="FAILED"
                                )
                        except Exception as e:
                            logger.log_error(e, "자동 종목 스캔 중 오류 발생")
                            # 다음 스캔 시간 설정 (오류 시 빨리 재시도)
                            last_symbol_search = current_datetime - timedelta(seconds=search_interval - 30)
                            logger.log_system(f"종목 스캔 오류로 30초 후 재시도 예정")
                            
                            # 거래 로그에 오류 기록
                            logger.log_trade(
                                action="AUTO_SCAN_ERROR",
                                symbol="SYSTEM",
                                price=0,
                                quantity=0,
                                reason=f"종목 스캔 오류: {str(e)}",
                                time=current_datetime.strftime("%H:%M:%S"),
                                status="ERROR"
                            )
                    
                    # 포지션 체크
                    await order_manager.check_positions()
                    
                    # 시스템 상태 업데이트
                    db.update_system_status("RUNNING")
                    
                    # 주기적 상태 로깅 (1분마다)
                    current_minute = current_datetime.minute
                    if current_datetime.second < 10:  # 매 분 처음 10초 이내에만 실행
                        logger.log_system(f"시스템 실행 중 - 현재 시간: {current_time}, 장 시간: {self._is_market_open(current_time)}")
                    
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
            
            # 스캔 시작 로그 추가
            logger.log_system("종목 스캔 시작 - 거래 가능 종목 조회")
            
            # 홀수 분에는 코스피, 짝수 분에는 코스닥에서 종목 가져오기
            if current_minute % 2 == 0:
                market_type = "KOSDAQ"
            else:
                market_type = "KOSPI"
                
            logger.log_system(f"Searching tradable symbols from {market_type}")
            logger.log_trade(
                action="SYMBOL_SEARCH_START",
                symbol="SYSTEM",
                price=0,
                quantity=0,
                reason=f"종목 검색 시작 - {market_type}",
                market_type=market_type,
                time=datetime.now().strftime("%H:%M:%S")
            )
            
            # 지정된 시장에서 종목 가져오기 (최대 3회 재시도)
            symbols = []
            retry_count = 0
            max_retries = 3
            
            while not symbols and retry_count < max_retries:
                retry_count += 1
                try:
                    symbols = await stock_explorer.get_tradable_symbols(market_type=market_type)
                    if symbols:
                        logger.log_system(f"{market_type}에서 {len(symbols)}개 종목을 찾았습니다.")
                        break
                    else:
                        logger.log_system(f"{market_type}에서 종목을 찾지 못했습니다. 재시도... ({retry_count}/{max_retries})")
                except Exception as e:
                    logger.log_error(e, f"종목 검색 오류 발생. 재시도... ({retry_count}/{max_retries})")
                
                if retry_count < max_retries:
                    await asyncio.sleep(2)  # 재시도 전 2초 대기
            
            # 종목이 충분히 많지 않으면 다른 시장에서도 가져오기
            if len(symbols) < 20:
                other_market = "KOSPI" if market_type == "KOSDAQ" else "KOSDAQ"
                logger.log_system(f"Not enough symbols ({len(symbols)}), adding from {other_market}")
                logger.log_trade(
                    action="SYMBOL_SEARCH_EXTEND",
                    symbol="SYSTEM",
                    price=0,
                    quantity=len(symbols),
                    reason=f"종목수 부족({len(symbols)}개), {other_market}에서 추가 검색",
                    current_market=market_type,
                    additional_market=other_market
                )
                try:
                    additional_symbols = await stock_explorer.get_tradable_symbols(market_type=other_market)
                    if additional_symbols:
                        logger.log_system(f"{other_market}에서 추가로 {len(additional_symbols)}개 종목을 찾았습니다.")
                        symbols = list(set(symbols + additional_symbols))  # 중복 제거
                except Exception as e:
                    logger.log_error(e, f"{other_market}에서 추가 종목 검색 중 오류 발생")
            
            # 결과 로그 추가
            if symbols:
                logger.log_system(f"종목 스캔 완료: 총 {len(symbols)}개 거래 가능 종목 발견")
                logger.log_trade(
                    action="SYMBOL_SEARCH_RESULT",
                    symbol="SYSTEM",
                    price=0,
                    quantity=len(symbols),
                    reason=f"거래 가능 종목 스캔 완료",
                    markets=f"{market_type}+{other_market if len(symbols) < 20 else ''}",
                    top_symbols=", ".join(symbols[:5]) if symbols else "",
                    time=datetime.now().strftime("%H:%M:%S")
                )
            else:
                logger.log_system("거래 가능 종목을 찾지 못했습니다.")
                logger.log_trade(
                    action="SYMBOL_SEARCH_FAILED",
                    symbol="SYSTEM",
                    price=0,
                    quantity=0,
                    reason="거래 가능 종목 없음",
                    time=datetime.now().strftime("%H:%M:%S")
                )
            
            return symbols
            
        except Exception as e:
            logger.log_error(e, "Error in _get_tradable_symbols")
            return []
    
    def _is_market_open(self, current_time: time) -> bool:
        """장 시간 확인"""
        # 테스트 모드: 항상 장 시간으로 인식 (개발 및 디버깅용)
        test_mode = TEST_MODE
        
        # 중요: 로그 추가하여 테스트 모드 확인
        if test_mode:
            logger.log_system(f"테스트 모드 활성화: 현재 시간은 {current_time}이지만 장 시간으로 처리합니다. - 항상 True 반환")
            return True
        
        # 실제 장 시간 체크
        is_market_time = (
            self.trading_config.market_open <= current_time <= 
            self.trading_config.market_close
        )
        
        logger.log_system(f"시장 시간 체크: {current_time}, 장 시간 여부: {is_market_time}, 테스트 모드: {test_mode}")
        return is_market_time
    
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
            logger.log_system("Stopping combined strategy...")
            await combined_strategy.stop()
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
                    # 텔레그램 핸들러가 준비되어 있는지 확인 (예외 처리 추가)
                    try:
                        # 준비 이벤트 확인
                        logger.log_system("종료 알림 전송 전 텔레그램 핸들러 준비 확인...")
                        telegram_ready = telegram_bot_handler.is_ready()
                    except Exception as check_e:
                        logger.log_error(check_e, "텔레그램 핸들러 준비 확인 중 오류")
                        telegram_ready = False
                    
                    # 알림 전송 시도 (이벤트 루프 닫힘 오류에 대비한 예외 처리 추가)
                    if telegram_ready or is_important_message(shutdown_message):
                        logger.log_system(f"{message_type} 알림 전송 실행...")
                        try:
                            await telegram_bot_handler._send_message(shutdown_message)
                            logger.log_system(f"{message_type} 알림 전송 완료 (DB 저장 확인 필요)")
                            # 메시지 전송 후 잠시 대기하여 메시지 전송이 완료될 시간 제공
                            await asyncio.sleep(2)
                        except RuntimeError as e:
                            if "loop is closed" in str(e) or "Event loop is closed" in str(e):
                                logger.log_warning(f"이벤트 루프가 닫혀 {message_type} 알림 전송 실패")
                            else:
                                raise
                except Exception as e:
                    logger.log_error(e, f"{message_type} 알림 전송 실패")

            logger.log_system("Trading bot shutdown process completed.")
            return

        except Exception as e:
            logger.log_error(e, "Error during shutdown process")
            return

def is_important_message(message: str) -> bool:
    """중요 메시지 여부 확인 (오류, 경고, 종료 관련)"""
    important_keywords = [
        "❌", "⚠️", "오류", "실패", "error", "fail", "종료", "stop", "ERROR", "WARNING", "CRITICAL"
    ]
    return any(keyword in message for keyword in important_keywords)

async def main(force_update=False):
    """메인 함수"""
    bot = TradingBot()
    telegram_task = None
    exit_code = 0

    try:
        # 프로그램 시작 시 기본 로깅 테스트
        logger.log_system("프로그램 시작: 로깅 시스템 초기화 확인")
        logger.log_trade(
            action="STARTUP",
            symbol="SYSTEM",
            price=0,
            quantity=0,
            reason=f"프로그램 시작 - {datetime.now().strftime('%H:%M:%S')}"
        )
        
        # 텔레그램 봇 핸들러 설정 및 시작 (별도 태스크로)
        telegram_task = asyncio.create_task(telegram_bot_handler.start_polling())
        logger.log_system("텔레그램 봇 핸들러 시작됨 (백그라운드)")

        # 텔레그램 봇 핸들러가 준비될 때까지 대기 (타임아웃 확장)
        try:
            logger.log_system("텔레그램 봇 핸들러 준비 대기 시작...")
            await asyncio.wait_for(telegram_bot_handler.ready_event.wait(), timeout=30)  # 30초로 확장
            logger.log_system("텔레그램 봇 핸들러 준비 완료!")
            
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
                await telegram_bot_handler._send_message(start_message)
                logger.log_system("프로그램 시작 알림 전송 완료")
            except Exception as e:
                logger.log_error(e, "Failed to send start notification")
                
        except asyncio.TimeoutError:
            logger.log_error(Exception("텔레그램 봇 준비 시간 초과"), "텔레그램 봇 준비 타임아웃, 그래도 프로그램 계속 실행")
            # 텔레그램 초기화 실패해도 메인 로직은 계속 실행
        except Exception as e:
            logger.log_error(e, "텔레그램 봇 초기화 오류, 그래도 프로그램 계속 실행")
            # 텔레그램 초기화 실패해도 메인 로직은 계속 실행

        # 메인 봇 실행 (API 초기화 시도)
        logger.log_system("Starting main bot execution...")
        try:
            # API 접속 시도 (initialize 메소드 호출)
            await bot.initialize()
            logger.log_system("API 초기화 성공!")
            
            # API 접속 성공 알림 (텔레그램 사용 가능한 경우에만)
            if telegram_bot_handler.is_ready():
                kis_success_message = f"""
                *KIS API 접속 성공* [OK]
                접속 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                
                한국투자증권 API 서버에 성공적으로 접속했습니다.
                """
                await telegram_bot_handler._send_message(kis_success_message)
                logger.log_system("KIS API 접속 성공 알림 전송 완료")
            
            # 봇 실행 계속
            await bot.run()
            
        except Exception as e:
            logger.log_error(e, "메인 봇 실행 오류")
            
            # API 접속 실패 알림 (텔레그램 사용 가능한 경우에만)
            if telegram_bot_handler.is_ready():
                kis_fail_message = f"""
                *KIS API 접속 실패* ❌
                시도 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                
                한국투자증권 API 서버 접속에 실패했습니다.
                오류: {str(e)}
                """
                await telegram_bot_handler._send_message(kis_fail_message)
                logger.log_system("KIS API 접속 실패 알림 전송 완료")
            
            # 오류 발생해도 정상 종료 과정 진행
            
        logger.log_system("Main bot execution finished or failed.")
        # 종료 처리
        logger.log_system("Initiating shutdown...")
        try:
            await bot.shutdown()
        except Exception as e:
            logger.log_error(e, "봇 종료 중 오류 발생")
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
        # 텔레그램 종료 처리
        try:
            # 먼저 텔레그램 메시지 전송이 완료될 수 있도록 충분한 대기 시간 제공
            logger.log_system("텔레그램 메시지 전송 완료 대기 중...")
            await asyncio.sleep(5)
            
            # 봇 세션을 명시적으로 닫기 시도
            logger.log_system("텔레그램 봇 세션 닫기 시도...")
            try:
                # 이벤트 루프가 아직 살아있다면 세션을 명시적으로 닫기
                await telegram_bot_handler.close_session()
                # 세션이 완전히 닫힐 시간을 주기 위해 잠시 대기
                await asyncio.sleep(1)
            except Exception as session_error:
                logger.log_error(session_error, "텔레그램 봇 세션 종료 중 오류")
                
            # 텔레그램 태스크 정리 (최대 5초 대기)
            if telegram_task and not telegram_task.done():
                # 중요: 텔레그램 태스크를 취소하기 전에 마지막 메시지가 전송될 수 있도록 충분한 시간 제공
                logger.log_system("텔레그램 태스크 취소 전 마지막 메시지 전송을 위해 대기 중...")
                # 종료 메시지가 전송될 수 있도록 더 긴 시간 대기
                await asyncio.sleep(3)
                
                logger.log_system("Cancelling Telegram polling task...")
                telegram_task.cancel()
                
                # 종료될 때까지 최대 5초 대기
                try:
                    await asyncio.wait_for(telegram_task, timeout=5)
                    logger.log_system("Telegram polling task successfully cancelled.")
                except asyncio.CancelledError:
                    logger.log_system("Telegram polling task cancellation confirmed.")
                except asyncio.TimeoutError:
                    logger.log_warning("Telegram polling task cancellation timed out, but proceeding anyway.")
                except Exception as e:
                    logger.log_error(e, "Error during Telegram task cancellation")
        except Exception as e:
            logger.log_error(e, "Error cleaning up Telegram resources")
        
        logger.log_system(f"Main function exiting with code {exit_code}.")
        return exit_code

# 매수 조건 테스트 기능
async def test_buy_condition(symbol=None):
    """매수 조건 테스트 함수"""
    from strategies.combined_strategy import CombinedStrategy
    from core.api_client import api_client
    import os
    
    # 토큰 존재 여부 확인
    if not await api_client.ensure_token():
        print("토큰 발급에 실패했습니다.")
        return
    
    # 통합 전략 초기화
    strategy = CombinedStrategy()
    
    # 매수 기준 표시
    buy_threshold = strategy.params["buy_threshold"]
    min_agreement = strategy.params["min_agreement"]
    
    print("\n===== 매수 조건 테스트 =====")
    print(f"매수 기준 점수: {buy_threshold}")
    print(f"최소 동의 전략 수: {min_agreement}")
    print("===========================\n")
    
    # 테스트할 종목 리스트 가져오기
    if symbol:
        symbols = [symbol]
    else:
        # KOSPI 상위 10개 종목 가져오기
        kospi_top_symbols = await api_client.get_top_trading_volume_symbols(market="KOSPI", limit=10)
        # KOSDAQ 상위 10개 종목 가져오기
        kosdaq_top_symbols = await api_client.get_top_trading_volume_symbols(market="KOSDAQ", limit=10)
        
        symbols = kospi_top_symbols + kosdaq_top_symbols
        
    # 각 종목별 매수 조건 테스트
    for symbol in symbols[:10]:  # 최대 10개 종목만 테스트
        try:
            # 종목 기본 정보 가져오기
            symbol_info = await api_client.get_symbol_info(symbol)
            print(f"\n종목코드: {symbol} - {symbol_info.get('name', '알 수 없음')}")
            
            # 1일 가격 데이터 가져오기
            price_data = await api_client.get_daily_price_data(symbol, 20)  # 20일 데이터
            if not price_data:
                print("  가격 데이터를 가져오지 못했습니다.")
                continue
                
            # 가격 데이터 전략에 설정
            strategy.price_data[symbol] = price_data
            
            # 전략 신호 업데이트
            await strategy._update_signals(symbol)
                
            # 신호 계산
            score, direction, agreements = strategy._calculate_combined_signal(symbol)
            
            # 결과 출력
            print(f"  방향: {direction}, 점수: {score:.1f}/10.0")
            print(f"  동의 전략: 매수={agreements.get('BUY', 0)}, 매도={agreements.get('SELL', 0)}")
            
            # 매수 가능 여부
            buy_possible = direction == "BUY" and score >= buy_threshold
            if buy_possible:
                print(f"  매수 가능: 예 (기준점수 {buy_threshold} 이상)")
            else:
                if direction != "BUY":
                    print(f"  매수 가능: 아니오 (방향이 매수가 아님)")
                else:
                    print(f"  매수 가능: 아니오 (점수 {score:.1f} < 기준점수 {buy_threshold})")
            
            # 각 전략별 점수 상세 출력
            strategies = strategy.signals[symbol]['strategies']
            print("\n  전략별 점수:")
            print(f"  브레이크아웃: {strategies['breakout']['signal']:.1f} ({strategies['breakout']['direction']})")
            print(f"  모멘텀: {strategies['momentum']['signal']:.1f} ({strategies['momentum']['direction']})")
            print(f"  갭 전략: {strategies['gap']['signal']:.1f} ({strategies['gap']['direction']})")
            print(f"  VWAP: {strategies['vwap']['signal']:.1f} ({strategies['vwap']['direction']})")
            print(f"  거래량: {strategies['volume']['signal']:.1f} ({strategies['volume']['direction']})")
            
        except Exception as e:
            print(f"  오류 발생: {str(e)}")
            
    print("\n===== 테스트 완료 =====\n")

# 매수 점수 기준 동적 조정
async def set_buy_threshold(threshold):
    """매수 점수 기준 동적 조정"""
    from strategies.combined_strategy import CombinedStrategy
    
    # 통합 전략 인스턴스 가져오기
    strategy = CombinedStrategy()
    
    # 기존 값 저장
    old_threshold = strategy.params["buy_threshold"]
    
    # 새 값 설정
    strategy.params["buy_threshold"] = float(threshold)
    
    print(f"\n매수 점수 기준이 {old_threshold}에서 {threshold}로 변경되었습니다.\n")
    
    # 변경된 설정으로 매수 조건 테스트 실행
    await test_buy_condition()

# --test 옵션 추가
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
    
    # 명령행 인자 파싱
    import argparse
    parser = argparse.ArgumentParser(description="트레이딩 봇")
    parser.add_argument("--update", action="store_true", help="강제 업데이트 실행")
    parser.add_argument("--checklog", action="store_true", help="로그 테스트")
    parser.add_argument("--test", action="store_true", help="매수 조건 테스트")
    parser.add_argument("--symbol", type=str, help="테스트할 특정 종목 코드")
    parser.add_argument("--threshold", type=float, help="매수 점수 기준 조정")
    args = parser.parse_args()
    
    # 로거 초기화
    logger.initialize_with_config()
    
    # 테스트 모드인 경우 테스트 함수 실행
    if args.test:
        loop = asyncio.get_event_loop()
        if args.threshold:
            loop.run_until_complete(set_buy_threshold(args.threshold))
        else:
            loop.run_until_complete(test_buy_condition(args.symbol))
        sys.exit(0)
    
    # 이벤트 루프 명시적 관리 - 수동으로 이벤트 루프 생성 및 종료
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    exit_code = 1  # 기본 종료 코드는 오류로 설정
    
    try:
        # 메인 함수 실행 (업데이트 옵션 전달)
        loop.run_until_complete(main(force_update=args.update))
        exit_code = 0  # 정상 종료 시 종료 코드를 0으로 변경
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received.")
        logger.log_system("프로그램 종료 중...")
        exit_code = 0
    except Exception as e:
        logger.log_error(e, "Unexpected error in main loop")
    finally:
        logger.log_system("이벤트 루프 정리 중...")
        try:
            # 남은 모든 작업이 완료될 때까지 대기
            pending = asyncio.all_tasks(loop)
            if pending:
                logger.log_system(f"{len(pending)}개의 미완료 작업 정리 중...")
                # 텔레그램 메시지 전송이 완료될 수 있도록 시간 제공
                loop.run_until_complete(asyncio.sleep(5))
                
                # 남은 작업들 취소
                for task in pending:
                    task.cancel()
                
                # 모든 작업이 취소될 때까지 대기 (최대 10초)
                try:
                    loop.run_until_complete(asyncio.wait_for(
                        asyncio.gather(*pending, return_exceptions=True),
                        timeout=10
                    ))
                    logger.log_system("모든 작업 정리 완료")
                except asyncio.TimeoutError:
                    logger.log_system("일부 작업이 시간 내에 정리되지 않았으나 계속 진행합니다.", level="WARNING")
        except Exception as cleanup_error:
            logger.log_error(cleanup_error, "작업 정리 중 오류 발생")
        finally:
            logger.log_system("이벤트 루프 닫기...")
            loop.close()
            logger.log_system(f"프로그램 종료 (종료 코드: {exit_code}).")
            sys.exit(exit_code)