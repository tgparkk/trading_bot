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
                                    
                                    # 재시도 카운터 초기화
                                    retry_count = 0
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
                                    
                                    # 실패 카운터 증가
                                    retry_count += 1
                                    if retry_count >= max_retries:
                                        logger.log_warning(f"종목 스캔 {max_retries}회 연속 실패, API 연결 문제가 의심됩니다.")
                                        # 토큰 강제 갱신 시도
                                        try:
                                            logger.log_system("API 토큰 강제 갱신 시도...")
                                            refresh_result = api_client.force_token_refresh()
                                            logger.log_system(f"토큰 갱신 결과: {refresh_result.get('status')} - {refresh_result.get('message')}")
                                            retry_count = 0  # 토큰 갱신 후 카운터 초기화
                                        except Exception as token_error:
                                            logger.log_error(token_error, "토큰 갱신 중 오류 발생")
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
                                
                                # 오류 카운터 증가
                                retry_count += 1
                                if retry_count >= max_retries:
                                    logger.log_warning(f"종목 스캔 {max_retries}회 연속 오류, API 연결 문제가 의심됩니다.")
                                    # 토큰 강제 갱신 시도
                                    try:
                                        logger.log_system("API 토큰 강제 갱신 시도...")
                                        refresh_result = api_client.force_token_refresh()
                                        logger.log_system(f"토큰 갱신 결과: {refresh_result.get('status')} - {refresh_result.get('message')}")
                                        retry_count = 0  # 토큰 갱신 후 카운터 초기화
                                    except Exception as token_error:
                                        logger.log_error(token_error, "토큰 갱신 중 오류 발생")
                        
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
    
    # 워치독 타이머 설정 (30분)
    last_heartbeat = datetime.now()
    watchdog_interval = 30 * 60  # 30분 (초 단위)
    logger.log_system(f"워치독 타이머 설정: {watchdog_interval/60}분")

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

        # 워치독 타이머를 위한 하트비트 태스크 시작
        heartbeat_task = asyncio.create_task(
            _heartbeat_monitor(last_heartbeat, watchdog_interval)
        )

        # 메인 봇 실행 (API 초기화 시도)
        logger.log_system("Starting main bot execution...")
        try:
            # API 접속 시도 (initialize 메소드 호출)
            await bot.initialize()
            last_heartbeat = datetime.now()  # 성공적인 초기화 후 하트비트 업데이트
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
            last_heartbeat = datetime.now()  # 봇 실행 완료 후 하트비트 업데이트
            
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
            
        # 하트비트 태스크 정리
        if 'heartbeat_task' in locals() and heartbeat_task and not heartbeat_task.done():
            logger.log_system("하트비트 모니터링 태스크 정리 중...")
            heartbeat_task.cancel()
            try:
                await asyncio.wait_for(heartbeat_task, timeout=3)
                logger.log_system("하트비트 모니터링 태스크 정리 완료")
            except (asyncio.CancelledError, RuntimeError, asyncio.TimeoutError) as ce:
                logger.log_system(f"하트비트 태스크 취소 중 예외 발생 (무시됨): {ce}")
            
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
                if "Event loop is closed" in str(session_error):
                    logger.log_system("이벤트 루프가 이미 닫혔습니다. 계속 진행합니다.")
                else:
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
        
        logger.log_system(f"Main function exiting with code {exit_code}.")
        return exit_code

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

