"""
브레이크아웃 전략 (Breakout Strategy)
장 시작 후 30분간의 가격 범위를 기준으로 돌파 시 매매하는 전략
"""
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime, time, timedelta
from collections import deque
import numpy as np

from config.settings import config
from core.api_client import api_client
from core.websocket_client import ws_client
from core.order_manager import order_manager
from utils.logger import logger
from monitoring.alert_system import alert_system

class BreakoutStrategy:
    """브레이크아웃 전략 클래스"""
    
    def __init__(self):
        # get 메서드 대신 직접 속성 접근 또는 기본값 설정
        self.params = {
            "k_value": 0.4,             # 돌파 레벨 계산 시 사용할 K값 (0.3~0.5)
            "stop_loss_pct": 0.5,       # 손절 비율 (시작 범위의 50%)
            "take_profit_pct": 1.5,     # 익절 비율 (시작 범위의 150%)
            "max_positions": 3,         # 최대 포지션 개수
            "position_size": 1000000    # 기본 포지션 크기 (100만원)
        }
        
        # 설정에 breakout_params가 있으면 업데이트
        if hasattr(config["trading"], "breakout_params"):
            self.params.update(config["trading"].breakout_params)
            
        self.running = False
        self.paused = False
        self.watched_symbols = set()
        self.price_data = {}          # {symbol: deque of price data}
        self.breakout_levels = {}     # {symbol: {'high_level': float, 'low_level': float, 'range': float}}
        self.positions = {}           # {position_id: position_data}
        self.initialization_complete = {} # {symbol: bool} - 초기화 완료 여부
        
        # 성능 최적화를 위한 캐시 및 세마포어 미리 초기화
        self._symbol_positions_cache = {}
        self._setup_semaphore = asyncio.Semaphore(5)   # 레벨 설정용 세마포어
        self._analysis_semaphore = asyncio.Semaphore(10)  # 분석용 세마포어
        self._exit_semaphore = asyncio.Semaphore(5)    # 포지션 청산용 세마포어
        
    async def start(self, symbols: List[str]):
        """전략 시작"""
        try:
            self.running = True
            self.paused = False
            self.watched_symbols = set(symbols)
            
            # 각 종목별 데이터 초기화
            for symbol in symbols:
                self.price_data[symbol] = deque(maxlen=300)  # 약 30분치 데이터 (6초당 1틱 가정)
                self.breakout_levels[symbol] = {
                    'high_level': None, 
                    'low_level': None, 
                    'range': None,
                    'init_high': None,
                    'init_low': None
                }
                self.initialization_complete[symbol] = False
                
                # 웹소켓 구독
                await ws_client.subscribe_price(symbol, self._handle_price_update)
            
            logger.log_system(f"Breakout strategy started for {len(symbols)} symbols")
            
            # 초기 데이터 로드 (병렬 처리)
            asyncio.create_task(self._load_initial_data_for_all_symbols(symbols))
            
            # 전략 실행 루프
            asyncio.create_task(self._strategy_loop())
            
        except Exception as e:
            logger.log_error(e, "Failed to start breakout strategy")
            await alert_system.notify_error(e, "Breakout strategy start error")
    
    async def stop(self):
        """전략 중지"""
        self.running = False
        
        # 웹소켓 구독 해제
        for symbol in self.watched_symbols:
            await ws_client.unsubscribe(symbol, "price")
        
        logger.log_system("Breakout strategy stopped")
    
    async def pause(self):
        """전략 일시 중지"""
        if not self.paused:
            self.paused = True
            logger.log_system("Breakout strategy paused")
        return True

    async def resume(self):
        """전략 재개"""
        if self.paused:
            self.paused = False
            logger.log_system("Breakout strategy resumed")
        return True
    
    async def _handle_price_update(self, data: Dict[str, Any]):
        """실시간 체결가 업데이트 처리 - 성능 최적화 버전"""
        try:
            # 필수 데이터 추출
            symbol = data.get("tr_key")
            price = float(data.get("stck_prpr", 0))
            
            # 유효성 검사 (빠른 실패)
            if symbol not in self.price_data or price <= 0:
                return
                
            # 현재 시간 한 번만 계산
            now = datetime.now()
            current_time = now.time()
            
            # 가격 데이터 저장
            self.price_data[symbol].append({
                "price": price,
                "timestamp": now
            })
            
            # 초기화 완료 상태에 따라 분기 (불필요한 체크 최소화)
            if self.initialization_complete.get(symbol, False):
                # 이미 초기화 완료된 경우 더 이상의 처리 불필요
                return
                
            # 시장 시간대 확인 (9:00-9:30 사이)
            is_collection_period = time(9, 0) <= current_time < time(9, 30)
            is_setup_time = current_time >= time(9, 30)
            
            if is_collection_period:
                # 초기화 중 최고가/최저가 업데이트 - 인라인 처리로 호출 오버헤드 감소
                breakout_data = self.breakout_levels[symbol]
                if breakout_data['init_high'] is None or price > breakout_data['init_high']:
                    breakout_data['init_high'] = price
                if breakout_data['init_low'] is None or price < breakout_data['init_low']:
                    breakout_data['init_low'] = price
                    
            elif is_setup_time:
                # 레벨 설정 태스크 생성 (비동기 처리)
                asyncio.create_task(self._setup_breakout_levels_with_timeout(symbol))
                    
        except Exception as e:
            logger.log_error(e, f"Error handling price update for {symbol}")
    
    async def _setup_breakout_levels_with_timeout(self, symbol: str):
        """타임아웃이 설정된 브레이크아웃 레벨 설정 함수 (중첩 함수 분리)"""
        async with self._setup_semaphore:
            try:
                await asyncio.wait_for(self._set_breakout_levels(symbol), timeout=5.0)
            except asyncio.TimeoutError:
                logger.log_warning(f"{symbol} - 브레이크아웃 레벨 설정 타임아웃")
            except Exception as e:
                logger.log_error(e, f"{symbol} - 브레이크아웃 레벨 설정 중 오류")
    
    async def _set_breakout_levels(self, symbol: str):
        """돌파 레벨 설정 (9:30에 실행) - 성능 최적화 버전"""
        try:
            if self.initialization_complete.get(symbol, False):
                return
                    
            breakout_data = self.breakout_levels[symbol]
            
            # 9:00~9:30 데이터에서 고가/저가 계산
            if breakout_data['init_high'] is None or breakout_data['init_low'] is None:
                # 실시간 데이터 부족한 경우 API로 조회 (비동기 처리)
                try:
                    # API 호출을 비동기로 처리하고 타임아웃 설정
                    price_data_task = asyncio.create_task(api_client.get_minute_price(symbol))
                    price_data = await asyncio.wait_for(price_data_task, timeout=3.0)
                    
                    if price_data.get("rt_cd") == "0":
                        # 데이터가 있는지 확인
                        chart_data = price_data.get("output2", [])
                        
                        if chart_data:
                            logger.log_system(f"{symbol} - 차트 데이터 {len(chart_data)}개 로드 성공")
                            
                            # 리스트 컴프리헨션 사용하여 필터링과 변환을 한 번에 처리
                            prices = []
                            times = []
                            
                            # 데이터 형식에 따라 적절한 필드 추출
                            for item in chart_data:
                                price_value = None
                                if "stck_prpr" in item:
                                    price_value = float(item["stck_prpr"])
                                elif "clos" in item:  # 종가(clos) 필드가 있는 경우
                                    price_value = float(item["clos"])
                                
                                time_value = item.get("time", "")
                                
                                if price_value is not None:
                                    prices.append(price_value)
                                    times.append(time_value)
                            
                            # 9:00~9:30 데이터만 필터링 (더 효율적인 방식)
                            filtered_prices = [
                                price for price, time_str in zip(prices, times)
                                if "090000" <= time_str <= "093000"
                            ]
                            
                            if filtered_prices:
                                # NumPy 이미 임포트되어 있으므로 중복 임포트 제거
                                breakout_data['init_high'] = np.max(filtered_prices)
                                breakout_data['init_low'] = np.min(filtered_prices)
                            else:
                                # 필터링된 데이터가 없으면 받아온 모든 데이터에서 처리
                                data_limit = min(30, len(prices))
                                if data_limit > 0:
                                    breakout_data['init_high'] = max(prices[:data_limit])
                                    breakout_data['init_low'] = min(prices[:data_limit])
                            
                            logger.log_system(f"{symbol} - 초기 브레이크아웃 레벨: 고가={breakout_data['init_high']}, 저가={breakout_data['init_low']}")
                        else:
                            logger.log_system(f"{symbol} - 차트 데이터 없음")
                except asyncio.TimeoutError:
                    logger.log_warning(f"{symbol} - 차트 데이터 조회 타임아웃 (3초)")
                except Exception as api_error:
                    logger.log_error(api_error, f"{symbol} - 차트 데이터 조회 중 오류")
            
            # 고가/저가가 설정되었는지 확인
            if breakout_data['init_high'] is None or breakout_data['init_low'] is None:
                logger.log_system(f"{symbol} - 브레이크아웃 레벨 설정 실패: 고가/저가 데이터 없음")
                return
            
            # 가격 범위 계산
            price_range = breakout_data['init_high'] - breakout_data['init_low']
            
            # 돌파 레벨 설정
            k_value = self.params["k_value"]
            breakout_data['high_level'] = breakout_data['init_high'] + (price_range * k_value)
            breakout_data['low_level'] = breakout_data['init_low'] - (price_range * k_value)
            breakout_data['range'] = price_range
            
            self.initialization_complete[symbol] = True
            
            logger.log_system(f"Breakout levels set for {symbol}: High={breakout_data['high_level']}, Low={breakout_data['low_level']}, Range={price_range}")
            
        except Exception as e:
            logger.log_error(e, f"Error setting breakout levels for {symbol}")
    
    async def _analyze_with_semaphore(self, symbol: str):
        """세마포어로 제한된 분석 함수 (중첩 함수 분리)"""
        async with self._analysis_semaphore:
            try:
                # 각 종목 분석에 타임아웃 설정
                await asyncio.wait_for(
                    self._analyze_and_trade(symbol),
                    timeout=2.0
                )
            except asyncio.TimeoutError:
                logger.log_warning(f"{symbol} - 분석 타임아웃 (2초)")
            except Exception as e:
                logger.log_error(e, f"{symbol} - 분석 중 오류")
    
    async def _strategy_loop(self):
        """전략 실행 루프 - 성능 최적화 버전"""
        while self.running:
            try:
                # 장 시간 체크
                current_time = datetime.now().time()
                if not (time(9, 0) <= current_time <= time(15, 30)):
                    await asyncio.sleep(60)  # 장 시간 아닌 경우 1분 대기
                    continue
                
                # 전략이 일시 중지된 경우 스킵
                if self.paused or order_manager.is_trading_paused():
                    await asyncio.sleep(1)
                    continue
                
                # 9:30 이후에만 트레이딩 실행
                if current_time >= time(9, 30):
                    # 병렬 분석 태스크 생성 (중첩 함수 제거)
                    analysis_tasks = []
                    
                    for symbol in self.watched_symbols:
                        # 초기화 완료된 종목만 분석
                        if self.initialization_complete.get(symbol, False):
                            # 태스크 생성 및 추가
                            task = asyncio.create_task(self._analyze_with_semaphore(symbol))
                            analysis_tasks.append(task)
                    
                    # 모든 분석 태스크가 완료될 때까지 대기 (최대 10초)
                    if analysis_tasks:
                        try:
                            # 그룹으로 대기
                            await asyncio.wait_for(asyncio.gather(*analysis_tasks), timeout=10.0)
                        except asyncio.TimeoutError:
                            logger.log_warning("종목 분석 전체 타임아웃 (10초)")
                        except Exception as e:
                            logger.log_error(e, "종목 분석 중 오류 발생")
                
                # 포지션 모니터링 - 개선된 버전 사용
                await self._monitor_positions()
                
                await asyncio.sleep(1)  # 1초 대기
                
            except Exception as e:
                logger.log_error(e, "Breakout strategy loop error")
                await asyncio.sleep(5)  # 에러 시 5초 대기
    
    async def _analyze_and_trade(self, symbol: str):
        """종목 분석 및 거래 - 성능 최적화 버전"""
        try:
            # 빠른 검증 (일시 중지 상태 확인)
            if self.paused or order_manager.is_trading_paused():
                return
                    
            # 필요한 데이터 확인 (빠른 실패)
            if (not self.price_data.get(symbol) or
                not self.breakout_levels.get(symbol) or
                not self.initialization_complete.get(symbol, False)):
                return
                
            # 현재가 및 돌파 레벨 확인
            current_price = self.price_data[symbol][-1]["price"]
            breakout_data = self.breakout_levels[symbol]
            
            if not breakout_data.get('high_level') or not breakout_data.get('low_level'):
                return
            
            # 이미 포지션 있는지 확인 (캐싱 단순화)
            now = datetime.now()
            cache_entry = self._symbol_positions_cache.get(symbol, {})
            
            # 캐시가 없거나 1초 이상 지났으면 업데이트
            if not cache_entry or 'last_update' not in cache_entry or (now - cache_entry.get('last_update', now - timedelta(seconds=2))).total_seconds() > 1:
                symbol_positions = self._get_symbol_positions(symbol)
                # 캐시 업데이트
                self._symbol_positions_cache[symbol] = {
                    'positions': symbol_positions,
                    'last_update': now
                }
            else:
                # 캐시 사용
                symbol_positions = cache_entry['positions']
                
            if len(symbol_positions) >= self.params["max_positions"]:
                return
            
            # 돌파 확인 및 포지션 진입 - 함수 호출 최소화
            high_level = breakout_data['high_level']
            low_level = breakout_data['low_level']
            
            if current_price > high_level:
                # 상방 돌파 (매수 신호)
                await self._enter_position(symbol, "BUY", current_price, breakout_data)
                
            elif current_price < low_level:
                # 하방 돌파 (매도 신호)
                await self._enter_position(symbol, "SELL", current_price, breakout_data)
                
        except Exception as e:
            logger.log_error(e, f"Breakout analysis error for {symbol}")
    
    def _get_symbol_positions(self, symbol: str) -> List[str]:
        """특정 종목의 포지션 ID 목록 반환"""
        return [
            position_id for position_id, position in self.positions.items()
            if position["symbol"] == symbol
        ]
    
    async def _enter_position(self, symbol: str, side: str, current_price: float, breakout_data: Dict[str, Any]):
        """포지션 진입"""
        try:
            # 주문 수량 계산
            position_size = self.params["position_size"]  # 100만원
            quantity = int(position_size / current_price)
            
            if quantity <= 0:
                return
            
            # 주문 실행
            result = await order_manager.place_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type="MARKET",
                strategy="breakout",
                reason=f"breakout_{side.lower()}"
            )
            
            if result["status"] == "success":
                # 돌파 레벨 기준으로 손절/익절 계산
                stop_loss_pct = self.params["stop_loss_pct"]
                take_profit_pct = self.params["take_profit_pct"]
                price_range = breakout_data['range']
                
                if side == "BUY":
                    stop_price = current_price - (price_range * stop_loss_pct)
                    target_price = current_price + (price_range * take_profit_pct)
                else:  # SELL
                    stop_price = current_price + (price_range * stop_loss_pct)
                    target_price = current_price - (price_range * take_profit_pct)
                
                # 포지션 저장
                position_id = result.get("order_id", str(datetime.now().timestamp()))
                self.positions[position_id] = {
                    "symbol": symbol,
                    "entry_price": current_price,
                    "entry_time": datetime.now(),
                    "side": side,
                    "quantity": quantity,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "breakout_level": breakout_data['high_level'] if side == "BUY" else breakout_data['low_level']
                }
                
                logger.log_system(
                    f"Breakout: Entered {side} position for {symbol} at {current_price}, "
                    f"stop: {stop_price}, target: {target_price}"
                )
                
        except Exception as e:
            logger.log_error(e, f"Breakout entry error for {symbol}")
    
    async def _exit_with_semaphore(self, pos_id: str, reason: str):
        """세마포어로 제한된 포지션 청산 함수 (중첩 함수 분리)"""
        async with self._exit_semaphore:
            try:
                # 청산 처리에 타임아웃 설정
                await asyncio.wait_for(
                    self._exit_position(pos_id, reason),
                    timeout=3.0
                )
            except asyncio.TimeoutError:
                logger.log_warning(f"포지션 {pos_id} 청산 타임아웃 (3초)")
            except Exception as e:
                logger.log_error(e, f"포지션 {pos_id} 청산 중 오류")
    
    async def _monitor_positions(self):
        """포지션 모니터링 - 성능 최적화 버전"""
        try:
            # 청산할 포지션 식별
            positions_to_exit = []
            
            for position_id, position in list(self.positions.items()):
                symbol = position["symbol"]
                
                # 현재 가격 확인
                if symbol not in self.price_data or not self.price_data[symbol]:
                    continue
                
                current_price = self.price_data[symbol][-1]["price"]
                side = position["side"]
                
                # 손절/익절 확인
                should_exit = False
                exit_reason = ""
                
                if side == "BUY":
                    # 매수 포지션
                    if current_price <= position["stop_price"]:
                        should_exit = True
                        exit_reason = "stop_loss"
                    elif current_price >= position["target_price"]:
                        should_exit = True
                        exit_reason = "take_profit"
                        
                else:  # SELL
                    # 매도 포지션
                    if current_price >= position["stop_price"]:
                        should_exit = True
                        exit_reason = "stop_loss"
                    elif current_price <= position["target_price"]:
                        should_exit = True
                        exit_reason = "take_profit"
                
                # 청산 대상 포지션 추가
                if should_exit:
                    positions_to_exit.append((position_id, exit_reason))
            
            # 청산 대상이 있으면 병렬 처리
            if positions_to_exit:
                # 청산 태스크 생성 및 실행 (중첩 함수 제거)
                exit_tasks = [
                    asyncio.create_task(self._exit_with_semaphore(pos_id, reason))
                    for pos_id, reason in positions_to_exit
                ]
                
                # 모든 청산 태스크 완료 대기 (최대 10초)
                try:
                    await asyncio.wait_for(asyncio.gather(*exit_tasks), timeout=10.0)
                except asyncio.TimeoutError:
                    logger.log_warning("포지션 청산 전체 타임아웃 (10초)")
                except Exception as e:
                    logger.log_error(e, "포지션 청산 중 오류 발생")
                    
        except Exception as e:
            logger.log_error(e, "Breakout position monitoring error")
    
    async def _exit_position(self, position_id: str, reason: str):
        """포지션 청산"""
        try:
            position = self.positions[position_id]
            symbol = position["symbol"]
            exit_side = "SELL" if position["side"] == "BUY" else "BUY"
            
            result = await order_manager.place_order(
                symbol=symbol,
                side=exit_side,
                quantity=position["quantity"],
                order_type="MARKET",
                strategy="breakout",
                reason=reason
            )
            
            if result["status"] == "success":
                # 포지션 제거
                del self.positions[position_id]
                
                logger.log_system(f"Breakout: Exited position for {symbol}, reason: {reason}")
                
        except Exception as e:
            logger.log_error(e, f"Breakout exit error for position {position_id}")
    
    def get_signal_strength(self, symbol: str) -> float:
        """신호 강도 측정 (0 ~ 10)"""
        try:
            if symbol not in self.price_data or not self.price_data[symbol]:
                return 0
                
            # 초기화 안 된 경우
            if not self.initialization_complete.get(symbol, False):
                return 0
                
            current_price = self.price_data[symbol][-1]["price"]
            breakout_data = self.breakout_levels[symbol]
            
            # 돌파 레벨 미설정 시
            if not breakout_data.get('high_level') or not breakout_data.get('low_level'):
                return 0
            
            # 상방 돌파 점수
            if current_price > breakout_data['high_level']:
                # 얼마나 많이 돌파했는지 계산 (최대 5%)
                breakout_pct = (current_price - breakout_data['high_level']) / breakout_data['high_level']
                return min(10, breakout_pct * 200)  # 최대 5% 돌파 시 10점
                
            # 하방 돌파 점수
            elif current_price < breakout_data['low_level']:
                # 얼마나 많이 돌파했는지 계산 (최대 5%)
                breakout_pct = (breakout_data['low_level'] - current_price) / breakout_data['low_level']
                return min(10, breakout_pct * 200)  # 최대 5% 돌파 시 10점
            
            # 돌파 근접도 점수 (돌파 레벨까지 남은 거리, 최대 1%)
            high_proximity = (breakout_data['high_level'] - current_price) / breakout_data['high_level']
            low_proximity = (current_price - breakout_data['low_level']) / breakout_data['low_level']
            
            if high_proximity < 0.01:  # 상방 돌파에 1% 내로 근접
                return min(5, (0.01 - high_proximity) * 500)  # 최대 5점
                
            if low_proximity < 0.01:   # 하방 돌파에 1% 내로 근접
                return min(5, (0.01 - low_proximity) * 500)  # 최대 5점
            
            return 0  # 신호 없음
            
        except Exception as e:
            logger.log_error(e, f"Error calculating breakout signal strength for {symbol}")
            return 0
    
    def get_signal_direction(self, symbol: str) -> str:
        """신호 방향 반환"""
        try:
            # 초기화가 완료되지 않았으면 중립 반환
            if not self.initialization_complete.get(symbol, False):
                return "NEUTRAL"
            
            # 현재가 확인
            current_price = 0
            if symbol in self.price_data and self.price_data[symbol]:
                current_price = self.price_data[symbol][-1]["price"]
            
            # 브레이크아웃 레벨 확인
            breakout_data = self.breakout_levels.get(symbol, {})
            if not breakout_data or 'high_level' not in breakout_data or 'low_level' not in breakout_data:
                return "NEUTRAL"
            
            high_level = breakout_data.get('high_level')
            low_level = breakout_data.get('low_level')
            
            # 방향 판단
            if current_price > high_level:  # 상향 돌파
                return "BUY"
            elif current_price < low_level:  # 하향 돌파
                return "SELL"
            else:
                return "NEUTRAL"
                
        except Exception as e:
            logger.log_error(e, f"{symbol} - 브레이크아웃 방향 판단 오류")
            return "NEUTRAL"
    
    async def get_signal(self, symbol: str) -> Dict[str, Any]:
        """전략 신호 반환 (combined_strategy에서 호출)"""
        try:
            # 초기화가 완료되지 않았으면 중립 반환
            if not self.initialization_complete.get(symbol, False):
                logger.log_system(f"[DEBUG] {symbol} - 브레이크아웃 초기화 미완료, 중립 신호 반환")
                return {"signal": 0, "direction": "NEUTRAL"}
            
            # 현재가 확인
            current_price = 0
            if symbol in self.price_data and self.price_data[symbol]:
                current_price = self.price_data[symbol][-1]["price"]
            
            # 현재가가 없으면 API에서 가져오기
            if current_price <= 0:
                try:
                    symbol_info = await api_client.get_symbol_info(symbol)
                    if symbol_info and "current_price" in symbol_info:
                        current_price = float(symbol_info["current_price"])
                except Exception as e:
                    logger.log_error(e, f"{symbol} - 브레이크아웃 현재가 조회 실패")
                    return {"signal": 0, "direction": "NEUTRAL"}
            
            # 브레이크아웃 레벨 확인
            breakout_data = self.breakout_levels.get(symbol, {})
            if not breakout_data or 'high_level' not in breakout_data or 'low_level' not in breakout_data:
                logger.log_system(f"[DEBUG] {symbol} - 브레이크아웃 레벨 미설정, 중립 신호 반환")
                return {"signal": 0, "direction": "NEUTRAL"}
            
            high_level = breakout_data.get('high_level')
            low_level = breakout_data.get('low_level')
            
            # 방향과 신호 강도 계산
            direction = "NEUTRAL"
            signal_strength = 0
            
            if current_price > high_level:  # 상향 돌파
                direction = "BUY"
                # 돌파 정도에 따른 신호 강도 계산 (최대 10)
                price_range = breakout_data.get('range', 0)
                if price_range > 0:
                    excess = current_price - high_level
                    signal_strength = min(10, (excess / price_range) * 10)
                else:
                    signal_strength = 5  # 기본값
            
            elif current_price < low_level:  # 하향 돌파
                direction = "SELL"
                # 돌파 정도에 따른 신호 강도 계산 (최대 10)
                price_range = breakout_data.get('range', 0)
                if price_range > 0:
                    excess = low_level - current_price
                    signal_strength = min(10, (excess / price_range) * 10)
                else:
                    signal_strength = 5  # 기본값
            
            logger.log_system(f"[DEBUG] {symbol} - 브레이크아웃 신호: 방향={direction}, 강도={signal_strength}, 현재가={current_price}, 상향레벨={high_level}, 하향레벨={low_level}")
            return {"signal": signal_strength, "direction": direction}
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 브레이크아웃 신호 계산 오류")
            return {"signal": 0, "direction": "NEUTRAL"}
    
    async def update_symbols(self, new_symbols: List[str]):
        """종목 목록 업데이트"""
        try:
            # 새로운 종목 집합
            new_set = set(new_symbols)
            
            # 현재 감시 중인 종목 집합
            current_set = set(self.watched_symbols)
            
            # 제거할 종목들
            to_remove = current_set - new_set
            
            # 추가할 종목들
            to_add = new_set - current_set
            
            # 종목 데이터 정리
            for symbol in to_remove:
                if symbol in self.price_data:
                    del self.price_data[symbol]
                if symbol in self.breakout_levels:
                    del self.breakout_levels[symbol]
                if symbol in self._symbol_positions_cache:
                    del self._symbol_positions_cache[symbol]
            
            # 새 종목 초기화
            for symbol in to_add:
                self.price_data[symbol] = deque(maxlen=300)
                self.breakout_levels[symbol] = {
                    'high_level': None, 
                    'low_level': None, 
                    'range': None,
                    'init_high': None,
                    'init_low': None
                }
                self.initialization_complete[symbol] = False
            
            # 감시 종목 업데이트
            self.watched_symbols = list(new_set)
            
            logger.log_system(f"브레이크아웃 전략: 감시 종목 {len(self.watched_symbols)}개로 업데이트됨")
            
            # 초기 데이터 로드 시작 (추가된 종목만)
            if to_add:
                asyncio.create_task(self._load_initial_data_for_all_symbols(list(to_add)))
        
        except Exception as e:
            logger.log_error(e, "브레이크아웃 전략 종목 업데이트 오류")

    async def _load_initial_data_for_all_symbols(self, symbols: List[str], batch_size: int = 5):
        """여러 종목의 초기 데이터를 효율적으로 로드하는 헬퍼 함수"""
        logger.log_system(f"브레이크아웃 전략: {len(symbols)}개 종목 초기 데이터 로드 시작")
        
        # 세마포어로 동시 API 호출 수 제한
        semaphore = asyncio.Semaphore(batch_size)
        
        async def _load_with_semaphore(symbol):
            async with semaphore:
                try:
                    # API 호출을 타임아웃과 함께 처리
                    price_data_task = asyncio.create_task(api_client.get_minute_price(symbol))
                    price_data = await asyncio.wait_for(price_data_task, timeout=3.0)
                    
                    if price_data.get("rt_cd") == "0":
                        chart_data = price_data.get("output2", [])
                        if not chart_data:
                            logger.log_system(f"{symbol} - 차트 데이터 없음")
                            return False
                        
                        # 가격 데이터 추출 및 필터링
                        prices = []
                        for item in chart_data:
                            price_value = None
                            if "stck_prpr" in item:
                                price_value = float(item["stck_prpr"])
                            elif "clos" in item:
                                price_value = float(item["clos"])
                            
                            if price_value is not None:
                                prices.append(price_value)
                        
                        if not prices:
                            logger.log_system(f"{symbol} - 가격 데이터 추출 실패")
                            return False
                        
                        # 데이터 저장
                        self.breakout_levels[symbol]['init_high'] = max(prices[:min(30, len(prices))])
                        self.breakout_levels[symbol]['init_low'] = min(prices[:min(30, len(prices))])
                        
                        logger.log_system(f"{symbol} - 초기 데이터 로드 성공: 고가={self.breakout_levels[symbol]['init_high']}, 저가={self.breakout_levels[symbol]['init_low']}")
                        return True
                    else:
                        logger.log_warning(f"{symbol} - API 응답 오류: {price_data.get('msg1', '알 수 없는 오류')}")
                        return False
                        
                except asyncio.TimeoutError:
                    logger.log_warning(f"{symbol} - 초기 데이터 로드 타임아웃")
                    return False
                except Exception as e:
                    logger.log_error(e, f"{symbol} - 초기 데이터 로드 실패")
                    return False
        
        # 병렬 처리 실행
        tasks = [_load_with_semaphore(symbol) for symbol in symbols]
        results = await asyncio.gather(*tasks)
        
        # 성공/실패 카운트
        success_count = sum(1 for result in results if result)
        
        logger.log_system(f"브레이크아웃 전략: 초기 데이터 로드 완료 ({success_count}/{len(symbols)}개 성공)")
        return success_count

# 싱글톤 인스턴스
breakout_strategy = BreakoutStrategy()