"""
자동 매매 봇 메인 모듈
"""

import asyncio
import concurrent.futures
import signal
import sys
import os
import time
import random
from datetime import datetime, timedelta, time as datetime_time
from typing import Dict, List, Any, Optional, Tuple

from core.api_client import api_client
from core.order_manager import order_manager
from core.account_state import account_state
from core.websocket_client import websocket_client
from strategies.combined_strategy import combined_strategy
from monitoring.alert_system import alert_system
from utils.logger import logger
from utils.database import database_manager
from utils.market_hours import is_market_open, get_next_market_open, format_market_time

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
from utils.database import database_manager
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

# 전역 변수로 모니터링 종목 관리
MONITORED_SYMBOLS: List[str] = []
LAST_SYMBOL_UPDATE: Optional[datetime] = None

class TradingBot:
    """자동매매 봇"""
    
    def __init__(self) -> None:
        self.running: bool = False
        self.trading_config = config["trading"]
        # max_websocket_retries 속성을 안전하게 가져오기
        try:
            self.max_retries: int = self.trading_config.max_websocket_retries
        except AttributeError:
            self.max_retries: int = 3  # 기본값 설정
            logger.log_warning("max_websocket_retries not found in config, using default value 3")
        
    async def initialize(self) -> None:
        """초기화"""
        try:
            logger.log_system("Initializing trading bot...")
            
            # DB 초기화
            database_manager.update_system_status("INITIALIZING")
            
            # 계좌 상태 관리자 초기화
            #logger.log_system("계좌 상태 관리자 초기화 중...")
            await account_state.initialize()
            # logger.log_system("계좌 상태 관리자 초기화 완료")
            
            # 주문 관리자 초기화
            await order_manager.initialize()
            
            # 전략들이 제대로 로드되었는지 확인
            # logger.log_system("전략 확인 시작...")
            strategies = combined_strategy.strategies
            logger.log_system(f"전략 갯수: {len(strategies)}")
            
            # 계좌 정보 확인 및 로깅
            account_balance = await order_manager.get_account_balance()
            cash_balance = account_balance.get("cash_balance", 0)
            if cash_balance > 0:
                logger.log_system(f"계좌 잔고 확인: {cash_balance:,.0f}원")
            else:
                logger.log_warning(f"계좌 잔고 확인 실패 또는 잔고 부족: {cash_balance:,.0f}원")
            
            # 웹소켓 연결 - 개선된 재시도 로직
            websocket_connected = False
            retry_count = 0
            
            # API 서버가 준비되었는지 확인하기 위해 잠시 대기
            logger.log_system("API 서버 준비 확인을 위해 3초 대기...")
            await asyncio.sleep(3)
            
            while not websocket_connected and retry_count < self.max_retries:
                retry_count += 1
                try:
                    logger.log_system(f"웹소켓 연결 시도... ({retry_count}/{self.max_retries})")
                    
                    # 웹소켓 클라이언트 상태 초기화 확인
                    if ws_client.ws is not None or ws_client.is_connected():
                        logger.log_system("기존 웹소켓 연결 자원 정리...")
                        await ws_client.close()
                        await asyncio.sleep(2)  # 자원 정리를 위한 대기
                    
                    # 접속 시도
                    connection_success = await ws_client.connect()
                    websocket_connected = ws_client.is_connected()
                    
                    if websocket_connected:
                        logger.log_system("웹소켓 연결 성공!")
                        break
                    else:
                        logger.log_warning(f"웹소켓 연결 시도 실패 ({retry_count}/{self.max_retries})")
                except Exception as e:
                    logger.log_error(e, f"웹소켓 연결 실패 ({retry_count}/{self.max_retries})")
                
                if retry_count < self.max_retries:
                    # 재시도 간격을 증가
                    wait_time = 5 * retry_count  # 선형적 증가 (5초, 10초, 15초...)
                    logger.log_system(f"웹소켓 재연결 {wait_time}초 후 재시도...")
                    await asyncio.sleep(wait_time)
            
            # 시스템 상태 업데이트
            database_manager.update_system_status("RUNNING")
            
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
    
    async def run(self) -> None:
        """실행 - 리팩토링된 버전"""
        try:
            global MONITORED_SYMBOLS, LAST_SYMBOL_UPDATE
            
            self.running = True
            
            logger.log_system("=== 백엔드 서버 준비 완료 - 트레이딩 프로세스 시작 ===")
            
            # 1. 초기 종목 스캔 - 5개 전략 사용하여 상위 100개 선정
            await self._initial_symbol_scan()
            
            while self.running:
                try:
                    current_time = datetime.now().time()
                    
                    # 3. 장 시작 30분 전 (8:30) 또는 오래된 데이터일 경우 종목 재스캔
                    if self._should_rescan_symbols(current_time):
                        await self._rescan_symbols()
                    
                    # 4. 장 시간 체크 및 거래 실행
                    if self._is_market_open(current_time):
                        # 포지션 체크 - 중복 호출 제거 (check_sell_signals에서 이미 실행됨)
                        
                        # 매도 신호 체크 및 주문 실행 로직 추가
                        try:
                            await self.check_sell_signals()
                        except Exception as sell_error:
                            logger.log_error(sell_error, "매도 신호 체크 중 예외 발생")
                            await alert_system.notify_system_status(
                                "ERROR", 
                                f"매도 신호 체크 중 오류: {str(sell_error)}\n자세한 내용은 로그를 확인하세요."
                            )
                            
                        if len(MONITORED_SYMBOLS) > 0:
                            # 매수 신호 체크
                            try:
                                await self.check_buy_signals()
                            except Exception as buy_error:
                                logger.log_error(buy_error, "매수 신호 체크 중 예외 발생")
                                await alert_system.notify_system_status(
                                    "ERROR", 
                                    f"매수 신호 체크 중 오류: {str(buy_error)}\n자세한 내용은 로그를 확인하세요."
                                )
                                                
                        # 시스템 상태 업데이트
                        database_manager.update_system_status("RUNNING")
                        
                        # 주기적 상태 로깅 (1분마다)
                        if datetime.now().second < 5:
                            logger.log_system(f"시스템 실행 중 - 현재 시간: {current_time}, 모니터링 종목 수: {len(MONITORED_SYMBOLS)}")
                    else:
                        # 장 마감 처리
                        if current_time > self.trading_config.market_close:
                            await self._handle_market_close()
                    
                    # 30초 대기 (API 호출 빈도 조절)
                    await asyncio.sleep(30)
                    
                except Exception as loop_error:
                    logger.log_error(loop_error, "메인 루프 내부 처리 중 오류 발생")
                    await asyncio.sleep(5)
                
        except Exception as e:
            logger.log_error(e, "Trading bot error")
            await self.shutdown(error=str(e))


    async def check_sell_signals(self):
        """익절 조건 체크 및 매도 주문 실행"""
        logger.log_system("======== 익절 조건 체크 시작 ========")
        
        # 포지션 정보 가져오기
        try:
            positions = await order_manager.get_positions()
            if positions and "output1" in positions:
                position_items = positions.get("output1", [])
                
                # 보유 종목 수량이 0인 경우 체크 중단
                if not position_items or len(position_items) == 0:
                    logger.log_system("보유 종목 정보가 없습니다.")
                    logger.log_system("======== 익절 조건 체크 종료 ========")
                    return
                    
                logger.log_system(f"보유 종목 수: {len(position_items)}개")
                
                # 보유 종목 심볼 목록 생성 (빠른 조회용)
                held_symbols = {}
                for position in position_items:
                    symbol = position.get("pdno", "")
                    qty = int(position.get("hldg_qty", "0"))
                    
                    # 수량이 0 초과인 종목만 저장
                    if qty > 0 and symbol:
                        held_symbols[symbol] = {
                            "qty": qty,
                            "avg_price": float(position.get("pchs_avg_pric", "0")),
                            "profit_rate": float(position.get("evlu_pfls_rt", "0")),
                            "pchs_amt": float(position.get("pchs_amt", "0"))
                        }
                
                # 보유 종목이 실제로 있는지 다시 확인
                if not held_symbols:
                    logger.log_system("실제 보유 중인 종목(수량 > 0)이 없습니다.")
                    logger.log_system("======== 익절 조건 체크 종료 ========")
                    return
                    
                logger.log_system(f"실제 보유 종목 수: {len(held_symbols)}개, 종목 목록: {', '.join(held_symbols.keys())}")
                
                # 병렬 처리를 위한 함수 정의
                async def process_sell_position(symbol, position_data):
                    """종목별 매도 조건 체크 및 주문 실행 함수"""
                    try:
                        # 종목 정보 추출
                        qty = position_data["qty"]
                        avg_price = position_data["avg_price"]
                        current_profit_rate = position_data["profit_rate"]
                        sell_result = {"symbol": symbol, "executed": False}
                        
                        # 매수 시간 확인 (DB에서 최근 매수 기록 조회)
                        try:
                            buy_record = database_manager.get_latest_trade(symbol, "BUY")
                            if buy_record:
                                # 최근 매수 시간 추출
                                buy_time_str = buy_record.get("time", "")
                                if buy_time_str:
                                    # 현재 날짜와 시간 문자열 결합
                                    today = datetime.now().strftime("%Y-%m-%d")
                                    buy_datetime_str = f"{today} {buy_time_str}"
                                    try:
                                        buy_datetime = datetime.strptime(buy_datetime_str, "%Y-%m-%d %H:%M:%S")
                                        # 매수 후 경과 시간 계산 (분 단위)
                                        time_since_buy = (datetime.now() - buy_datetime).total_seconds() / 60
                                        
                                        # 수익률에 따른 유연한 홀딩 시간 적용
                                        min_hold_time = 10  # 기본 10분
                                        
                                        # 수익률이 높을수록 최소 홀딩 시간 감소
                                        if current_profit_rate >= 4.0:  # 4% 이상
                                            min_hold_time = 2  # 2분만 홀딩
                                            logger.log_system(f"[수익률 높음] {symbol}: {current_profit_rate:.2f}% 수익으로 최소 홀딩 시간 2분 적용")
                                        elif current_profit_rate >= 3.0:  # 3% 이상
                                            min_hold_time = 4  # 4분만 홀딩
                                            logger.log_system(f"[수익률 양호] {symbol}: {current_profit_rate:.2f}% 수익으로 최소 홀딩 시간 4분 적용")
                                        elif current_profit_rate >= 2.0:  # 2% 이상
                                            min_hold_time = 6  # 6분만 홀딩
                                            logger.log_system(f"[수익률 보통] {symbol}: {current_profit_rate:.2f}% 수익으로 최소 홀딩 시간 6분 적용")
                                        
                                        if time_since_buy < min_hold_time:
                                            logger.log_system(f"[매도 보류] {symbol}: 매수 후 {time_since_buy:.1f}분 경과 (최소 {min_hold_time}분 홀딩 필요)")
                                            return sell_result
                                        logger.log_system(f"[매도 검토] {symbol}: 매수 후 {time_since_buy:.1f}분 경과 (최소 대기 시간 충족)")
                                    except Exception as time_error:
                                        logger.log_error(time_error, f"{symbol} 매수 시간 파싱 오류")
                        except Exception as db_error:
                            logger.log_error(db_error, f"{symbol} 매수 기록 조회 오류")
                        
                        # 손익률이 2% 이상인지 확인
                        if current_profit_rate < 2.0 or qty <= 0:
                            logger.log_system(f"[익절 대기] {symbol}: 현재 손익률={current_profit_rate:.2f}% (목표: 2.0% 이상)")
                            return sell_result
                            
                        logger.log_system(f"[익절 조건 감지] {symbol}: 보유수량={qty}주, 매수가={avg_price:,.0f}원, 손익률={current_profit_rate:.2f}%")
                        
                        # 현재가 조회
                        try:
                            symbol_info = await asyncio.wait_for(
                                api_client.get_symbol_info(symbol),
                                timeout=2.0
                            )
                            
                            if not symbol_info or "current_price" not in symbol_info:
                                logger.log_system(f"[익절 패스] {symbol}: 현재가 조회 실패")
                                return sell_result
                                
                            current_price = symbol_info["current_price"]
                            # 실제 수익률 계산 (API 응답과 일치여부 확인)
                            calc_profit_rate = ((current_price / avg_price) - 1) * 100
                            
                            logger.log_system(f"[익절 확인] {symbol}: 매수가={avg_price:,.0f}원, 현재가={current_price:,.0f}원, "
                                            f"계산 손익률={calc_profit_rate:.2f}%, API 손익률={current_profit_rate:.2f}%")
                            
                            # 매도 여부 및 이유 결정
                            sell_decision = self._decide_sell_action(symbol, calc_profit_rate, current_profit_rate)
                            
                            # 매도 실행 결정되었으면 바로 주문 진행
                            if sell_decision["should_sell"]:
                                # 이 함수 내에서 바로 매도 주문 실행
                                sell_success = await self._execute_sell_order(
                                    symbol, 
                                    qty, 
                                    current_price, 
                                    avg_price, 
                                    sell_decision["reason"], 
                                    0  # 개별 처리이므로 orders_count는 의미 없음
                                )
                                sell_result["executed"] = sell_success
                                return sell_result
                            else:
                                logger.log_system(f"[익절 보류] {symbol}: 전략 신호에 따라 매도하지 않고 계속 보유")
                                return sell_result
                                
                        except asyncio.TimeoutError:
                            logger.log_system(f"[익절 패스] {symbol}: 현재가 조회 타임아웃")
                            return sell_result
                        except Exception as e:
                            logger.log_error(e, f"{symbol} 현재가 조회 중 오류")
                            return sell_result
                            
                    except Exception as e:
                        logger.log_error(e, f"{symbol} 매도 조건 체크 중 오류")
                        return {"symbol": symbol, "executed": False}
                
                # 각 종목에 대해 비동기 태스크 생성
                tasks = []
                for symbol, position_data in held_symbols.items():
                    tasks.append(process_sell_position(symbol, position_data))
                
                # 모든 태스크 완료 대기 및 결과 수집
                results = await asyncio.gather(*tasks)
                
                # 매도 실행 결과 집계
                sell_orders_placed = sum(1 for result in results if result.get("executed", False))
                
                # 익절 주문 결과 요약
                logger.log_system(f"익절 주문 실행 결과: {sell_orders_placed}개 주문 실행됨")
            else:
                logger.log_system("보유 종목 정보가 없습니다.")
        except Exception as positions_error:
            logger.log_error(positions_error, "포지션 정보 조회 실패")
        
        logger.log_system("======== 익절 조건 체크 종료 ========")

    def _decide_sell_action(self, symbol, calc_profit_rate, current_profit_rate):
        """매도 결정 및 이유 반환"""
        should_sell = False
        sell_reason = "2% 익절 자동 매도"
        force_sell = False
        
        # 손절매 설정 가져오기 (기본값 -2%)
        stop_loss_threshold = getattr(self.trading_config, "stop_loss", -2.0)
        
        # 손익률 중 더 낮은 값 사용 (보수적 접근)
        profit_rate = min(calc_profit_rate, current_profit_rate)
        
        # 매수 시간 확인 (DB에서 최근 매수 기록 조회)
        time_since_buy = 0
        buy_record = None
        try:
            buy_record = database_manager.get_latest_trade(symbol, "BUY")
            if buy_record:
                # 최근 매수 시간 추출
                buy_time_str = buy_record.get("time", "")
                if buy_time_str:
                    # 현재 날짜와 시간 문자열 결합
                    today = datetime.now().strftime("%Y-%m-%d")
                    buy_datetime_str = f"{today} {buy_time_str}"
                    try:
                        buy_datetime = datetime.strptime(buy_datetime_str, "%Y-%m-%d %H:%M:%S")
                        # 매수 후 경과 시간 계산 (분 단위)
                        time_since_buy = (datetime.now() - buy_datetime).total_seconds() / 60
                        logger.log_system(f"[시간 확인] {symbol}: 매수 후 {time_since_buy:.1f}분 경과")
                    except Exception as time_error:
                        logger.log_error(time_error, f"{symbol} 매수 시간 파싱 오류")
        except Exception as db_error:
            logger.log_error(db_error, f"{symbol} 매수 기록 조회 오류")
        
        # 전략 신호 한 번만 조회 (중복 호출 방지)
        strategy_status = None
        signal_info = None
        signal_direction = "NEUTRAL"  # 기본값
        signal_score = 0  # 기본값
        
        try:
            strategy_status = combined_strategy.get_strategy_status(symbol)
            
            if "signals" in strategy_status and symbol in strategy_status["signals"]:
                signal_info = strategy_status["signals"][symbol]
                signal_direction = signal_info.get("direction", "NEUTRAL")
                signal_score = signal_info.get("score", 0)
                logger.log_system(f"[전략 상태] {symbol}: 방향={signal_direction}, 점수={signal_score:.1f}")
        except Exception as strategy_error:
            logger.log_error(strategy_error, f"{symbol} 전략 신호 확인 중 오류")
        
        # 손실 상태인 경우 손절매 로직 적용
        if profit_rate < 0:
            # 손실 비율 계산 (음수 값)
            loss_rate = profit_rate
            
            # 1. 매수 직후 급락 처리 (5분 이내의 초기 하락은 더 큰 하락 허용)
            if time_since_buy < 5:
                # 초기 5분 내 급락이면 더 강화된 손절 기준 적용 (-5% 이하)
                if loss_rate <= -5.0:
                    should_sell = True
                    sell_reason = f"매수 직후 급락으로 긴급 손절 ({loss_rate:.2f}%)"
                    logger.log_system(f"[손절매 결정] {symbol}: 매수 직후 급락 ({loss_rate:.2f}%)")
                else:
                    # 초기 하락은 좀 더 지켜봄
                    logger.log_system(f"[손절매 보류] {symbol}: 매수 직후 초기 하락 대기 중 ({loss_rate:.2f}%)")
                    return {"should_sell": False, "reason": "매수 직후 하락 관망"}
            
            # 2. 손절매 기준점 초과 손실 확인
            elif loss_rate <= stop_loss_threshold:
                # 전략 신호 재확인
                logger.log_system(f"[손절매 검토] {symbol}: 손실률={loss_rate:.2f}%, 전략방향={signal_direction}, 점수={signal_score:.1f}")
                
                # 2-1. 전략이 매도/중립이면 바로 손절
                if signal_direction in ["SELL", "NEUTRAL"]:
                    should_sell = True
                    sell_reason = f"손절매 기준 도달 ({loss_rate:.2f}%), 전략 {signal_direction}"
                    logger.log_system(f"[손절매 결정] {symbol}: 손실률 {loss_rate:.2f}% + {signal_direction} 신호")
                
                # 2-2. 전략이 매수지만 손실이 심각하면 손절 (-7% 이하)
                elif signal_direction == "BUY" and loss_rate <= -7.0:
                    should_sell = True
                    sell_reason = f"매수 신호 불구 심각한 손실 ({loss_rate:.2f}%)"
                    logger.log_system(f"[손절매 결정] {symbol}: 매수 신호 있으나 심각한 손실 ({loss_rate:.2f}%)")
                
                # 2-3. 매수 신호 점수가 낮고 손실이 기준치 이하면 손절
                elif signal_direction == "BUY" and signal_score < 3.0 and loss_rate <= stop_loss_threshold:
                    should_sell = True
                    sell_reason = f"약한 매수 신호({signal_score:.1f})와 손실 ({loss_rate:.2f}%)"
                    logger.log_system(f"[손절매 결정] {symbol}: 약한 매수 신호({signal_score:.1f}) + 손실 ({loss_rate:.2f}%)")
                
                # 2-4. 매수 신호 점수가 높으면 조금 더 기다림
                else:
                    logger.log_system(f"[손절매 보류] {symbol}: 강한 매수 신호(점수={signal_score:.1f})로 홀딩 유지")
                    return {"should_sell": False, "reason": "강한 매수 신호로 홀딩 유지"}
            
            # 3. 장기 보유 종목 중 지속적 하락세 처리
            elif time_since_buy > 60 and loss_rate <= -3.0:  # 1시간 이상 보유, 3% 이상 손실
                # 거래량 확인 등 추가 지표 활용 가능
                should_sell = True
                sell_reason = f"장기 보유 중 지속 하락 ({loss_rate:.2f}%)"
                logger.log_system(f"[손절매 결정] {symbol}: 장기 보유({time_since_buy:.1f}분) 중 지속 하락 ({loss_rate:.2f}%)")
            
            # 4. 그 외 손실은 계속 모니터링
            else:
                logger.log_system(f"[손절매 보류] {symbol}: 손절 기준 미달 손실 ({loss_rate:.2f}%)")
                return {"should_sell": False, "reason": "손절 기준 미달"}
            
            # 손절매 결정된 경우 바로 반환
            if should_sell:
                return {"should_sell": should_sell, "reason": sell_reason}
        
        # 아래는 기존 익절 로직 유지
        # 0.5. 긴급 급등 매도 조건 (초단기 급등)
        if calc_profit_rate >= 4.5 or current_profit_rate >= 4.5:
            should_sell = True
            force_sell = True
            sell_reason = f"급등 수익 확정 매도 ({max(calc_profit_rate, current_profit_rate):.2f}%)"
            logger.log_system(f"[긴급 매도] {symbol}: 급등으로 인한 즉시 매도 결정 (수익률: {max(calc_profit_rate, current_profit_rate):.2f}%)")
            return {"should_sell": should_sell, "reason": sell_reason}
        
        # 1. 고수익 안전장치 - 5% 이상이면 무조건 매도
        if calc_profit_rate >= 5.0 or current_profit_rate >= 5.0:
            should_sell = True
            force_sell = True
            sell_reason = "5% 이상 고수익 확정 매도"
            logger.log_system(f"[전략 무관 매도] {symbol}: 5% 이상 고수익으로 전략 신호와 관계없이 매도")
            return {"should_sell": should_sell, "reason": sell_reason}
        
        # 1.5. 낮은 수익률(0.5% 이상 1.0% 미만)인 경우 매도하지 않음 (홀딩 장려)
        if 0.5 <= calc_profit_rate < 1.0 or 0.5 <= current_profit_rate < 1.0:
            logger.log_system(f"[매도 보류] {symbol}: 낮은 수익률({calc_profit_rate:.2f}%)로 홀딩 유지")
            return {"should_sell": False, "reason": "낮은 수익률로 홀딩 유지"}
        
        # 2. 손익률 기본 검증 - 최소 2% 이상 (1.5%는 매도하지 않고 홀딩)
        if ((calc_profit_rate >= 2.0 and current_profit_rate >= 1.5) or 
            (current_profit_rate >= 2.0 and calc_profit_rate >= 1.5)) and not force_sell:
            # 3. 전략 신호 확인 (이미 위에서 조회했으므로 중복 호출 제거)
            logger.log_system(f"[전략 결과] {symbol}: 방향={signal_direction}, 점수={signal_score:.1f}")
            
            # 전략 신호에 따른 매도 결정
            if signal_direction == "SELL":
                # 매도 신호가 있으면 매도
                should_sell = True
                sell_reason = f"전략 매도 신호에 따른 익절 (점수: {signal_score:.1f})"
                logger.log_system(f"[전략 매도] {symbol}: 매도 신호로 익절")
            elif signal_direction == "NEUTRAL":
                # 중립 신호이고 2% 이상이면 매도
                should_sell = True
                sell_reason = "중립 신호 상태에서 2% 익절"
                logger.log_system(f"[전략 매도] {symbol}: 중립 신호 + 2% 이상으로 익절")
            elif signal_direction == "BUY":
                # 매수 신호면 3.5% 미만일 경우 홀딩 (3.5% 이상이면 매도)
                if calc_profit_rate >= 3.5 or current_profit_rate >= 3.5:
                    should_sell = True
                    sell_reason = f"매수 신호지만 3.5% 이상 수익 확정 (전략 점수: {signal_score:.1f})"
                    logger.log_system(f"[전략 매도] {symbol}: 매수 신호지만 3.5% 이상 수익으로 매도")
                else:
                    logger.log_system(f"[전략 홀딩] {symbol}: 매수 신호로 3.5% 미만 수익 홀딩 (점수: {signal_score:.1f})")
            else:
                # 신호 데이터가 없거나 인식할 수 없는 경우
                if calc_profit_rate >= 2.5 or current_profit_rate >= 2.5:
                    should_sell = True
                    sell_reason = "전략 데이터 미확인, 2.5% 익절 진행"
                    logger.log_system(f"[전략 미확인] {symbol}: 전략 데이터 없어 2.5% 익절")
                else:
                    logger.log_system(f"[전략 홀딩] {symbol}: 전략 데이터 없으나 2.5% 미만이라 홀딩")
        
        return {"should_sell": should_sell, "reason": sell_reason}

    async def _execute_sell_order(self, symbol, qty, current_price, avg_price, sell_reason, sell_orders_count):
        """매도 주문 실행"""
        logger.log_system(f"[익절 주문] {symbol} 매도 실행: 수량={qty}주, 가격={current_price:,.0f}원, 사유={sell_reason}")
        
        try:
            order_result = await asyncio.wait_for(
                order_manager.place_order(
                    symbol=symbol,
                    side="SELL",
                    quantity=qty,
                    price=current_price,
                    order_type="MARKET",
                    strategy="main_bot",
                    reason=sell_reason.replace(" ", "_").lower()
                ),
                timeout=5.0
            )
            
            if order_result and order_result.get("status") == "success":
                logger.log_system(f"✅ 익절 주문 성공: {symbol}, 주문ID={order_result.get('order_id')}")
                
                # 주문 정보 로깅
                logger.log_trade(
                    action="SELL",
                    symbol=symbol,
                    price=current_price,
                    quantity=qty,
                    reason=sell_reason,
                    strategy="main_bot",
                    profit_rate=f"{((current_price / avg_price) - 1) * 100:.2f}%",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="SUCCESS"
                )
                return True
            else:
                logger.log_system(f"❌ 익절 주문 실패: {symbol}, 사유={order_result.get('reason')}")
                return False
        except asyncio.TimeoutError:
            logger.log_system(f"⏱️ 익절 주문 타임아웃: {symbol}")
            return False
        except Exception as order_error:
            logger.log_error(order_error, f"{symbol} 익절 주문 처리 중 오류")
            return False

    async def check_buy_signals(self):
        """매수 신호 체크 및 주문 실행"""
        logger.log_system("======== 매수 신호 체크 시작 ========")
        
        # 계좌 정보 조회 및 투자 금액 계산
        investment_info = await self.get_investment_amount()
        if not investment_info["can_invest"]:
            logger.log_system("투자 가능 금액 부족으로 매수 체크 중단")
            return
        
        # 모니터링 종목 중 상위 30개만 체크
        symbols_to_check = MONITORED_SYMBOLS[:30]
        logger.log_system(f"체크할 종목 수: {len(symbols_to_check)}개")
        
        # 매수 신호 체크 및 주문 실행
        buy_signals_found = 0
        orders_placed = 0
        used_investment = 0
        
        # 멀티스레드 처리를 위한 함수 정의
        def check_symbol_signal(symbol: str) -> Tuple[str, bool, Dict]:
            """스레드에서 실행할 매수 신호 체크 함수"""
            try:
                # 매수 신호 확인
                signal_info = self.check_buy_signal(symbol)
                has_signal = signal_info["has_signal"]
                return symbol, has_signal, signal_info
            except Exception as e:
                logger.log_error(e, f"{symbol} 매수 신호 체크 중 스레드 오류")
                return symbol, False, {"has_signal": False}
        
        # 스레드풀 설정 
        max_workers = min(30, len(symbols_to_check))  # 최대 30개 스레드까지만 사용
        logger.log_system(f"매수 신호 체크에 {max_workers}개 스레드 병렬 사용")
        
        # 신호가 있는 종목들을 저장할 리스트
        buy_signal_symbols = []
        
        # 멀티스레드 실행 블록
        loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 모든 종목에 대해 동시에 신호 체크 요청
            futures = [
                loop.run_in_executor(executor, check_symbol_signal, symbol) 
                for symbol in symbols_to_check
            ]
            
            # 결과 수집
            for future in await asyncio.gather(*futures):
                symbol, has_signal, signal_info = future
                if has_signal:
                    buy_signals_found += 1
                    logger.log_system(f"[{buy_signals_found}] 매수 신호 발견: {symbol}, 점수={signal_info['score']:.1f}")
                    buy_signal_symbols.append((symbol, signal_info))
        
        logger.log_system(f"매수 신호 체크 완료: {buy_signals_found}개 발견")
        
        # 매수 신호가 있는 종목에 대해 순차적으로 주문 처리
        for symbol, signal_info in buy_signal_symbols:
            # 최대 주문 수 도달 시 중단
            if orders_placed >= investment_info["max_stocks_to_buy"]:
                logger.log_system(f"최대 주문 수 도달 ({investment_info['max_stocks_to_buy']}개), 추가 주문 중단")
                break
                
            # 남은 투자 가능 금액 체크
            remaining_investment = investment_info["total_amount"] - used_investment
            if remaining_investment < 10000:  # 최소 주문 금액 미만
                logger.log_system(f"남은 투자 가능 금액 부족: {remaining_investment:,.0f}원, 매수 신호 체크 중단")
                break
            
            # 주문 처리
            order_result = await self.process_buy_order(
                symbol=symbol,
                signal_score=signal_info["score"],
                remaining_investment=remaining_investment,
                per_stock_amount=investment_info["per_stock_amount"]
            )
            
            if order_result["success"]:
                orders_placed += 1
                used_investment += order_result["order_amount"]
                logger.log_system(f"남은 투자 가능 금액: {investment_info['total_amount'] - used_investment:,.0f}원")
                await asyncio.sleep(1.0)  # 주문 간 간격 추가
        
        # 결과 요약
        logger.log_system(f"매수 신호 체크 완료: 발견={buy_signals_found}개, 주문 실행={orders_placed}개")
        if orders_placed > 0:
            logger.log_system(f"투자 금액 사용: {used_investment:,.0f}원/{investment_info['total_amount']:,.0f}원")
        logger.log_system("======== 매수 신호 체크 종료 ========")

    async def get_investment_amount(self):
        """계좌 정보 조회 및 투자 금액 계산"""
        try:
            # 계좌 잔고 조회
            account_balance = await order_manager.get_account_balance()
            
            # API 응답에서 예수금 정보 가져오기
            cash_balance = account_balance.get("cash_balance", 0.0)
            total_balance = account_balance.get("total_balance", 0.0)
            
            logger.log_system(f"계좌 정보: 예수금={cash_balance:,.0f}원, 총평가금액={total_balance:,.0f}원")
            
            # 투자 가능 여부 및 금액 계산
            if cash_balance <= 0:
                logger.log_warning(f"예수금이 부족합니다: {cash_balance:,.0f}원")
                return {"can_invest": False}
            
            # 투자 금액 설정 (예수금의 30% - 기존 50%에서 감소)
            total_amount = cash_balance * 0.3
            
            # 최대 매수 종목 수 증가 (더 작은 금액으로 분산)
            max_stocks_to_buy = 5  # 3에서 5로 증가
            
            # 종목당 금액 계산시 추가 안전 마진 적용 (85%)
            per_stock_amount = (total_amount / max_stocks_to_buy) * 0.85
            
            # 종목당 최대 투자 금액 제한 (30만원)
            max_per_stock = 300000
            if per_stock_amount > max_per_stock:
                per_stock_amount = max_per_stock
                logger.log_system(f"종목당 투자 금액이 상한선({max_per_stock:,.0f}원)으로 제한되었습니다")
            
            logger.log_system(f"투자 가능 금액: {total_amount:,.0f}원 (예수금의 30%), 종목당 최대 {per_stock_amount:,.0f}원, 최대 {max_stocks_to_buy}개 종목")
            
            return {
                "can_invest": True,
                "total_amount": total_amount,
                "per_stock_amount": per_stock_amount,
                "max_stocks_to_buy": max_stocks_to_buy
            }
        except Exception as e:
            logger.log_error(e, "계좌 잔고 조회 실패")
            # 기본값으로 계속 진행 (더 보수적인 값으로 설정)
            return {
                "can_invest": True,
                "total_amount": 500000,  # 기본값 50만원 (100만원에서 감소)
                "per_stock_amount": 100000,  # 기본 종목당 약 10만원 (33만원에서 감소)
                "max_stocks_to_buy": 5
            }

    def check_buy_signal(self, symbol):
        """종목의 매수 신호 확인"""
        try:
            # 통합 전략에서 신호 얻기
            strategy_status = combined_strategy.get_strategy_status(symbol)
            
            # 신호 정보 유효성 확인
            if (symbol not in strategy_status.get("signals", {}) or 
                "direction" not in strategy_status["signals"][symbol]):
                return {"has_signal": False}
            
            # 매수 신호 및 점수 확인
            signal_info = strategy_status["signals"][symbol]
            signal_direction = signal_info["direction"]
            signal_score = signal_info.get("score", 0)
            
            # 매수 신호 및 최소 점수(6.0) 확인
            if signal_direction == "BUY" and signal_score >= 5.0:
                return {"has_signal": True, "score": signal_score}
                
            return {"has_signal": False}
        except Exception as e:
            logger.log_error(e, f"{symbol} 매수 신호 확인 중 오류")
            return {"has_signal": False}
    
    def calculate_order_quantity(self, symbol, current_price, available_amount):
        """주문 수량 및 금액 계산"""
        try:
            # 기본 주문 단위 (실제로는 API 조회 필요)
            order_unit = 1
            
            # 최대 주문 가능 금액 결정 (가용 자금의 80%로 제한하여 안전 마진 확보)
            max_safe_amount = available_amount * 0.8  # 90%에서 80%로 수정하여 안전 마진 강화
            
            # 수수료 및 슬리피지 고려 (약 0.5% 추가 비용)
            fee_rate = 0.005  # 0.5%로 증가 (0.3%에서)
            available_for_order = max_safe_amount / (1 + fee_rate)
            
            # 로깅 - 계산 과정 추적
            logger.log_system(f"주문 계산: {symbol}, 가격={current_price:,.0f}원, 가용금액={available_amount:,.0f}원, 수수료고려={available_for_order:,.0f}원")
            
            # 기본 주문 수량 계산 - 보수적으로 내림
            initial_quantity = int(available_for_order / current_price)
            
            # 주문 가능한 최소 수량 체크
            if initial_quantity <= 0:
                logger.log_system(f"주문 불가: {symbol}, 가용 금액으로 최소 1주도 구매할 수 없습니다.")
                return {"can_order": False}
            
            # 안전 제한 적용
            max_order_value = min(500000, available_for_order)  # 최대 50만원으로 제한(100만원에서 변경)
            if initial_quantity * current_price > max_order_value:
                quantity = int(max_order_value / current_price)
                logger.log_system(f"주문 조정: {symbol}, 최대 주문 금액 제한으로 수량 조정 {initial_quantity}→{quantity}주")
            else:
                quantity = initial_quantity
                
            # 최대 주문 수량 제한
            max_quantity_limit = 500  # 1천주에서 500주로 제한 감소
            if quantity > max_quantity_limit:
                quantity_before = quantity
                quantity = max_quantity_limit
                logger.log_system(f"주문 조정: {symbol}, 최대 주문 수량 제한으로 수량 조정 {quantity_before}→{quantity}주")
            
            # 최소 주문 재확인
            if quantity <= 0:
                logger.log_system(f"주문 불가: {symbol}, 조정 후 계산된 주문 수량이 0입니다.")
                return {"can_order": False}
            
            # 주문 단위 조정
            if order_unit > 1 and quantity % order_unit != 0:
                quantity_before = quantity
                quantity = (quantity // order_unit) * order_unit
                logger.log_system(f"주문 조정: {symbol}, 주문 단위 조정으로 수량 조정 {quantity_before}→{quantity}주")
                if quantity <= 0:
                    logger.log_system(f"주문 불가: {symbol}, 주문 단위 조정 후 수량이 0입니다.")
                    return {"can_order": False}
            
            # 최종 주문 금액 계산
            order_amount = current_price * quantity
            
            # 예상 수수료 계산
            estimated_fee = order_amount * fee_rate
            total_cost = order_amount + estimated_fee
            
            # 최종 안전성 검증 - 총 비용이 가용 금액을 초과하는지
            if total_cost > available_amount:
                # 수량 추가 조정
                safe_quantity = int((available_amount / (current_price * (1 + fee_rate))))
                
                # 안전 마진 추가 (70%로 조정 - 80%에서 감소)
                safe_quantity = int(safe_quantity * 0.7)
                
                # 주문 단위 조정
                if order_unit > 1:
                    safe_quantity = (safe_quantity // order_unit) * order_unit
                
                if safe_quantity > 0:
                    quantity = safe_quantity
                    order_amount = current_price * quantity
                    logger.log_system(f"주문 최종 조정: {symbol}, 가용 금액 초과로 안전하게 수량 재조정 → {quantity}주")
                else:
                    logger.log_system(f"주문 불가: {symbol}, 가용 금액 내에서 주문 가능한 수량이 없습니다.")
                    return {"can_order": False}
            
            # 최종 주문 정보 로깅
            logger.log_system(f"주문 계산 완료: {symbol}, 수량={quantity}주, 주문금액={order_amount:,.0f}원, 예상수수료={estimated_fee:,.0f}원")
            
            return {
                "can_order": True,
                "quantity": quantity,
                "order_amount": order_amount,
                "estimated_total": total_cost
            }
        except Exception as e:
            logger.log_error(e, f"주문 수량 계산 중 오류: {symbol}")
            return {"can_order": False}

    async def process_buy_order(self, symbol, signal_score, remaining_investment, per_stock_amount):
        """매수 주문 처리"""
        try:
            # 현재가 조회
            symbol_info = await asyncio.wait_for(
                api_client.get_symbol_info(symbol),
                timeout=2.0
            )
            
            if not symbol_info or "current_price" not in symbol_info:
                logger.log_system(f"현재가 조회 실패: {symbol}")
                return {"success": False}
            
            current_price = symbol_info["current_price"]
            
            # 가격 급등 확인 (전일 종가 대비)
            if "prev_close" in symbol_info and symbol_info["prev_close"] > 0:
                prev_close = symbol_info["prev_close"]
                price_change_rate = (current_price - prev_close) / prev_close * 100
                
                # 급등 종목 필터링 (전일 대비 7% 이상 상승 - 10%에서 하향 조정)
                if price_change_rate > 7.0:
                    logger.log_system(f"가격 급등으로 매수 제한: {symbol}, 전일대비={price_change_rate:.1f}%")
                    return {"success": False}
            
            # 점수에 따른 투자금액 조정 (6.0-10.0점 범위)
            score_weight = min(1.0, (signal_score - 6.0) / 4.0 + 0.6)  # 0.6-1.0 범위로 조정
            adjusted_amount = min(per_stock_amount * score_weight, remaining_investment)
            
            # 보수적인 주문을 위해 금액 추가 조정 (최대 70%만 사용)
            conservative_amount = adjusted_amount * 0.7
            
            # 계좌 상태 확인
            logger.log_system(f"매수 시도 전 계좌 상태 확인: {symbol}")
            await order_manager.sync_account_state()
            
            # 주문 수량 및 금액 계산
            order_info = self.calculate_order_quantity(symbol, current_price, conservative_amount)
            if not order_info["can_order"]:
                logger.log_system(f"주문 수량 계산 결과 주문 불가: {symbol}")
                return {"success": False}
            
            # 주문 실행
            quantity = order_info["quantity"]
            order_amount = order_info["order_amount"]
            
            # 주문 시작 전 충분한 대기 시간 추가 (이전 주문 처리 완료 대기)
            await asyncio.sleep(1.0)
            
            # 대량 주문인 경우 분할 주문 적용
            if quantity > 200:  # 200주 이상이면 분할 주문
                split_qty = quantity // 2
                remainder_qty = quantity - split_qty
                logger.log_system(f"대량 주문 분할: {symbol}, 원래 수량={quantity}주 → 1차 {split_qty}주 + 2차 {remainder_qty}주")
                
                # 1차 주문
                logger.log_system(f"매수 주문 실행 (1차): {symbol}, 가격={current_price:,.0f}원, 수량={split_qty}주")
                try:
                    first_order = await asyncio.wait_for(
                        order_manager.place_order(
                            symbol=symbol,
                            side="BUY",
                            quantity=split_qty,
                            price=current_price,
                            order_type="LIMIT",
                            strategy="main_bot",
                            reason="strategy_signal_split_1"
                        ),
                        timeout=5.0
                    )
                    
                    if first_order and first_order.get("status") == "success":
                        logger.log_system(f"✅ 매수 주문 성공 (1차): {symbol}, 주문ID={first_order.get('order_id')}")
                        
                        # 1차 주문 성공 후 잠시 대기 (5초로 증가)
                        await asyncio.sleep(5.0)
                        
                        # 2차 주문
                        logger.log_system(f"매수 주문 실행 (2차): {symbol}, 가격={current_price:,.0f}원, 수량={remainder_qty}주")
                        second_order = await asyncio.wait_for(
                            order_manager.place_order(
                                symbol=symbol,
                                side="BUY",
                                quantity=remainder_qty,
                                price=current_price,
                                order_type="LIMIT",
                                strategy="main_bot",
                                reason="strategy_signal_split_2"
                            ),
                            timeout=5.0
                        )
                        
                        if second_order and second_order.get("status") == "success":
                            logger.log_system(f"✅ 매수 주문 성공 (2차): {symbol}, 주문ID={second_order.get('order_id')}")
                            success = True
                        else:
                            # 2차 주문만 실패 (1차 주문 성공)
                            error_reason = second_order.get("reason", "알 수 없는 오류")
                            logger.log_system(f"❌ 매수 주문 실패 (2차): {symbol}, 사유={error_reason}")
                            # 1차 주문은 성공했으므로 부분 성공으로 처리
                            success = True
                            order_amount = split_qty * current_price  # 금액 조정
                    else:
                        # 1차 주문 실패
                        error_reason = first_order.get("reason", "알 수 없는 오류")
                        logger.log_system(f"❌ 매수 주문 실패 (1차): {symbol}, 사유={error_reason}")
                        return {"success": False}
                        
                except (asyncio.TimeoutError, Exception) as e:
                    logger.log_error(e, f"{symbol} 분할 주문 처리 중 오류")
                    return {"success": False}
                
                # 주문 정보 로깅
                logger.log_trade(
                    action="BUY",
                    symbol=symbol,
                    price=current_price,
                    quantity=quantity,  # 원래 의도한 총 수량
                    reason="전략 신호에 따른 자동 매수 (분할 주문)",
                    strategy="main_bot",
                    score=f"{signal_score:.1f}",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="SUCCESS"
                )
                
                # 주문 후 계좌 상태 동기화 (5초 대기 후)
                await asyncio.sleep(5.0)
                await order_manager.sync_account_state(force=True)
                
                return {"success": True, "order_amount": order_amount}
            
            # 일반 주문 (200주 미만)
            logger.log_system(f"매수 주문 실행: {symbol}, 가격={current_price:,.0f}원, 수량={quantity}주, 총액={order_amount:,.0f}원")
            
            try:
                order_result = await asyncio.wait_for(
                    order_manager.place_order(
                        symbol=symbol,
                        side="BUY",
                        quantity=quantity,
                        price=current_price,
                        order_type="LIMIT",  # 지정가 주문
                        strategy="main_bot",
                        reason="strategy_signal"
                    ),
                    timeout=5.0
                )
                
                if order_result and order_result.get("status") == "success":
                    logger.log_system(f"✅ 매수 주문 성공: {symbol}, 주문ID={order_result.get('order_id')}")
                    
                    # 주문 정보 로깅
                    logger.log_trade(
                        action="BUY",
                        symbol=symbol,
                        price=current_price,
                        quantity=quantity,
                        reason="전략 신호에 따른 자동 매수",
                        strategy="main_bot",
                        score=f"{signal_score:.1f}",
                        time=datetime.now().strftime("%H:%M:%S"),
                        status="SUCCESS"
                    )
                    
                    # 주문 후 계좌 상태 동기화 (3초 대기 후)
                    await asyncio.sleep(3.0)
                    await order_manager.sync_account_state(force=True)
                    
                    return {"success": True, "order_amount": order_amount}
                else:
                    error_reason = order_result.get("reason", "알 수 없는 오류")
                    logger.log_system(f"❌ 매수 주문 실패: {symbol}, 사유={error_reason}")
                    return {"success": False}
                    
            except (asyncio.TimeoutError, Exception) as e:
                logger.log_error(e, f"{symbol} 주문 처리 중 오류")
                return {"success": False}
                
        except (asyncio.TimeoutError, Exception) as e:
            logger.log_error(e, f"{symbol} 현재가 조회 중 오류")
            return {"success": False}
    
    async def _initial_symbol_scan(self) -> None:
        """초기 종목 스캔 - 5개 전략 사용하여 상위 100개 선정"""
        global MONITORED_SYMBOLS, LAST_SYMBOL_UPDATE
        
        try:
            logger.log_system("=== 초기 종목 스캔 시작 (5개 전략 사용) ===")
            
            # 전략들이 준비될 때까지 잠시 대기
            #logger.log_system("전략 초기화 대기 중...")
            #await asyncio.sleep(3)
            
            # 1. 5개 전략으로 종목 분석
            top_symbols = await self._analyze_symbols_with_strategies()
            
            if not top_symbols:
                logger.log_warning("초기 종목 스캔 결과가 없습니다. 대안으로 거래량 상위 종목 사용")
                # 대안: 거래량 상위 100개 종목 사용
                top_symbols = await stock_explorer.get_tradable_symbols(market_type="ALL")
                top_symbols = top_symbols[:100]
                
                # 거래량 상위 종목도 없는 경우
                if not top_symbols:
                    logger.log_error("거래량 상위 종목도 찾을 수 없습니다.")
                    return
            
            # 2. 전역 변수에 저장
            MONITORED_SYMBOLS = top_symbols[:100]  # 상위 100개만
            LAST_SYMBOL_UPDATE = datetime.now()
            
            logger.log_system(f"초기 종목 스캔 완료: {len(MONITORED_SYMBOLS)}개 종목 선정")
            #logger.log_system(f"상위 10개 종목: {', '.join(MONITORED_SYMBOLS[:10])}")
            
            # 3. 통합 전략에 종목 업데이트 (30개만 사용)
            await combined_strategy.update_symbols(MONITORED_SYMBOLS[:30])
            
            # 4. 전략 시작 (이미 시작된 경우 무시됨)
            if not combined_strategy.running:
                await combined_strategy.start(MONITORED_SYMBOLS[:30])
                logger.log_system("통합 전략 시작 완료")
            else:
                logger.log_system("통합 전략이 이미 실행 중입니다")
            
            # 5. 스캔 결과 로그
            logger.log_trade(
                action="INITIAL_SCAN_COMPLETE",
                symbol="SYSTEM",
                price=0,
                quantity=len(MONITORED_SYMBOLS),
                reason=f"초기 종목 스캔 완료",
                top_symbols=", ".join(MONITORED_SYMBOLS[:10]),
                time=datetime.now().strftime("%H:%M:%S"),
                status="SUCCESS"
            )
            
        except Exception as e:
            logger.log_error(e, "초기 종목 스캔 중 오류 발생")
            # 오류 발생 시 기본 거래량 상위 종목 사용
            try:
                MONITORED_SYMBOLS = await stock_explorer.get_tradable_symbols(market_type="ALL")
                MONITORED_SYMBOLS = MONITORED_SYMBOLS[:100] if MONITORED_SYMBOLS else []
                LAST_SYMBOL_UPDATE = datetime.now()
                
                if MONITORED_SYMBOLS:
                    logger.log_system(f"대안으로 거래량 상위 {len(MONITORED_SYMBOLS)}개 종목 사용")
                else:
                    logger.log_error("대안 종목도 찾을 수 없습니다")
            except Exception as fallback_error:
                logger.log_error(fallback_error, "대안 종목 탐색 중 오류 발생")
                MONITORED_SYMBOLS = []
                LAST_SYMBOL_UPDATE = datetime.now()
    
    async def _analyze_symbols_with_strategies(self) -> List[str]:
        """5개 전략을 사용하여 종목 분석 및 점수 계산"""
        try:
            # 1. 거래 가능한 모든 종목 가져오기
            all_symbols = await stock_explorer.get_tradable_symbols(market_type="ALL")
            
            if not all_symbols:
                logger.log_warning("거래 가능한 종목이 없습니다")
                return []
            
            logger.log_system(f"분석 대상 종목 수: {len(all_symbols)}개")
            
            # 2. 각 종목에 대해 5개 전략으로 신호 계산
            symbol_scores = {}
            
            # 각 전략 준비
            strategies = {
                'breakout': combined_strategy.strategies.get('breakout'),
                'momentum': combined_strategy.strategies.get('momentum'),
                'gap': combined_strategy.strategies.get('gap'),
                'vwap': combined_strategy.strategies.get('vwap'),
                'volume': combined_strategy.strategies.get('volume')
            }
            
            # 전략 유효성 확인
            valid_strategies = {}
            for name, strategy in strategies.items():
                if strategy and hasattr(strategy, 'get_signal'):
                    valid_strategies[name] = strategy
                    logger.log_system(f"전략 확인: {name} - OK")
                else:
                    logger.log_warning(f"전략 확인: {name} - 사용 불가")
            
            if not valid_strategies:
                logger.log_warning("사용 가능한 전략이 없습니다")
                return []
            
            # 분석 시간 고려하여 상위 30개만 분석
            analysis_symbols = all_symbols[:30]
            
            # *** 전략들이 데이터를 준비하도록 초기화 ***
            logger.log_system("전략 데이터 준비 시작...")
            
            for strategy_name, strategy in valid_strategies.items():
                try:
                    # 전략별로 초기 데이터 준비
                    if hasattr(strategy, '_load_initial_data'):
                        #logger.log_system(f"{strategy_name} 전략 초기 데이터 로딩 중...")
                        
                        # 각 심볼에 대해 초기 데이터 로딩
                        for symbol in analysis_symbols[:30]:
                            try:
                                # 전략의 price_data 초기화
                                if not hasattr(strategy, 'price_data'):
                                    strategy.price_data = {}
                                
                                # deque 초기화
                                from collections import deque
                                max_period = 100  # 충분한 데이터 저장을 위해
                                if hasattr(strategy, 'params'):
                                    # 전략별 파라미터 확인
                                    if strategy_name == 'momentum':
                                        max_period = max(strategy.params.get('rsi_period', 14), 
                                                       strategy.params.get('ma_long_period', 20)) * 2
                                    elif strategy_name == 'breakout':
                                        max_period = strategy.params.get('period', 20) * 2
                                    elif strategy_name == 'gap':
                                        max_period = strategy.params.get('confirmation_period', 10) * 2
                                    elif strategy_name == 'vwap':
                                        max_period = strategy.params.get('confirmation_bars', 3) * 2
                                    elif strategy_name == 'volume':
                                        max_period = strategy.params.get('volume_sma_period', 20) * 2
                                
                                strategy.price_data[symbol] = deque(maxlen=max_period)
                                
                                # indicators 초기화
                                if not hasattr(strategy, 'indicators'):
                                    strategy.indicators = {}
                                
                                strategy.indicators[symbol] = {
                                    'rsi': None,
                                    'ma_short': None,
                                    'ma_long': None,
                                    'macd': None,
                                    'macd_signal': None,
                                    'prev_rsi': None,
                                    'prev_ma_cross': False
                                }
                                
                                # watched_symbols 초기화 (일부 전략에서 필요)
                                if not hasattr(strategy, 'watched_symbols'):
                                    strategy.watched_symbols = set()
                                strategy.watched_symbols.add(symbol)
                                
                                # 초기 데이터 로딩
                                await strategy._load_initial_data(symbol)
                                
                            except Exception as e:
                                logger.log_error(e, f"{strategy_name} - {symbol} 초기 데이터 로딩 실패")
                                
                except Exception as e:
                    logger.log_error(e, f"{strategy_name} 전략 데이터 준비 실패")
            
            #logger.log_system("전략 데이터 준비 완료")
            # *** 초기화 끝 ***
            
            for idx, symbol in enumerate(analysis_symbols):
                try:
                    # 진행률 로깅 (20개마다)
                    #if idx % 20 == 0:
                    #    logger.log_system(f"종목 분석 진행률: {idx}/{len(analysis_symbols)}")
                    
                    total_score = 0
                    buy_votes = 0
                    strategy_signals = {}
                    
                    # 각 전략에서 신호 가져오기
                    for strategy_name, strategy in valid_strategies.items():
                        try:
                            # 타임아웃 설정 (2초)
                            signal_task = asyncio.create_task(strategy.get_signal(symbol))
                            signal = await asyncio.wait_for(signal_task, timeout=2.0)
                            
                            if signal and isinstance(signal, dict):
                                # 신호 저장
                                strategy_signals[strategy_name] = signal
                                
                                # 신호 강도 누적
                                signal_value = float(signal.get('signal', 0))
                                total_score += signal_value
                                
                                # BUY 신호인 경우 투표
                                if signal.get('direction') == 'BUY':
                                    buy_votes += 1
                                
                                # logger.log_system(f"{symbol} - {strategy_name}: signal={signal_value:.1f}, direction={signal.get('direction')}")
                            else:
                                logger.log_system(f"{symbol} - {strategy_name}: 신호 없음")
                                
                        except asyncio.TimeoutError:
                            logger.log_warning(f"{symbol} - {strategy_name} 전략 타임아웃")
                        except Exception as strategy_error:
                            logger.log_error(strategy_error, f"{symbol} - {strategy_name} 전략 오류")
                    
                    # 종합 점수 저장 (BUY 투표 수와 신호 강도 모두 고려)
                    if buy_votes >= 2:  # 최소 2개 전략이 BUY 신호
                        symbol_scores[symbol] = {
                            'total_score': total_score,
                            'buy_votes': buy_votes,
                            'signals': strategy_signals
                        }
                        logger.log_system(f"{symbol} - 종합: BUY={buy_votes}, 점수={total_score:.1f}")
                    
                except Exception as e:
                    logger.log_error(e, f"종목 {symbol} 분석 중 오류")
                    continue
            
            # 3. 점수 기준으로 정렬 (buy_votes 우선, total_score 차선)
            sorted_symbols = sorted(
                symbol_scores.items(),
                key=lambda x: (x[1]['buy_votes'], x[1]['total_score']),
                reverse=True
            )
            
            # 4. 상위 30개 종목만 반환
            top_symbols = [item[0] for item in sorted_symbols[:30]]
            
            logger.log_system(f"전략 분석 완료: {len(top_symbols)}개 종목 선정")
            #logger.log_system(f"상위 5개 종목 상세:")
            #for i, (symbol, score_data) in enumerate(sorted_symbols[:5]):
            #    logger.log_system(f"{i+1}. {symbol}: BUY={score_data['buy_votes']}, 점수={score_data['total_score']:.1f}")
            
            return top_symbols
            
        except Exception as e:
            logger.log_error(e, "전략 기반 종목 분석 중 오류")
            return []
    
    def _should_rescan_symbols(self, current_time: datetime_time) -> bool:
        """종목 재스캔이 필요한지 확인"""
        global LAST_SYMBOL_UPDATE
        
        # 마지막 업데이트가 없으면 스캔 필요
        if LAST_SYMBOL_UPDATE is None:
            return True
        
        # 현재 시간이 8:30 ~ 8:40 사이이고, 오늘 아직 스캔하지 않았다면
        if datetime_time(8, 30) <= current_time <= datetime_time(8, 40):
            last_update_date = LAST_SYMBOL_UPDATE.date()
            current_date = datetime.now().date()
            
            if last_update_date < current_date:
                return True
        
        # 마지막 업데이트로부터 6시간 이상 경과했다면
        if (datetime.now() - LAST_SYMBOL_UPDATE).total_seconds() > 6 * 60 * 60:
            return True
        
        return False
    
    async def _rescan_symbols(self) -> None:
        """종목 재스캔"""
        global MONITORED_SYMBOLS, LAST_SYMBOL_UPDATE
        
        try:
            logger.log_system("=== 종목 재스캔 시작 (장 시작 전 또는 정기 업데이트) ===")
            
            # 기존 전략 중지
            await combined_strategy.stop()
            
            # 새로운 종목 분석
            new_symbols = await self._analyze_symbols_with_strategies()
            
            if not new_symbols:
                logger.log_warning("재스캔 결과가 없습니다. 기존 종목 유지")
                return
            
            # 전역 변수 업데이트
            old_symbols = MONITORED_SYMBOLS.copy()
            MONITORED_SYMBOLS = new_symbols[:30]
            LAST_SYMBOL_UPDATE = datetime.now()
            
            # 변경된 종목 로깅
            added_symbols = set(MONITORED_SYMBOLS) - set(old_symbols)
            removed_symbols = set(old_symbols) - set(MONITORED_SYMBOLS)
            
            logger.log_system(f"종목 재스캔 완료: {len(MONITORED_SYMBOLS)}개 종목")
            logger.log_system(f"추가된 종목: {len(added_symbols)}개")
            logger.log_system(f"제거된 종목: {len(removed_symbols)}개")
            
            # 통합 전략 업데이트 및 재시작 (30개만 사용)
            await combined_strategy.update_symbols(MONITORED_SYMBOLS[:30])
            await combined_strategy.start(MONITORED_SYMBOLS[:30])
            
            # 재스캔 결과 로그
            logger.log_trade(
                action="RESCAN_COMPLETE",
                symbol="SYSTEM",
                price=0,
                quantity=len(MONITORED_SYMBOLS),
                reason=f"종목 재스캔 완료",
                added_count=len(added_symbols),
                removed_count=len(removed_symbols),
                time=datetime.now().strftime("%H:%M:%S"),
                status="SUCCESS"
            )
            
        except Exception as e:
            logger.log_error(e, "종목 재스캔 중 오류 발생")
            # 오류 발생 시 기존 종목으로 전략 재시작
            await combined_strategy.start(MONITORED_SYMBOLS[:30])
    
    def _is_market_open(self, current_time: datetime_time) -> bool:
        """장 시간 확인"""
        return (self.trading_config.market_open <= current_time <= 
                self.trading_config.market_close)
    
    async def _handle_market_close(self):
        """장 마감 처리"""
        try:
            # 일일 요약
            summary = await order_manager.get_daily_summary()
            
            # 성과 기록
            if summary.get('date') is None:
                summary['date'] = datetime.now().strftime('%Y-%m-%d')
            
            # 필요한 필드들 기본값 설정
            if 'win_rate' not in summary:
                summary['win_rate'] = 0.0
            if 'total_pnl' not in summary:
                summary['total_pnl'] = summary.get('daily_pnl', 0)
            if 'top_gainers' not in summary:
                summary['top_gainers'] = []
            if 'top_losers' not in summary:
                summary['top_losers'] = []
                
            # 포트폴리오 가치 추가
            if 'portfolio_value' not in summary:
                try:
                    account_balance = await order_manager.get_account_balance()
                    summary['portfolio_value'] = account_balance.get('total_balance', 0)
                except Exception as e:
                    logger.log_warning(f"포트폴리오 가치 조회 실패: {str(e)}")
                    summary['portfolio_value'] = 0
            
            # 데이터베이스 저장용 데이터 필터링 (performance 테이블에 존재하는 컬럼만 포함)
            db_summary = {
                'date': summary.get('date'),
                'daily_pnl': summary.get('daily_pnl', 0),
                'daily_trades': summary.get('daily_trades', 0),
                'win_rate': summary.get('win_rate', 0.0),
                'total_pnl': summary.get('total_pnl', 0)
                # top_gainers, top_losers, portfolio_value는 제외
            }
            
            # 필터링된 데이터로 성과 저장
            database_manager.save_performance(db_summary)
            
            # 일일 리포트 전송 (원본 summary 사용 - 모든 데이터 포함)
            await alert_system.send_daily_report(summary)
            
            # 데이터베이스 백업 제거
            # database_manager.backup_database()
            
            logger.log_system("Market closed. Daily process completed")
            
        except Exception as e:
            logger.log_error(e, "Market close handling error")
    
    async def shutdown(self, error: Optional[str] = None) -> None:
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
                database_manager.update_system_status("ERROR", error)

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
                database_manager.update_system_status("STOPPED")

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

async def main(force_update: bool = False) -> int:
    """메인 함수"""
    # 초기 설정
    bot: TradingBot = TradingBot()
    telegram_task = None
    heartbeat_task = None
    exit_code = 0
    
    # 워치독 타이머 설정
    last_heartbeat = datetime.now()
    watchdog_interval = 30 * 60 * 2 # 60분 (초 단위)
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
        # 현재 루프 기록
        current_loop = asyncio.get_running_loop()
        logger.log_system(f"main.py 이벤트 루프 ID: {id(current_loop)}")
        
        # 이벤트가 생성되었는지 최대 5초 대기
        wait_start = datetime.now()
        while telegram_bot_handler.ready_event is None:
            # 대기 시간이 5초를 넘으면 중단
            if (datetime.now() - wait_start).total_seconds() > 5:
                logger.log_error(Exception("Timeout waiting for ready_event creation"), 
                              "텔레그램 봇 ready_event 생성 대기 타임아웃")
                break
            logger.log_system("텔레그램 봇 ready_event 생성 대기 중...")
            await asyncio.sleep(0.5)
        
        # 이벤트가 생성되었으면 시그널 대기
        if telegram_bot_handler.ready_event is not None:
            logger.log_system("텔레그램 봇 핸들러 준비 대기 시작...")
            await asyncio.wait_for(telegram_bot_handler.ready_event.wait(), timeout=30)
            logger.log_system("텔레그램 봇 핸들러 준비 완료!")
            
            # 프로그램 시작 알림 전송
            await _send_startup_notification()
        else:
            logger.log_error(Exception("ready_event not created"), 
                          "텔레그램 봇 ready_event가 생성되지 않음")
            
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


async def _run_trading_bot(bot: TradingBot, force_update: bool = False):
    """메인 봇 실행"""
    # API 초기화 시도
    logger.log_system("Starting main bot execution...")
    try:
        # 봇 초기화
        await bot.initialize()
        # logger.log_system("API 초기화 성공!")
        
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


async def _graceful_shutdown(bot: TradingBot):
    """정상 종료 처리"""
    if bot:
        await bot.shutdown()
        # 종료 메시지가 확실히 전송될 수 있도록 대기
        await asyncio.sleep(2)


async def _emergency_shutdown(bot: TradingBot, error: str = None):
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
                # logger.log_system("하트비트 타임아웃으로 인한 복구 조치 시작...")
                
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