# 토큰 파일 테스트 및 상태 확인
async def test_token():
    """토큰 파일 테스트 및 상태 확인"""
    try:
        print("===== 토큰 파일 테스트 시작 =====")
        
        # 토큰 파일 경로
        token_file_path = os.path.join(os.path.abspath(os.getcwd()), "token_info.json")
        token_exists = False
        
        # 토큰 파일 존재 여부 확인
        if os.path.exists(token_file_path):
            token_exists = True
            print(f"토큰 파일 발견: {token_file_path}")
            
            # 파일 정보 확인
            file_stats = os.stat(token_file_path)
            file_size = file_stats.st_size
            modified_time = datetime.fromtimestamp(file_stats.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            
            print(f"파일 크기: {file_size} 바이트")
            print(f"마지막 수정 시간: {modified_time}")
            
            # 파일 내용 확인
            try:
                with open(token_file_path, 'r') as f:
                    token_info = json.load(f)
                
                # 현재 토큰 정보
                if 'current' in token_info and token_info['current']:
                    current_token = token_info['current']
                    print("\n현재 토큰 정보:")
                    print(f"  토큰 상태: {current_token.get('status', 'N/A')}")
                    
                    # 토큰이 있으면 만료 시간 계산
                    if 'token' in current_token and current_token['token']:
                        token_prefix = current_token['token'][:8] + "..." if len(current_token['token']) > 8 else "N/A"
                        print(f"  토큰: {token_prefix}")
                        
                        if 'expire_time' in current_token:
                            expire_time = current_token['expire_time']
                            expire_time_str = current_token.get('expire_time_str', 'N/A')
                            print(f"  만료 시간: {expire_time_str}")
                            
                            # 남은 시간 계산
                            current_time = datetime.now().timestamp()
                            if expire_time > current_time:
                                remaining_hours = (expire_time - current_time) / 3600
                                print(f"  남은 시간: {remaining_hours:.1f}시간 (만료까지)")
                            else:
                                print(f"  상태: 만료됨")
                        
                        if 'issue_time' in current_token:
                            issue_time_str = current_token.get('issue_time_str', 'N/A')
                            print(f"  발급 시간: {issue_time_str}")
                    else:
                        print("  토큰 정보가 없습니다.")
                else:
                    print("현재 토큰 정보가 없습니다.")
                
                # 히스토리 정보
                if 'history' in token_info and token_info['history']:
                    print("\n토큰 히스토리 (최근 5개):")
                    for i, history in enumerate(token_info['history'][-5:]):
                        print(f"  {i+1}. 시간: {history.get('recorded_at', 'N/A')}, 상태: {history.get('status', 'N/A')}")
                        if history.get('error_message'):
                            print(f"     오류: {history.get('error_message')}")
                else:
                    print("\n토큰 히스토리가 없습니다.")
            
            except json.JSONDecodeError:
                print("토큰 파일이 유효한 JSON 형식이 아닙니다.")
            except Exception as e:
                print(f"토큰 파일 읽기 오류: {str(e)}")
        
        else:
            print(f"토큰 파일이 존재하지 않습니다: {token_file_path}")
            print("토큰 파일은 api_client.py가 처음 실행될 때 생성됩니다.")
        
        # API 클라이언트에서 토큰 상태 확인
        print("\nAPI 클라이언트에서 토큰 상태 확인:")
        token_status = api_client.check_token_status()
        print(f"  상태: {token_status.get('status', 'N/A')}")
        print(f"  메시지: {token_status.get('message', 'N/A')}")
        
        if 'expires_in_hours' in token_status:
            print(f"  만료까지 남은 시간: {token_status['expires_in_hours']:.1f}시간")
        
        if 'expire_time' in token_status:
            print(f"  만료 시간: {token_status.get('expire_time', 'N/A')}")
        
        if 'issue_time' in token_status:
            print(f"  발급 시간: {token_status.get('issue_time', 'N/A')}")
        
        # 토큰 파일 상세 정보
        file_info = api_client.get_token_file_info()
        if file_info:
            print("\n토큰 파일 상세 정보:")
            for key, value in file_info.items():
                print(f"  {key}: {value}")
        
        print("\n===== 토큰 테스트 완료 =====")
        
        # 강제 토큰 갱신 옵션 (파일이 없는 경우 자동으로 제안)
        if not token_exists:
            print("\n토큰 파일이 없습니다. 토큰을 생성하시겠습니까? (y/n): ", end="")
        else:
            print("\n토큰을 강제로 갱신하시겠습니까? (y/n): ", end="")
        
        user_input = input().strip().lower()
        force_refresh = user_input in ['y', 'yes', '예', 'ㅇ', 'ㅛ', 'ㅛㄷㄴ', 'yes']
        
        if force_refresh:
            print("\n토큰 강제 갱신 시작...")
            refresh_result = api_client.force_token_refresh()
            print(f"갱신 결과: {refresh_result.get('status', 'N/A')}")
            print(f"메시지: {refresh_result.get('message', 'N/A')}")
            
            # 갱신 후 상태 확인
            if 'token_status' in refresh_result:
                token_status = refresh_result['token_status']
                print(f"갱신 후 상태: {token_status.get('status', 'N/A')}")
                print(f"갱신 후 메시지: {token_status.get('message', 'N/A')}")
            
            print("토큰 강제 갱신 완료")
            
            # 파일 체크
            if os.path.exists(token_file_path):
                print(f"\n토큰 파일이 성공적으로 생성되었습니다: {token_file_path}")
                file_stats = os.stat(token_file_path)
                print(f"파일 크기: {file_stats.st_size} 바이트")
                print(f"생성 시간: {datetime.fromtimestamp(file_stats.st_ctime).strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                print(f"\n에러: 토큰 파일이 생성되지 않았습니다.")
        
    except Exception as e:
        print(f"토큰 테스트 중 오류 발생: {str(e)}")
        import traceback
        traceback.print_exc()

# 로그 확인 함수
async def check_log():
    """최근 로그 확인"""
    log_path = os.path.join(os.path.abspath(os.getcwd()), "logs")
    
    # 로그 디렉토리 확인
    if not os.path.exists(log_path):
        print(f"로그 디렉토리가 존재하지 않습니다: {log_path}")
        return
        
    # 로그 파일 목록 가져오기
    log_files = [f for f in os.listdir(log_path) if f.endswith('.log')]
    
    if not log_files:
        print("로그 파일이 없습니다.")
        return
        
    # 최신 로그 파일 찾기
    latest_log = max(log_files, key=lambda x: os.path.getmtime(os.path.join(log_path, x)))
    
    # 최신 로그 내용 표시
    log_file_path = os.path.join(log_path, latest_log)
    print(f"\n=== 최신 로그 파일: {latest_log} ===\n")
    
    # 마지막 100줄만 표시
    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        if lines:
            num_lines = len(lines)
            start_line = max(0, num_lines - 100)
            
            print(f"최근 100줄 표시 (전체 {num_lines}줄 중 {start_line+1}~{num_lines}줄)\n")
            for i, line in enumerate(lines[start_line:], start=start_line+1):
                print(f"{i}: {line.strip()}")
        else:
            print("로그 파일이 비어 있습니다.")
    except Exception as e:
        print(f"로그 파일 읽기 오류: {str(e)}")
    
    print("\n=== 로그 확인 완료 ===\n")

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
    parser = argparse.ArgumentParser(description="주식 자동매매 프로그램")
    parser.add_argument("--update", action="store_true", help="종목 데이터 업데이트")
    parser.add_argument("--checklog", action="store_true", help="최근 로그 확인")
    parser.add_argument("--test", action="store_true", help="매수 조건 임시 테스트")
    parser.add_argument("--test_token", action="store_true", help="토큰 파일 테스트 및 상태 확인")
    parser.add_argument("--symbol", type=str, help="특정 종목 테스트용 (ex: 005930)")
    parser.add_argument("--threshold", type=float, help="매수 임계값 설정 (ex: 5.0)")
    parser.add_argument("--restart", action="store_true", help="프로그램 충돌 시 자동 재시작 활성화")
    
    args = parser.parse_args()
    
    try:
        if args.test_token:
            asyncio.run(test_token())
        elif args.threshold is not None:
            asyncio.run(set_buy_threshold(args.threshold))
        elif args.test:
            asyncio.run(test_buy_condition(args.symbol))
        elif args.checklog:
            asyncio.run(check_log())
        elif args.update:
            asyncio.run(main(force_update=True))
        else:
            # 자동 재시작 기능이 활성화된 경우
            if args.restart:
                print("자동 재시작 기능이 활성화되었습니다. 프로그램이 충돌하면 자동으로 재시작됩니다.")
                restart_count = 0
                max_restarts = 5
                
                while restart_count < max_restarts:
                    try:
                        exit_code = asyncio.run(main())
                        
                        if exit_code == 0:  # 정상 종료
                            print("프로그램이 정상적으로 종료되었습니다.")
                            break
                        else:
                            print(f"프로그램이 오류 코드 {exit_code}로 종료되었습니다. 재시작합니다...")
                            restart_count += 1
                            print(f"재시작 시도 {restart_count}/{max_restarts}")
                            time.sleep(30)  # 30초 후 재시작
                    except Exception as e:
                        print(f"치명적인 오류 발생: {str(e)}. 재시작합니다...")
                        restart_count += 1
                        print(f"재시작 시도 {restart_count}/{max_restarts}")
                        time.sleep(30)  # 30초 후 재시작
                
                if restart_count >= max_restarts:
                    print(f"최대 재시작 횟수({max_restarts}회)를 초과했습니다. 프로그램을 종료합니다.")
            else:
                # 일반 실행
                asyncio.run(main())
    except KeyboardInterrupt:
        print("프로그램 종료 (Ctrl+C)")
        sys.exit(0)
    except Exception as e:
        print(f"프로그램 실행 중 예상치 못한 오류 발생: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)