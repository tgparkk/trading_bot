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
        """실시간 체결가 업데이트 처리"""
        try:
            symbol = data.get("tr_key")
            price = float(data.get("stck_prpr", 0))
            
            if symbol in self.price_data:
                self.price_data[symbol].append({
                    "price": price,
                    "timestamp": datetime.now()
                })
                
                # 장 시작 시간에 초기 데이터 수집
                current_time = datetime.now().time()
                if time(9, 0) <= current_time <= time(9, 30) and not self.initialization_complete[symbol]:
                    # 초기화 중 최고가/최저가 업데이트
                    breakout_data = self.breakout_levels[symbol]
                    if breakout_data['init_high'] is None or price > breakout_data['init_high']:
                        breakout_data['init_high'] = price
                    if breakout_data['init_low'] is None or price < breakout_data['init_low']:
                        breakout_data['init_low'] = price
                
                # 9:30에 돌파 레벨 설정
                if current_time >= time(9, 30) and not self.initialization_complete[symbol]:
                    await self._set_breakout_levels(symbol)
                    
        except Exception as e:
            logger.log_error(e, "Error handling price update in breakout strategy")
    
    async def _set_breakout_levels(self, symbol: str):
        """돌파 레벨 설정 (9:30에 실행)"""
        try:
            if self.initialization_complete[symbol]:
                return
                
            breakout_data = self.breakout_levels[symbol]
            
            # 9:00~9:30 데이터에서 고가/저가 계산
            if breakout_data['init_high'] is None or breakout_data['init_low'] is None:
                # 실시간 데이터 부족한 경우 API로 조회
                data = api_client.get_minute_price(
                    symbol=symbol,
                    time_unit="1"
                )
                
                if not data or data.get("rt_cd") != "0":
                    logger.log_warning(f"{symbol} - 브레이크아웃 전략 초기 데이터 부족")
                    return
                
                # output2가 실제 차트 데이터를 담고 있음
                chart_data = data.get("output2", [])
                if not chart_data or len(chart_data) < 10:  # 최소 10개 데이터 필요
                    logger.log_warning(f"{symbol} - 브레이크아웃 전략 초기 데이터 부족")
                    return
                
                # 데이터 저장
                for item in chart_data:
                    # 시간 정보 파싱
                    time_str = item.get("bass_tm", "")
                    if time_str and len(time_str) >= 6:
                        try:
                            hour = int(time_str[:2])
                            minute = int(time_str[2:4])
                            second = int(time_str[4:6])
                            timestamp = datetime.now().replace(hour=hour, minute=minute, second=second)
                        except ValueError:
                            timestamp = datetime.now()
                    else:
                        timestamp = datetime.now()

                    self.price_data[symbol].append({
                        "timestamp": timestamp,
                        "price": float(item["stck_prpr"]),
                        "volume": int(item.get("cntg_vol", 0))
                    })
                
                # 돌파 레벨 계산
                if len(self.price_data[symbol]) >= 10:
                    prices = [item["price"] for item in self.price_data[symbol]]
                    high_price = max(prices)
                    low_price = min(prices)
                    price_range = high_price - low_price
                    
                    k_value = self.params["k_value"]
                    self.breakout_levels[symbol] = {
                        'high_level': high_price + (price_range * k_value),
                        'low_level': low_price - (price_range * k_value),
                        'range': price_range
                    }
                    
                    self.initialization_complete[symbol] = True
                    logger.log_system(f"{symbol} - 브레이크아웃 전략 초기화 완료")
                    logger.log_system(f"돌파 레벨: 상단={self.breakout_levels[symbol]['high_level']:,.0f}, "
                                    f"하단={self.breakout_levels[symbol]['low_level']:,.0f}")
                else:
                    logger.log_system(f"{symbol} - 가격 데이터를 추출할 수 없음")
            else:
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
    
    async def _strategy_loop(self):
        """전략 실행 루프"""
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
                    for symbol in self.watched_symbols:
                        # 초기화 완료된 종목만 분석
                        if self.initialization_complete.get(symbol, False):
                            await self._analyze_and_trade(symbol)
                
                # 포지션 모니터링
                await self._monitor_positions()
                
                await asyncio.sleep(1)  # 1초 대기
                
            except Exception as e:
                logger.log_error(e, "Breakout strategy loop error")
                await asyncio.sleep(5)  # 에러 시 5초 대기
    
    async def _analyze_and_trade(self, symbol: str):
        """종목 분석 및 거래"""
        try:
            # 전략이 일시 중지 상태인지 확인
            if self.paused or order_manager.is_trading_paused():
                return
                
            # 충분한 데이터 있는지 확인
            if not self.price_data[symbol]:
                return
                
            # 현재가
            current_price = self.price_data[symbol][-1]["price"]
            
            # 돌파 레벨
            breakout_data = self.breakout_levels[symbol]
            if not breakout_data.get('high_level') or not breakout_data.get('low_level'):
                return
            
            # 이미 포지션 있는지 확인
            symbol_positions = self._get_symbol_positions(symbol)
            if len(symbol_positions) >= self.params["max_positions"]:
                return
            
            # 돌파 확인
            if current_price > breakout_data['high_level']:
                # 상방 돌파 (매수 신호)
                await self._enter_position(symbol, "BUY", current_price, breakout_data)
                
            elif current_price < breakout_data['low_level']:
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
    
    async def _monitor_positions(self):
        """포지션 모니터링"""
        try:
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
                
                # 청산 실행
                if should_exit:
                    await self._exit_position(position_id, exit_reason)
                    
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
    
    async def _load_initial_data(self, symbol: str):
        """초기 데이터 로딩"""
        try:
            # 기존 데이터 초기화
            self.price_data[symbol] = deque(maxlen=2000)
            self.breakout_levels[symbol] = {
                'high_level': None,
                'low_level': None,
                'range': None
            }
            self.initialization_complete[symbol] = False
            
            # 초기 데이터 요청
            end_time = datetime.now()
            start_time = end_time - timedelta(minutes=30)  # 30분 데이터
            
            # API로 데이터 요청
            data = api_client.get_minute_price(
                symbol=symbol,
                time_unit="1"
            )
            
            if not data or data.get("rt_cd") != "0":
                logger.log_warning(f"{symbol} - 브레이크아웃 전략 초기 데이터 부족")
                return
            
            # output2가 실제 차트 데이터를 담고 있음
            chart_data = data.get("output2", [])
            if not chart_data or len(chart_data) < 10:  # 최소 10개 데이터 필요
                logger.log_warning(f"{symbol} - 브레이크아웃 전략 초기 데이터 부족")
                return
            
            # 데이터 저장
            for item in chart_data:
                # 시간 정보 파싱
                time_str = item.get("bass_tm", "")
                if time_str and len(time_str) >= 6:
                    try:
                        hour = int(time_str[:2])
                        minute = int(time_str[2:4])
                        second = int(time_str[4:6])
                        timestamp = datetime.now().replace(hour=hour, minute=minute, second=second)
                    except ValueError:
                        timestamp = datetime.now()
                else:
                    timestamp = datetime.now()

                self.price_data[symbol].append({
                    "timestamp": timestamp,
                    "price": float(item["stck_prpr"]),
                    "volume": int(item.get("cntg_vol", 0))
                })
            
            # 돌파 레벨 계산
            if len(self.price_data[symbol]) >= 10:
                prices = [item["price"] for item in self.price_data[symbol]]
                high_price = max(prices)
                low_price = min(prices)
                price_range = high_price - low_price
                
                k_value = self.params["k_value"]
                self.breakout_levels[symbol] = {
                    'high_level': high_price + (price_range * k_value),
                    'low_level': low_price - (price_range * k_value),
                    'range': price_range
                }
                
                self.initialization_complete[symbol] = True
                logger.log_system(f"{symbol} - 브레이크아웃 전략 초기화 완료")
                logger.log_system(f"돌파 레벨: 상단={self.breakout_levels[symbol]['high_level']:,.0f}, "
                                f"하단={self.breakout_levels[symbol]['low_level']:,.0f}")
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 브레이크아웃 전략 초기 데이터 로딩 오류")
    
    def get_signal_strength(self, symbol: str) -> float:
        """신호 강도 반환"""
        try:
            # 초기화가 완료되지 않았으면 0 반환
            if not self.initialization_complete.get(symbol, False):
                return 0
            
            # 현재가 확인
            current_price = 0
            if symbol in self.price_data and self.price_data[symbol]:
                current_price = self.price_data[symbol][-1]["price"]
            
            # 브레이크아웃 레벨 확인
            if symbol not in self.breakout_levels:
                return 0
            
            breakout_data = self.breakout_levels[symbol]
            
            # 돌파 레벨 미설정 시 재계산 시도
            if not breakout_data.get('high_level') or not breakout_data.get('low_level'):
                if len(self.price_data[symbol]) >= 10:
                    prices = [item["price"] for item in self.price_data[symbol]]
                    high_price = max(prices)
                    low_price = min(prices)
                    price_range = high_price - low_price
                    
                    k_value = self.params["k_value"]
                    self.breakout_levels[symbol] = {
                        'high_level': high_price + (price_range * k_value),
                        'low_level': low_price - (price_range * k_value),
                        'range': price_range
                    }
                    self.initialization_complete[symbol] = True
                else:
                    return 0
            
            # 상방 돌파 점수
            if current_price > breakout_data['high_level']:
                # 얼마나 많이 돌파했는지 계산 (최대 5%)
                high_level = breakout_data['high_level']
                if high_level <= 0:  # 안전 체크
                    high_level = 0.0001
                breakout_pct = (current_price - high_level) / high_level
                return min(10, breakout_pct * 200)  # 최대 5% 돌파 시 10점
                
            # 하방 돌파 점수
            elif current_price < breakout_data['low_level']:
                # 얼마나 많이 돌파했는지 계산 (최대 5%)
                low_level = breakout_data['low_level']
                if low_level <= 0:  # 안전 체크
                    low_level = 0.0001
                breakout_pct = (low_level - current_price) / low_level
                return min(10, breakout_pct * 200)  # 최대 5% 돌파 시 10점
            
            # 돌파 근접도 점수 (돌파 레벨까지 남은 거리, 최대 1%)
            high_level = breakout_data['high_level']
            low_level = breakout_data['low_level']
            
            # 나누기 연산 전 안전 체크
            if high_level <= 0:
                high_level = 0.0001
            if low_level <= 0:
                low_level = 0.0001
                
            high_proximity = (high_level - current_price) / high_level
            low_proximity = (current_price - low_level) / low_level
            
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
                    price_data = api_client.get_current_price(symbol)
                    if price_data and price_data.get("rt_cd") == "0" and "output" in price_data:
                        current_price = float(price_data["output"]["stck_prpr"])
                    else:
                        logger.log_system(f"[DEBUG] {symbol} - 브레이크아웃 현재가 조회 실패, 중립 신호 반환")
                        return {"signal": 0, "direction": "NEUTRAL"}
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
            
            # 가격 범위 확인
            price_range = breakout_data.get('range', 0)
            if price_range <= 0:
                # range가 없을 경우 high_level과 low_level 사이의 거리로 계산
                price_range = high_level - low_level
                if price_range <= 0:  # 여전히 0 이하면 기본값 설정
                    # 현재가의 일정 비율을 가격 범위로 사용
                    price_range = max(current_price * 0.02, 0.0001)  # 현재가의 2% 또는 최소값
                    logger.log_system(f"[DEBUG] {symbol} - 브레이크아웃 가격 범위가 0 이하여서 기본값({price_range:.4f})으로 설정")
            
            # 안전장치: 가격 범위가 매우 작은 경우 최소값 설정
            if price_range < 0.0001:
                price_range = 0.0001  # 최소 가격 범위 설정
            
            if current_price > high_level:  # 상향 돌파
                direction = "BUY"
                # 돌파 정도에 따른 신호 강도 계산 (최대 10)
                excess = current_price - high_level
                # 제로 나누기 방지
                if price_range <= 0.0001:
                    price_range = 0.0001  # 매우 작은 값으로 설정하여 나누기 오류 방지
                signal_strength = min(10, (excess / price_range) * 20)  # 배수 증가
            
            elif current_price < low_level:  # 하향 돌파
                direction = "SELL"
                # 돌파 정도에 따른 신호 강도 계산 (최대 10)
                excess = low_level - current_price
                # 제로 나누기 방지
                if price_range <= 0.0001:
                    price_range = 0.0001  # 매우 작은 값으로 설정하여 나누기 오류 방지
                signal_strength = min(10, (excess / price_range) * 20)  # 배수 증가
            
            # 돌파 근접도 추가 (돌파 레벨에 가까울수록 점수 추가)
            elif high_level - current_price < price_range * 0.02:  # 상방 돌파에 근접 (2% 이내)
                direction = "BUY"
                # 제로 나누기 방지
                if price_range <= 0.0001:
                    price_range = 0.0001
                proximity = (high_level - current_price) / price_range
                signal_strength = max(1, 5 * (1 - (proximity / 0.02)))  # 최소 1점, 최대 5점
            
            elif current_price - low_level < price_range * 0.02:  # 하방 돌파에 근접 (2% 이내)
                direction = "SELL"
                # 제로 나누기 방지
                if price_range <= 0.0001:
                    price_range = 0.0001
                proximity = (current_price - low_level) / price_range
                signal_strength = max(1, 5 * (1 - (proximity / 0.02)))  # 최소 1점, 최대 5점
            
            #logger.log_system(f"[DEBUG] {symbol} - 브레이크아웃 신호: 방향={direction}, 강도={signal_strength:.2f}, 현재가={current_price}, 상향레벨={high_level}, 하향레벨={low_level}, 가격범위={price_range}")
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
            
            # 감시 종목 업데이트
            self.watched_symbols = list(new_set)
            
            logger.log_system(f"브레이크아웃 전략: 감시 종목 {len(self.watched_symbols)}개로 업데이트됨")
        
        except Exception as e:
            logger.log_error(e, "브레이크아웃 전략 종목 업데이트 오류")

# 싱글톤 인스턴스
breakout_strategy = BreakoutStrategy() 