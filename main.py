"""
주식 자동매매 프로그램 메인
"""
import asyncio
import signal
import sys
import os
import json
import time
from pathlib import Path
from datetime import datetime, time as datetime_time, timedelta
from typing import List

# Windows에서 asyncio 관련 경고 해결을 위한 패치
if sys.platform.startswith('win'):
    # ProactorEventLoop 관련 오류 방지 패치
    import asyncio
    from functools import wraps
    
    # _ProactorBasePipeTransport.__del__ 패치
    _ProactorBasePipeTransport_orig_del = asyncio.proactor_events._ProactorBasePipeTransport.__del__
    
    @wraps(_ProactorBasePipeTransport_orig_del)
    def _patched_del(self):
        try:
            _ProactorBasePipeTransport_orig_del(self)
        except RuntimeError as e:
            if str(e) != 'Event loop is closed':
                raise
    
    # 패치 적용
    asyncio.proactor_events._ProactorBasePipeTransport.__del__ = _patched_del

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

# 플라스크 앱 초기화 (API 서버용)
# app = Flask(__name__)

class TradingBot:
    """자동매매 봇"""
    
    def __init__(self):
        self.running = False
        self.trading_config = config["trading"]
        self.max_retries = self.trading_config.get("max_websocket_retries", 3)
        
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
            
            while not websocket_connected and retry_count < self.max_retries:
                retry_count += 1
                try:
                    logger.log_system(f"웹소켓 연결 시도... ({retry_count}/{self.max_retries})")
                    await ws_client.connect()
                    websocket_connected = ws_client.is_connected()
                    if websocket_connected:
                        logger.log_system("웹소켓 연결 성공!")
                        break
                except Exception as e:
                    logger.log_error(e, f"웹소켓 연결 실패 ({retry_count}/{self.max_retries})")
                
                if retry_count < self.max_retries:
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
                    logger.log_system(f"상위 종목 50개: {', '.join(force_symbols[:50])}")
                    
                    # 종목 업데이트 및 전략 시작
                    await combined_strategy.update_symbols(force_symbols[:50])
                    
                    # 전략 시작 (이미 시작된 경우 무시됨)
                    if not combined_strategy.running:
                        logger.log_system("=== 통합 전략 시작 ===")
                        try:
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
                        except Exception as strategy_start_error:
                            logger.log_error(strategy_start_error, "통합 전략 시작 중 오류 발생")
                            logger.log_trade(
                                action="STRATEGY_START_FAILED",
                                symbol="SYSTEM",
                                price=0,
                                quantity=0,
                                reason=f"통합 전략 시작 실패: {str(strategy_start_error)}",
                                time=datetime.now().strftime("%H:%M:%S"),
                                status="ERROR"
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
                    # 초기 스캔 실패 시 기본값 설정
                    last_symbol_search = datetime.now() - timedelta(minutes=5)  # 5분 전으로 설정하여 빠른 재시도 유도
            except Exception as e:
                logger.log_error(e, "강제 자동 종목 스캔 중 오류 발생")
                # 예외 발생 시 기본값 설정
                last_symbol_search = datetime.now() - timedelta(minutes=5)
            logger.log_system("=== 강제 자동 종목 스캔 작업 종료 ===")
            
            # 장 시작 후 경과 시간 체크용
            market_open_time = None
            
            # 첫 번째 루프 실행 여부를 추적하는 플래그
            first_loop_run = True
            
            # 메인 루프 시작 시간 기록
            main_loop_start_time = datetime.now()
            retry_count = 0
            max_retries = 3
            
            # 메인 루프
            while self.running:
                current_time = datetime.now().time()
                current_datetime = datetime.now()
                
                # 안정성을 위한 메인 루프 모니터링
                loop_uptime = (current_datetime - main_loop_start_time).total_seconds() / 60  # 분 단위
                if current_datetime.minute % 5 == 0 and current_datetime.second < 10:  # 5분마다 로깅
                    logger.log_system(f"메인 루프 안정성 체크: 업타임 {loop_uptime:.1f}분, 상태: 정상")
                
                try:
                    # 장 시간 체크 - 명확한 로그 추가
                    market_open = self._is_market_open(current_time)
                    logger.log_system(f"메인 루프 체크 - 현재 시간: {current_time}, 장 시간 여부: {market_open}, 첫 루프: {first_loop_run}")
                    
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
                                    # 성공 처리
                                    await self._handle_scan_success(
                                        new_symbols, 
                                        current_datetime, 
                                        search_interval, 
                                        market_open_elapsed
                                    )
                                    last_symbol_search = current_datetime
                                    retry_count = 0
                                else:
                                    # 실패 처리
                                    logger.log_system(f"❌ 자동 종목 스캔 실패 - 거래 가능 종목이 없습니다.")
                                    last_symbol_search = current_datetime - timedelta(seconds=search_interval - 30)
                                    logger.log_system(f"종목 스캔 실패로 30초 후 재시도 예정")
                                    
                                    # 거래 로그에 실패 기록
                                    self._log_scan_failure("거래 가능 종목 없음", current_datetime)
                                    
                                    # 실패 카운터 증가
                                    retry_count += 1
                                    if retry_count >= max_retries:
                                        retry_count = await self._handle_consecutive_failures(
                                            retry_count, 
                                            max_retries, 
                                            "종목 스캔 연속 실패"
                                        )
                            except Exception as e:
                                # 예외 처리
                                logger.log_error(e, "자동 종목 스캔 중 오류 발생")
                                last_symbol_search = current_datetime - timedelta(seconds=search_interval - 30)
                                logger.log_system(f"종목 스캔 오류로 30초 후 재시도 예정")
                                
                                # 거래 로그에 오류 기록
                                self._log_scan_failure(f"종목 스캔 오류: {str(e)}", current_datetime, status="ERROR")
                                
                                # 오류 카운터 증가
                                retry_count += 1
                                if retry_count >= max_retries:
                                    retry_count = await self._handle_consecutive_failures(
                                        retry_count, 
                                        max_retries, 
                                        "종목 스캔 연속 오류"
                                    )

                            # 아래에 필요한 헬퍼 메서드들 추가 (TradingBot 클래스 내부에 정의)
                        
                        # 포지션 체크 (예외 처리 추가)
                        try:
                            await order_manager.check_positions()
                        except Exception as position_error:
                            logger.log_error(position_error, "포지션 체크 중 오류 발생")
                        
                        # 시스템 상태 업데이트
                        try:
                            db.update_system_status("RUNNING")
                        except Exception as db_error:
                            logger.log_error(db_error, "시스템 상태 업데이트 중 오류 발생")
                        
                        # 주기적 상태 로깅 (1분마다)
                        if current_datetime.second < 10:  # 매 분 처음 10초 이내에만 실행
                            logger.log_system(f"시스템 실행 중 - 현재 시간: {current_time}, 장 시간: {self._is_market_open(current_time)}")
                    
                    else:
                        # 장 마감 처리
                        if current_time > self.trading_config.market_close:
                            await self._handle_market_close()
                        # 장이 닫히면 market_open_time 초기화
                        market_open_time = None
                    
                except Exception as loop_error:
                    # 메인 루프 내부 오류 처리
                    logger.log_error(loop_error, "메인 루프 내부 처리 중 오류 발생, 계속 진행합니다.")
                    # 안전한 대기 시간 추가
                    await asyncio.sleep(5)
                
                # 안전한 대기 - 예외 처리 추가
                try:
                    await asyncio.sleep(10)  # 10초 대기
                except Exception as sleep_error:
                    logger.log_error(sleep_error, "대기 중 오류 발생")
                    # 짧은 대기로 다시 시도
                    await asyncio.sleep(1)
                
        except Exception as e:
            logger.log_error(e, "Trading bot error")
            await self.shutdown(error=str(e))

    async def _handle_scan_success(self, symbols, current_datetime, search_interval, market_open_elapsed):
        """종목 스캔 성공 처리 - 로깅 및 종목 업데이트"""
        # 로그 출력
        logger.log_system(f"[OK] 자동 종목 스캔 성공 - {len(symbols)}개 종목이 발견되었습니다.")
        logger.log_system(f"상위 종목 10개: {', '.join(symbols[:10])}")
        
        # 종목 업데이트
        await combined_strategy.update_symbols(symbols[:50])
        
        # 추가 로그
        logger.log_system(f"=======================================")
        logger.log_system(f"[OK] 자동 종목 스캔 완료 - 총 {len(symbols)}개 종목, 상위 50개 선택")
        logger.log_system(f"=======================================")
        
        # 거래 로그 기록
        top_symbols = ", ".join(symbols[:10]) if symbols else ""
        logger.log_trade(
            action="AUTO_SCAN_COMPLETE",
            symbol="SYSTEM",
            price=0,
            quantity=len(symbols[:50]),
            reason=f"자동 종목 스캔 완료",
            scan_interval=f"{search_interval}초",
            market_phase=market_open_elapsed < 120 and "장 초반" or "장 중",
            top_symbols=top_symbols,
            time=current_datetime.strftime("%H:%M:%S"),
            status="SUCCESS"
        )

    def _log_scan_failure(self, reason, current_datetime, status="FAILED"):
        """스캔 실패 로그 기록"""
        action = "AUTO_SCAN_ERROR" if status == "ERROR" else "AUTO_SCAN_FAILED"
        logger.log_trade(
            action=action,
            symbol="SYSTEM",
            price=0,
            quantity=0,
            reason=reason,
            time=current_datetime.strftime("%H:%M:%S"),
            status=status
        )

    async def _handle_consecutive_failures(self, retry_count, max_retries, failure_type):
        """연속 실패 처리 - 토큰 갱신 시도"""
        logger.log_warning(f"{failure_type}, {max_retries}회 연속 발생, API 연결 문제가 의심됩니다.")
        
        # 토큰 강제 갱신 시도
        try:
            logger.log_system("API 토큰 강제 갱신 시도...")
            refresh_result = api_client.force_token_refresh()
            logger.log_system(f"토큰 갱신 결과: {refresh_result.get('status')} - {refresh_result.get('message')}")
            return 0  # 토큰 갱신 후 카운터 초기화
        except Exception as token_error:
            logger.log_error(token_error, "토큰 갱신 중 오류 발생")
            return retry_count  # 기존 카운터 유지
    
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
    
    def _is_market_open(self, current_time: datetime_time) -> bool:
        """장 시간 확인"""
    
        # 실제 장 시간 체크
        is_market_time = (
            self.trading_config.market_open <= current_time <= 
            self.trading_config.market_close
        )
        
        logger.log_system(f"시장 시간 체크: {current_time}, 장 시간 여부: {is_market_time}")
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
    # 초기 설정
    bot = TradingBot()
    telegram_task = None
    heartbeat_task = None
    exit_code = 0
    
    # 워치독 타이머 설정
    last_heartbeat = datetime.now()
    watchdog_interval = 30 * 60  # 30분 (초 단위)
    logger.log_system(f"워치독 타이머 설정: {watchdog_interval/60}분")

    try:
        # 1. 기본 로깅 테스트
        _log_startup_info()
        
        # 2. 텔레그램 봇 설정 및 시작
        telegram_task = await _setup_telegram_bot()
        
        # 3. 워치독 모니터링 설정
        heartbeat_task = _setup_watchdog_monitor(last_heartbeat, watchdog_interval)
        
        # 4. 메인 봇 실행
        await _run_trading_bot(bot, force_update)
        last_heartbeat = datetime.now()  # 하트비트 갱신
        
    except KeyboardInterrupt:
        logger.log_warning("KeyboardInterrupt received. Initiating shutdown...")
        await _graceful_shutdown(bot)
        exit_code = 0
    except Exception as e:
        logger.log_error(e, "Unexpected error in main loop")
        await _emergency_shutdown(bot, error=str(e))
        exit_code = 1
    finally:
        # 5. 정리 작업
        await _cleanup_resources(telegram_task, heartbeat_task)
        logger.log_system(f"Main function exiting with code {exit_code}.")
        return exit_code


# --- 헬퍼 함수 ---

def _log_startup_info():
    """프로그램 시작 로깅"""
    logger.log_system("프로그램 시작: 로깅 시스템 초기화 확인")
    logger.log_trade(
        action="STARTUP",
        symbol="SYSTEM",
        price=0,
        quantity=0,
        reason=f"프로그램 시작 - {datetime.now().strftime('%H:%M:%S')}"
    )


async def _setup_telegram_bot():
    """텔레그램 봇 설정 및 시작"""
    # 텔레그램 봇 태스크 생성
    telegram_task = asyncio.create_task(telegram_bot_handler.start_polling())
    logger.log_system("텔레그램 봇 핸들러 시작됨 (백그라운드)")
    
    # 텔레그램 봇 준비 대기
    try:
        logger.log_system("텔레그램 봇 핸들러 준비 대기 시작...")
        await asyncio.wait_for(telegram_bot_handler.ready_event.wait(), timeout=30)
        logger.log_system("텔레그램 봇 핸들러 준비 완료!")
        
        # 프로그램 시작 알림 전송
        await _send_startup_notification()
    except asyncio.TimeoutError:
        logger.log_error(Exception("텔레그램 봇 준비 시간 초과"), "텔레그램 봇 준비 타임아웃, 그래도 프로그램 계속 실행")
    except Exception as e:
        logger.log_error(e, "텔레그램 봇 초기화 오류, 그래도 프로그램 계속 실행")
        
    return telegram_task


async def _send_startup_notification():
    """프로그램 시작 알림 전송"""
    try:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        start_message = f"""
        *주식 자동매매 프로그램 시작*
        시작 시간: {current_time}

        자동매매 프로그램이 시작되었습니다.
        이제부터 거래 및 주요 이벤트에 대한 알림을 받게 됩니다.
        """

        logger.log_system("프로그램 시작 알림 전송 시도...")
        await telegram_bot_handler._send_message(start_message)
        logger.log_system("프로그램 시작 알림 전송 완료")
    except Exception as e:
        logger.log_error(e, "Failed to send start notification")


def _setup_watchdog_monitor(last_heartbeat, watchdog_interval):
    """워치독 모니터링 태스크 설정"""
    return asyncio.create_task(
        _heartbeat_monitor(last_heartbeat, watchdog_interval)
    )


async def _run_trading_bot(bot, force_update=False):
    """메인 봇 실행"""
    # API 초기화 시도
    logger.log_system("Starting main bot execution...")
    try:
        # 봇 초기화
        await bot.initialize()
        logger.log_system("API 초기화 성공!")
        
        # API 접속 성공 알림
        await _send_api_success_notification()
        
        # 봇 실행
        await bot.run()
    except Exception as e:
        logger.log_error(e, "메인 봇 실행 오류")
        
        # API 접속 실패 알림
        await _send_api_failure_notification(str(e))
        raise  # 상위 핸들러로 예외 전달


async def _send_api_success_notification():
    """API 접속 성공 알림 전송"""
    if telegram_bot_handler.is_ready():
        try:
            kis_success_message = f"""
            *KIS API 접속 성공* [OK]
            접속 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            
            한국투자증권 API 서버에 성공적으로 접속했습니다.
            """
            await telegram_bot_handler._send_message(kis_success_message)
            logger.log_system("KIS API 접속 성공 알림 전송 완료")
        except Exception as e:
            logger.log_error(e, "API 접속 성공 알림 전송 실패")


async def _send_api_failure_notification(error_message):
    """API 접속 실패 알림 전송"""
    if telegram_bot_handler.is_ready():
        try:
            kis_fail_message = f"""
            *KIS API 접속 실패* ❌
            시도 시간: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            
            한국투자증권 API 서버 접속에 실패했습니다.
            오류: {error_message}
            """
            await telegram_bot_handler._send_message(kis_fail_message)
            logger.log_system("KIS API 접속 실패 알림 전송 완료")
        except Exception as e:
            logger.log_error(e, "API 접속 실패 알림 전송 실패")


async def _graceful_shutdown(bot):
    """정상 종료 처리"""
    if bot:
        await bot.shutdown()
        # 종료 메시지가 확실히 전송될 수 있도록 대기
        await asyncio.sleep(2)


async def _emergency_shutdown(bot, error=None):
    """오류 발생 시 종료 처리"""
    if bot:
        logger.log_system("Attempting shutdown due to unexpected error...")
        await bot.shutdown(error=error)
        # 종료 메시지가 확실히 전송될 수 있도록 대기
        await asyncio.sleep(2)


async def _cleanup_resources(telegram_task=None, heartbeat_task=None):
    """자원 정리 작업"""
    logger.log_system("Main function finally block entered.")
    
    # 1. 텔레그램 정리
    await _cleanup_telegram(telegram_task)
    
    # 2. 워치독 정리
    await _cleanup_heartbeat_task(heartbeat_task)


async def _cleanup_telegram(telegram_task):
    """텔레그램 자원 정리"""
    if telegram_task is None:
        return
        
    try:
        # 메시지 전송 완료 대기
        logger.log_system("텔레그램 메시지 전송 완료 대기 중...")
        await asyncio.sleep(5)
        
        # 봇 세션 명시적 종료
        try:
            logger.log_system("텔레그램 봇 세션 닫기 시도...")
            await telegram_bot_handler.close_session()
            await asyncio.sleep(1)
        except Exception as session_error:
            if "Event loop is closed" in str(session_error):
                logger.log_system("이벤트 루프가 이미 닫혔습니다. 계속 진행합니다.")
            else:
                logger.log_error(session_error, "텔레그램 봇 세션 종료 중 오류")
            
        # 텔레그램 태스크 정리
        if not telegram_task.done():
            # 중요: 텔레그램 태스크를 취소하기 전에 마지막 메시지가 전송될 수 있도록 충분한 시간 제공
            logger.log_system("텔레그램 태스크 취소 전 마지막 메시지 전송을 위해 대기 중...")
            await asyncio.sleep(3)
            
            logger.log_system("Cancelling Telegram polling task...")
            telegram_task.cancel()
            
            # 종료될 때까지 최대 5초 대기
            try:
                await asyncio.wait_for(telegram_task, timeout=5)
                logger.log_system("Telegram polling task successfully cancelled.")
            except (asyncio.CancelledError, RuntimeError):
                logger.log_system("Telegram polling task cancellation confirmed.")
            except asyncio.TimeoutError:
                logger.log_warning("Telegram polling task cancellation timed out, but proceeding anyway.")
            except Exception as e:
                logger.log_error(e, "Error during Telegram task cancellation")
    except Exception as e:
        if "Event loop is closed" in str(e):
            logger.log_system("이벤트 루프가 이미 닫혔습니다. 정리 작업을 건너뜁니다.")
        else:
            logger.log_error(e, "Error cleaning up Telegram resources")


async def _cleanup_heartbeat_task(heartbeat_task):
    """워치독 모니터링 태스크 정리"""
    if heartbeat_task is None:
        return
        
    # 하트비트 태스크 정리
    if not heartbeat_task.done():
        logger.log_system("하트비트 모니터링 태스크 정리 중...")
        heartbeat_task.cancel()
        try:
            await asyncio.wait_for(heartbeat_task, timeout=3)
            logger.log_system("하트비트 모니터링 태스크 정리 완료")
        except (asyncio.CancelledError, RuntimeError, asyncio.TimeoutError) as ce:
            logger.log_system(f"하트비트 태스크 취소 중 예외 발생 (무시됨): {ce}")

# 하트비트 모니터링을 위한 비동기 함수
async def _heartbeat_monitor(last_heartbeat, interval):
    """워치독 타이머 역할의 하트비트 모니터링 함수"""
    try:
        while True:
            await asyncio.sleep(60)  # 1분마다 확인
            
            # 마지막 하트비트로부터 경과 시간 확인
            time_since_heartbeat = (datetime.now() - last_heartbeat).total_seconds()
            
            if time_since_heartbeat > interval:
                logger.log_error(
                    Exception(f"하트비트 타임아웃: {time_since_heartbeat/60:.1f}분 동안 응답 없음"),
                    "시스템 응답 없음 감지"
                )
                
                # 텔레그램 알림 전송 시도
                try:
                    if telegram_bot_handler.is_ready():
                        watchdog_message = f"""
                        ⚠️ *시스템 경고: 하트비트 타임아웃* ⚠️
                        
                        {time_since_heartbeat/60:.1f}분 동안 시스템 응답이 없습니다.
                        마지막 하트비트: {last_heartbeat.strftime('%Y-%m-%d %H:%M:%S')}
                        현재 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
                        
                        자동 복구 프로세스가 진행 중입니다.
                        시스템 로그를 확인하세요.
                        """
                        await telegram_bot_handler._send_message(watchdog_message)
                except Exception as e:
                    logger.log_error(e, "하트비트 타임아웃 알림 전송 실패")
                
                # 여기에 시스템 복구 로직 추가 가능
                # 예: 프로세스 재시작, API 토큰 갱신 등
                logger.log_system("하트비트 타임아웃으로 인한 복구 조치 시작...")
                
                # 토큰 갱신 시도
                try:
                    logger.log_system("API 토큰 강제 갱신 시도...")
                    refresh_result = api_client.force_token_refresh()
                    logger.log_system(f"토큰 갱신 결과: {refresh_result.get('status')} - {refresh_result.get('message')}")
                except Exception as e:
                    logger.log_error(e, "토큰 갱신 실패")
                
                # 하트비트 초기화 (복구 조치 후)
                last_heartbeat = datetime.now()
                logger.log_system("하트비트 타이머 초기화 완료")
            
            elif time_since_heartbeat > (interval * 0.8):
                # 타임아웃 임계값의 80%에 도달했을 때 경고
                logger.log_warning(f"하트비트 경고: {time_since_heartbeat/60:.1f}분 동안 응답 없음 (타임아웃 임계값: {interval/60}분)")
                
    except asyncio.CancelledError:
        logger.log_system("하트비트 모니터링 태스크가 취소되었습니다.")
    except Exception as e:
        logger.log_error(e, "하트비트 모니터링 중 오류 발생")


# 메인 실행 진입점
if __name__ == "__main__":
    # 필수 환경 변수 체크
    required_vars = ["KIS_BASE_URL", "KIS_APP_KEY", "KIS_APP_SECRET", "KIS_ACCOUNT_NO"]
    if any(var not in os.environ for var in required_vars):
        print("필수 환경 변수가 없습니다. .env 파일 확인 후 다시 실행하세요.")
        sys.exit(1)
    
    try:
        # 메인 함수 실행
        asyncio.run(main())
    except KeyboardInterrupt:
        print("프로그램 종료 (Ctrl+C)")
    except Exception as e:
        print(f"오류 발생: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)