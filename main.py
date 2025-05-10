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
from typing import List, Optional, Dict, Any

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
            
            # 주문 관리자 초기화
            await order_manager.initialize()
            
            # 전략들이 제대로 로드되었는지 확인
            logger.log_system("전략 확인 시작...")
            strategies = combined_strategy.strategies
            logger.log_system(f"전략 갯수: {len(strategies)}")
            
            for name, strategy in strategies.items():
                if strategy:
                    logger.log_system(f"전략 {name}: OK")
                    if hasattr(strategy, 'get_signal'):
                        logger.log_system(f"전략 {name}: get_signal 메서드 존재")
                    else:
                        logger.log_warning(f"전략 {name}: get_signal 메서드 부재")
                else:
                    logger.log_warning(f"전략 {name}: None")
            
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
            
            # 2. 메인 루프 시작
            logger.log_system("메인 루프 시작 - 주기적 종목 모니터링 실행")
            
            while self.running:
                try:
                    current_time = datetime.now().time()
                    
                    # 3. 장 시작 30분 전 (8:30) 또는 오래된 데이터일 경우 종목 재스캔
                    if self._should_rescan_symbols(current_time):
                        await self._rescan_symbols()
                    
                    # 4. 장 시간 체크 및 거래 실행
                    if self._is_market_open(current_time):
                        # 포지션 체크
                        await order_manager.check_positions()
                        
                        # 매수 신호 체크 및 주문 실행 로직 추가
                        if len(MONITORED_SYMBOLS) > 0:
                            # 2분마다 신호 체크
                            now = datetime.now()
                            if now.minute % 2 == 0 and now.second < 10:  # 2분마다, 매 분의 처음 10초 내에 실행
                                logger.log_system("======== 매수 신호 체크 시작 ========")
                                
                                # 모니터링 중인 종목 중 상위 50개만 체크
                                symbols_to_check = MONITORED_SYMBOLS[:50]
                                logger.log_system(f"체크할 종목 수: {len(symbols_to_check)}개")
                                
                                # 계좌 잔고 조회로 주문 금액 계산
                                try:
                                    account_balance = await order_manager.get_account_balance()
                                    balance_data = account_balance.get("output1", {})
                                    
                                    # 예수금 총액 가져오기
                                    deposit_balance = float(balance_data.get("dnca_tot_amt", "0"))
                                    
                                    # 주문 금액은 예수금의 50%
                                    order_amount = deposit_balance * 0.5
                                    
                                    if order_amount <= 0:
                                        logger.log_system(f"계좌 잔고 부족으로 매수 신호 체크 중단: {deposit_balance:,.0f}원")
                                        continue
                                    
                                    logger.log_system(f"계좌 잔고: {deposit_balance:,.0f}원, 주문 금액: {order_amount:,.0f}원 (50%)")
                                    
                                except Exception as balance_error:
                                    logger.log_error(balance_error, "계좌 잔고 조회 실패")
                                    # 기본값으로 계속 진행
                                    order_amount = 1000000  # 100만원 기본값
                                    logger.log_system(f"계좌 잔고 조회 실패로 기본값 사용: {order_amount:,.0f}원")
                                
                                # 종목별 매수 신호 확인
                                buy_signals_found = 0
                                orders_placed = 0
                                
                                for symbol in symbols_to_check:
                                    try:
                                        # 동시에 너무 많은 API 호출 방지를 위한 딜레이
                                        await asyncio.sleep(0.2)
                                        
                                        # 통합 전략에서 신호 얻기
                                        strategy_status = combined_strategy.get_strategy_status(symbol)
                                        
                                        # 신호 정보 유효성 확인
                                        if (symbol not in strategy_status.get("signals", {}) or 
                                            "direction" not in strategy_status["signals"][symbol]):
                                            continue
                                        
                                        # 매수 신호 및 점수 확인
                                        signal_info = strategy_status["signals"][symbol]
                                        signal_direction = signal_info["direction"]
                                        signal_score = signal_info.get("score", 0)
                                        
                                        # 매수 신호 및 최소 점수(6.0) 확인
                                        if signal_direction == "BUY" and signal_score >= 6.0:
                                            buy_signals_found += 1
                                            logger.log_system(f"[{buy_signals_found}] 매수 신호 발견: {symbol}, 점수={signal_score:.1f}")
                                            
                                            # 현재가 조회
                                            try:
                                                symbol_info = await asyncio.wait_for(
                                                    api_client.get_symbol_info(symbol),
                                                    timeout=2.0
                                                )
                                                
                                                if symbol_info and "current_price" in symbol_info:
                                                    current_price = symbol_info["current_price"]
                                                    
                                                    # 주문 수량 계산 (계좌 잔고의 50%를 사용)
                                                    quantity = max(1, int(order_amount / current_price))
                                                    
                                                    # 주문 금액이 너무 큰 경우 실수 방지를 위한 안전장치
                                                    max_order_value = 5000000  # 최대 500만원
                                                    if quantity * current_price > max_order_value:
                                                        old_quantity = quantity
                                                        quantity = int(max_order_value / current_price)
                                                        logger.log_system(f"안전장치 작동: 주문 수량 제한 {old_quantity}→{quantity}주 (최대 {max_order_value:,.0f}원)")
                                                    
                                                    # 주문 실행
                                                    logger.log_system(f"매수 주문 실행: {symbol}, 가격={current_price:,.0f}원, 수량={quantity}주, 총액={current_price * quantity:,.0f}원")
                                                    
                                                    try:
                                                        order_result = await asyncio.wait_for(
                                                            order_manager.place_order(
                                                                symbol=symbol,
                                                                side="BUY",
                                                                quantity=quantity,
                                                                price=current_price,
                                                                order_type="MARKET",
                                                                strategy="main_bot",
                                                                reason="strategy_signal"
                                                            ),
                                                            timeout=5.0
                                                        )
                                                        
                                                        if order_result and order_result.get("status") == "success":
                                                            orders_placed += 1
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
                                                            
                                                            # 한 번에 너무 많은 주문 방지
                                                            if orders_placed >= 3:
                                                                logger.log_system("최대 주문 수 도달 (3개), 추가 주문 중단")
                                                                break
                                                        else:
                                                            logger.log_system(f"❌ 매수 주문 실패: {symbol}, 사유={order_result.get('reason')}")
                                                    except asyncio.TimeoutError:
                                                        logger.log_system(f"⏱️ 매수 주문 타임아웃: {symbol}")
                                                    except Exception as order_error:
                                                        logger.log_error(order_error, f"{symbol} 매수 주문 처리 중 오류")
                                            except asyncio.TimeoutError:
                                                logger.log_system(f"현재가 조회 타임아웃: {symbol}")
                                            except Exception as price_error:
                                                logger.log_error(price_error, f"{symbol} 현재가 조회 중 오류")
                                        
                                    except Exception as signal_error:
                                        logger.log_error(signal_error, f"{symbol} 매수 신호 처리 중 오류")
                                
                                # 종목 신호 체크 결과 요약
                                logger.log_system(f"매수 신호 체크 완료: 발견={buy_signals_found}개, 주문 실행={orders_placed}개")
                                logger.log_system("======== 매수 신호 체크 종료 ========")
                        
                        # 시스템 상태 업데이트
                        database_manager.update_system_status("RUNNING")
                        
                        # 주기적 상태 로깅 (1분마다)
                        if datetime.now().second < 5:
                            logger.log_system(f"시스템 실행 중 - 현재 시간: {current_time}, 모니터링 종목 수: {len(MONITORED_SYMBOLS)}")
                    else:
                        # 장 마감 처리
                        if current_time > self.trading_config.market_close:
                            await self._handle_market_close()
                    
                    # 5초 대기
                    await asyncio.sleep(5)
                    
                except Exception as loop_error:
                    logger.log_error(loop_error, "메인 루프 내부 처리 중 오류 발생")
                    await asyncio.sleep(5)
                
        except Exception as e:
            logger.log_error(e, "Trading bot error")
            await self.shutdown(error=str(e))
    
    async def _initial_symbol_scan(self) -> None:
        """초기 종목 스캔 - 5개 전략 사용하여 상위 100개 선정"""
        global MONITORED_SYMBOLS, LAST_SYMBOL_UPDATE
        
        try:
            logger.log_system("=== 초기 종목 스캔 시작 (5개 전략 사용) ===")
            
            # 전략들이 준비될 때까지 잠시 대기
            logger.log_system("전략 초기화 대기 중...")
            await asyncio.sleep(3)
            
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
            logger.log_system(f"상위 10개 종목: {', '.join(MONITORED_SYMBOLS[:10])}")
            
            # 3. 통합 전략에 종목 업데이트 (50개만 사용)
            await combined_strategy.update_symbols(MONITORED_SYMBOLS[:50])
            
            # 4. 전략 시작 (이미 시작된 경우 무시됨)
            if not combined_strategy.running:
                await combined_strategy.start(MONITORED_SYMBOLS[:50])
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
            
            # 분석 시간 고려하여 상위 200개만 분석
            analysis_symbols = all_symbols[:200]
            
            for idx, symbol in enumerate(analysis_symbols):
                try:
                    # 진행률 로깅 (20개마다)
                    if idx % 20 == 0:
                        logger.log_system(f"종목 분석 진행률: {idx}/{len(analysis_symbols)}")
                    
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
                                
                                logger.log_system(f"{symbol} - {strategy_name}: signal={signal_value:.1f}, direction={signal.get('direction')}")
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
            
            # 4. 상위 100개 종목만 반환
            top_symbols = [item[0] for item in sorted_symbols[:100]]
            
            logger.log_system(f"전략 분석 완료: {len(top_symbols)}개 종목 선정")
            logger.log_system(f"상위 5개 종목 상세:")
            for i, (symbol, score_data) in enumerate(sorted_symbols[:5]):
                logger.log_system(f"{i+1}. {symbol}: BUY={score_data['buy_votes']}, 점수={score_data['total_score']:.1f}")
            
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
            MONITORED_SYMBOLS = new_symbols[:100]
            LAST_SYMBOL_UPDATE = datetime.now()
            
            # 변경된 종목 로깅
            added_symbols = set(MONITORED_SYMBOLS) - set(old_symbols)
            removed_symbols = set(old_symbols) - set(MONITORED_SYMBOLS)
            
            logger.log_system(f"종목 재스캔 완료: {len(MONITORED_SYMBOLS)}개 종목")
            logger.log_system(f"추가된 종목: {len(added_symbols)}개")
            logger.log_system(f"제거된 종목: {len(removed_symbols)}개")
            
            # 통합 전략 업데이트 및 재시작 (50개만 사용)
            await combined_strategy.update_symbols(MONITORED_SYMBOLS[:50])
            await combined_strategy.start(MONITORED_SYMBOLS[:50])
            
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
            await combined_strategy.start(MONITORED_SYMBOLS[:50])
    
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
            database_manager.save_performance(summary)
            
            # 일일 리포트 전송
            await alert_system.send_daily_report(summary)
            
            # 데이터베이스 백업
            database_manager.backup_database()
            
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


async def _run_trading_bot(bot: TradingBot, force_update: bool = False):
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
