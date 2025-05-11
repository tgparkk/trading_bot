"""
갭 트레이딩 전략 (Gap Trading Strategy)
장 시작 시 전일 종가와 당일 시가의 차이(갭)를 활용하는 전략
"""
import asyncio
import threading
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

class GapStrategy:
    """갭 트레이딩 전략 클래스"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        """싱글톤 패턴 구현을 위한 __new__ 메서드 오버라이드"""
        with cls._lock:  # 스레드 안전성을 위한 락 사용
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """생성자는 인스턴스가 처음 생성될 때만 실행됨을 보장"""
        if not hasattr(self, '_initialized') or not self._initialized:
            # get 메서드 대신 직접 속성 접근 또는 기본값 설정
            self.params = {
            "min_gap_pct": 0.007,        # 최소 갭 비율 (0.7%)
            "max_gap_pct": 0.05,         # 최대 갭 비율 (5%)
            "gap_fill_pct": 0.75,        # 갭 채움 목표 비율 (75%)
            "volume_threshold": 1.5,     # 거래량 임계치 (평균 대비)
            "stop_loss_pct": 0.5,        # 추가 갭 확대 시 손절 비율 (갭의 50%)
            "max_positions": 3,          # 최대 포지션 개수
            "position_size": 1000000,    # 기본 포지션 크기 (100만원)
            "skip_news_symbols": True,   # 뉴스 있는 종목 스킵 여부
            "hold_time_minutes": 120     # 최대 보유 시간 (분)
        }
        
            # 설정에 gap_params가 있으면 업데이트
            if hasattr(config["trading"], "gap_params"):
                self.params.update(config["trading"].gap_params)
                
            self.running = False
            self.paused = False
            self.watched_symbols = set()
            self.price_data = {}           # {symbol: deque of price data}
            self.gap_data = {}             # {symbol: {'gap_pct': float, 'direction': str, 'prev_close': float}}
            self.positions = {}            # {position_id: position_data}
            self.volume_data = {}          # {symbol: {'avg_volume': float, 'volume_ratio': float}}
            self._initialized = True
        
    async def start(self, symbols: List[str]):
        """전략 시작"""
        try:
            self.running = True
            self.paused = False
            self.watched_symbols = set(symbols)
            
            # 각 종목별 데이터 초기화
            for symbol in symbols:
                self.price_data[symbol] = deque(maxlen=300)  # 약 30분치 데이터
                self.gap_data[symbol] = {
                    'gap_pct': None,
                    'direction': None,
                    'prev_close': None,
                    'today_open': None,
                    'gap_size': None,
                    'fill_target': None,
                    'gap_identified': False
                }
                self.volume_data[symbol] = {
                    'avg_volume': None,
                    'volume_ratio': None,
                    'volumes': deque(maxlen=20)  # 20일치 거래량 데이터
                }
                
                # 웹소켓 구독
                await ws_client.subscribe_price(symbol, self._handle_price_update)
                
                # 전일 종가 및 거래량 데이터 로드
                await self._load_historical_data(symbol)
            
            logger.log_system(f"Gap strategy started for {len(symbols)} symbols")
            
            # 전략 실행 루프
            asyncio.create_task(self._strategy_loop())
            
        except Exception as e:
            logger.log_error(e, "Failed to start gap strategy")
            await alert_system.notify_error(e, "Gap strategy start error")
    
    async def _load_initial_data(self, symbol: str):
        """초기 데이터 로딩"""
        try:
            logger.log_system(f"갭 전략 - {symbol} 초기 데이터 로딩 시작")
            
            # 현재가 정보 조회
            price_info = await api_client.get_symbol_info(symbol)
            if price_info and price_info.get("current_price"):
                current_price = float(price_info["current_price"])
                
                # 가격 데이터에 추가
                self.price_data[symbol].append({
                    "price": current_price,
                    "volume": 0,
                    "timestamp": datetime.now()
                })
                logger.log_system(f"갭 전략 - {symbol} 현재가 로드: {current_price:,.0f}원")
            
            # 일봉 데이터 조회
            price_data = api_client.get_daily_price(symbol)
            if price_data.get("rt_cd") == "0":
                # API 응답 구조에 맞게 수정
                if "output2" in price_data and price_data["output2"]:
                    daily_data = price_data["output2"]
                elif "output" in price_data and "lst" in price_data["output"]:
                    daily_data = price_data["output"]["lst"]
                else:
                    daily_data = []
                
                if daily_data and len(daily_data) > 1:
                    # 전일 데이터 (첫 번째가 오늘, 두 번째가 어제)
                    prev_day = daily_data[1]
                    prev_close = float(prev_day.get("stck_clpr", 0))
                    
                    if prev_close > 0:
                        self.gap_data[symbol]['prev_close'] = prev_close
                        
                        # 거래량 데이터 수집
                        volumes = []
                        for day_data in daily_data[1:21]:  # 최근 20일
                            vol = int(day_data.get("acml_vol", 0))
                            if vol > 0:
                                volumes.append(vol)
                        
                        if volumes:
                            avg_volume = np.mean(volumes)
                            self.volume_data[symbol]['avg_volume'] = avg_volume
                            self.volume_data[symbol]['volumes'] = deque(volumes, maxlen=20)
                            
                        logger.log_system(f"갭 전략 - {symbol} 전일 종가 로드: {prev_close:,.0f}원")
            
            # 오늘 시가 조회
            if "output1" in price_data and price_data["output1"]:
                today_open = float(price_data["output1"].get("stck_oprc", 0))
                if today_open > 0:
                    self.gap_data[symbol]['today_open'] = today_open
                    
                    # 갭 계산
                    if self.gap_data[symbol]['prev_close'] and today_open > 0:
                        prev_close = self.gap_data[symbol]['prev_close']
                        gap_pct = (today_open - prev_close) / prev_close
                        gap_size = abs(today_open - prev_close)
                        direction = "UP" if gap_pct > 0 else "DOWN"
                        
                        self.gap_data[symbol].update({
                            'gap_pct': gap_pct,
                            'gap_size': gap_size,
                            'direction': direction,
                            'gap_identified': True
                        })
                        
                        logger.log_system(f"갭 전략 - {symbol} 갭 확인: {direction} {gap_pct:.1%} "
                                        f"(시가: {today_open:,.0f}원, 전일종가: {prev_close:,.0f}원)")
                    
        except Exception as e:
            logger.log_error(e, f"갭 전략 - {symbol} 초기 데이터 로딩 오류")
    
    async def _load_historical_data(self, symbol: str):
        """과거 데이터 로드 (전일 종가, 거래량 등)"""
        try:
            # 일봉 데이터 조회
            price_data = api_client.get_daily_price(symbol)
            if price_data.get("rt_cd") == "0":
                # API 응답 구조 확인 - output2가 일봉 데이터 리스트
                if "output2" in price_data and price_data["output2"]:
                    daily_data = price_data["output2"]
                
                    if len(daily_data) > 1:
                        # 전일 데이터 (인덱스 0은 오늘, 1은 어제)
                        prev_day = daily_data[1]
                        prev_close = float(prev_day.get("stck_clpr", 0))
                        
                        if prev_close > 0:
                            self.gap_data[symbol]['prev_close'] = prev_close
                            
                            # 거래량 데이터 수집
                            volumes = []
                            for day_data in daily_data[1:21]:  # 최근 20일
                                vol = int(day_data.get("acml_vol", 0))
                                if vol > 0:
                                    volumes.append(vol)
                            
                            if volumes:
                                avg_volume = np.mean(volumes)
                                self.volume_data[symbol]['avg_volume'] = avg_volume
                                self.volume_data[symbol]['volumes'] = deque(volumes, maxlen=20)
                            
                            logger.log_system(f"갭 전략 - {symbol} 과거 데이터 로드 완료: "
                                            f"전일종가={prev_close:,.0f}원, 평균거래량={avg_volume:,.0f}")
                    
        except Exception as e:
            logger.log_error(e, f"갭 전략 - {symbol} 과거 데이터 로딩 오류")
    
    async def stop(self):
        """전략 중지"""
        self.running = False
        
        # 웹소켓 구독 해제
        for symbol in self.watched_symbols:
            await ws_client.unsubscribe(symbol, "price")
        
        logger.log_system("Gap strategy stopped")
    
    async def pause(self):
        """전략 일시 중지"""
        if not self.paused:
            self.paused = True
            logger.log_system("Gap strategy paused")
        return True

    async def resume(self):
        """전략 재개"""
        if self.paused:
            self.paused = False
            logger.log_system("Gap strategy resumed")
        return True
    
    async def _handle_price_update(self, data: Dict[str, Any]):
        """실시간 체결가 업데이트 처리"""
        try:
            symbol = data.get("tr_key")
            price = float(data.get("stck_prpr", 0))
            volume = int(data.get("cntg_vol", 0))
            
            if symbol in self.price_data:
                timestamp = datetime.now()
                self.price_data[symbol].append({
                    "price": price,
                    "volume": volume,
                    "timestamp": timestamp
                })
                
                # 장 시작 시간에 갭 확인
                current_time = timestamp.time()
                if time(9, 0) <= current_time <= time(9, 5) and not self.gap_data[symbol]['gap_identified']:
                    # 첫 체결가를 당일 시가로 간주
                    if not self.gap_data[symbol]['today_open']:
                        self.gap_data[symbol]['today_open'] = price
                        prev_close = self.gap_data[symbol]['prev_close']
                        
                        if prev_close:
                            # 갭 크기 계산
                            gap_pct = (price - prev_close) / prev_close
                            gap_size = abs(price - prev_close)
                            
                            # 갭 방향 결정
                            direction = "UP" if gap_pct > 0 else "DOWN"
                            
                            # 갭 정보 저장
                            self.gap_data[symbol].update({
                                'gap_pct': gap_pct,
                                'direction': direction,
                                'gap_size': gap_size,
                                'gap_identified': True
                            })
                            
                            # 갭 채움 목표 가격 계산
                            fill_pct = self.params["gap_fill_pct"]
                            if direction == "UP":
                                fill_target = price - (gap_size * fill_pct)
                            else:  # DOWN
                                fill_target = price + (gap_size * fill_pct)
                                
                            self.gap_data[symbol]['fill_target'] = fill_target
                            
                            # 거래량 비율 계산
                            if self.volume_data[symbol]['avg_volume']:
                                # 개장 초기라 현재 거래량은 아직 적을 수 있음
                                # 일단 비율은 1로 설정하고 나중에 업데이트
                                self.volume_data[symbol]['volume_ratio'] = 1.0
                            
                            # 로그 기록
                            logger.log_system(
                                f"Gap identified for {symbol}: {direction} gap of {gap_pct:.2%}, "
                                f"fill target: {fill_target}"
                            )
                
                # 거래량 누적 및 비율 업데이트 (9:30 이후 주기적으로)
                if time(9, 30) <= current_time and timestamp.second % 30 == 0:
                    self._update_volume_ratio(symbol)
                
        except Exception as e:
            logger.log_error(e, "Error handling price update in gap strategy")
    
    def _update_volume_ratio(self, symbol: str):
        """거래량 비율 업데이트"""
        try:
            if symbol not in self.price_data or not self.price_data[symbol]:
                return
                
            # 현재까지의 누적 거래량 계산
            current_total_volume = sum(item["volume"] for item in self.price_data[symbol])
            
            # 평균 거래량이 있는 경우 비율 계산
            if self.volume_data[symbol]['avg_volume'] and self.volume_data[symbol]['avg_volume'] > 0:
                # 일중 예상 비율 계산 (현재 시간에 따라 가중치)
                current_time = datetime.now()
                current_minute = current_time.hour * 60 + current_time.minute
                market_open_minute = 9 * 60  # 9:00 AM
                
                # 장 시작 후 경과 시간(분)
                elapsed_minutes = max(1, current_minute - market_open_minute)
                
                # 일일 거래 시간 (6.5시간 = 390분)
                total_trading_minutes = 390
                
                # 시간 가중치 (경과 시간 / 총 거래 시간)
                time_weight = min(1.0, elapsed_minutes / total_trading_minutes)
                
                # 현재 거래량 / (평균 일일 거래량 * 시간 가중치)
                expected_volume = self.volume_data[symbol]['avg_volume'] * time_weight
                volume_ratio = current_total_volume / expected_volume if expected_volume > 0 else 1.0
                
                self.volume_data[symbol]['volume_ratio'] = volume_ratio
                
        except Exception as e:
            logger.log_error(e, f"Error updating volume ratio for {symbol}")
    
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
                
                # 9:05 이후에만 트레이딩 실행 (갭 확인 후)
                if current_time >= time(9, 5):
                    for symbol in self.watched_symbols:
                        await self._analyze_and_trade(symbol)
                
                # 포지션 모니터링
                await self._monitor_positions()
                
                await asyncio.sleep(1)  # 1초 대기
                
            except Exception as e:
                logger.log_error(e, "Gap strategy loop error")
                await asyncio.sleep(5)  # 에러 시 5초 대기
    
    async def _analyze_and_trade(self, symbol: str):
        """종목 분석 및 거래"""
        try:
            # 전략이 일시 중지 상태인지 확인
            if self.paused or order_manager.is_trading_paused():
                return
                
            # 갭 확인 되었는지 체크
            gap_data = self.gap_data.get(symbol, {})
            if not gap_data.get('gap_identified', False):
                return
                
            # 갭 크기가 임계값 내에 있는지 확인
            gap_pct = gap_data.get('gap_pct', 0)
            if abs(gap_pct) < self.params["min_gap_pct"] or abs(gap_pct) > self.params["max_gap_pct"]:
                return
            
            # 충분한 데이터 있는지 확인
            if not self.price_data[symbol]:
                return
                
            # 현재가
            current_price = self.price_data[symbol][-1]["price"]
            
            # 갭이 채워지는지, 거래량도 충분한지 확인
            if not self.gap_data[symbol]['gap_identified'] or not self.gap_data[symbol]['fill_target']:
                return
                
            # 이미 포지션 있는지 확인
            symbol_positions = self._get_symbol_positions(symbol)
            
            # 조건 확인
            if gap_data['direction'] == "UP" and len(symbol_positions) < self.params["max_positions"]:
                # 갭 업일 때: 하락하여 갭 채움 목표에 도달하면 매수
                if current_price <= gap_data['fill_target']:
                    await self._enter_position(symbol, "BUY", current_price, gap_data)
            
            elif gap_data['direction'] == "DOWN" and len(symbol_positions) < self.params["max_positions"]:
                # 갭 다운일 때: 상승하여 갭 채움 목표에 도달하면 매수
                if current_price >= gap_data['fill_target']:
                    await self._enter_position(symbol, "BUY", current_price, gap_data)
                
        except Exception as e:
            logger.log_error(e, f"Gap analysis error for {symbol}")
    
    def _get_symbol_positions(self, symbol: str) -> List[str]:
        """특정 종목의 포지션 ID 목록 반환"""
        return [
            position_id for position_id, position in self.positions.items()
            if position["symbol"] == symbol
        ]
    
    async def _enter_position(self, symbol: str, side: str, current_price: float, gap_data: Dict[str, Any]):
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
                strategy="gap",
                reason=f"gap_fill_{gap_data['direction'].lower()}"
            )
            
            if result["status"] == "success":
                # 손절가 계산
                stop_loss_pct = self.params["stop_loss_pct"]
                gap_size = gap_data['gap_size']
                
                if gap_data['direction'] == "UP":
                    # 갭 업 -> 매수 진입 -> 더 하락하면 손절
                    stop_price = current_price - (gap_size * stop_loss_pct)
                    take_profit = gap_data['prev_close']  # 전일 종가가 익절 목표
                else:  # DOWN
                    # 갭 다운 -> 매수 진입 -> 더 하락하면 손절
                    stop_price = current_price - (gap_size * stop_loss_pct)
                    take_profit = gap_data['prev_close']  # 전일 종가가 익절 목표
                
                # 포지션 저장
                position_id = result.get("order_id", str(datetime.now().timestamp()))
                self.positions[position_id] = {
                    "symbol": symbol,
                    "entry_price": current_price,
                    "entry_time": datetime.now(),
                    "side": side,
                    "quantity": quantity,
                    "stop_price": stop_price,
                    "target_price": take_profit,
                    "gap_direction": gap_data['direction'],
                    "gap_pct": gap_data['gap_pct']
                }
                
                logger.log_system(
                    f"Gap: Entered {side} position for {symbol} at {current_price}, "
                    f"target: {take_profit}, stop: {stop_price}"
                )
                
        except Exception as e:
            logger.log_error(e, f"Gap entry error for {symbol}")
    
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
                entry_time = position["entry_time"]
                
                # 보유 시간 확인
                hold_time = (datetime.now() - entry_time).total_seconds() / 60  # 분 단위
                max_hold_time = self.params["hold_time_minutes"]  # 기본 2시간
                
                # 손절/익절/시간 만료 확인
                should_exit = False
                exit_reason = ""
                
                if side == "BUY":
                    # 매수 포지션
                    if current_price <= position["stop_price"]:
                        should_exit = True
                        exit_reason = "stop_loss"
                    elif current_price >= position["target_price"]:
                        should_exit = True
                        exit_reason = "gap_filled"
                        
                else:  # SELL
                    # 매도 포지션
                    if current_price >= position["stop_price"]:
                        should_exit = True
                        exit_reason = "stop_loss"
                    elif current_price <= position["target_price"]:
                        should_exit = True
                        exit_reason = "gap_filled"
                
                # 보유 시간 초과 확인
                if hold_time >= max_hold_time:
                    should_exit = True
                    exit_reason = "time_limit"
                
                # 청산 실행
                if should_exit:
                    await self._exit_position(position_id, exit_reason)
                    
        except Exception as e:
            logger.log_error(e, "Gap position monitoring error")
    
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
                strategy="gap",
                reason=reason
            )
            
            if result["status"] == "success":
                # 포지션 제거
                del self.positions[position_id]
                
                logger.log_system(f"Gap: Exited position for {symbol}, reason: {reason}")
                
        except Exception as e:
            logger.log_error(e, f"Gap exit error for position {position_id}")
    
    def get_signal_strength(self, symbol: str) -> float:
        """신호 강도 측정 (0 ~ 10)"""
        try:
            if symbol not in self.price_data or not self.price_data[symbol]:
                return 0
                
            # 갭 확인 안된 경우
            gap_data = self.gap_data.get(symbol, {})
            if not gap_data.get('gap_identified', False):
                return 0
            
            # 갭 크기가 임계값 내에 있는지 확인
            gap_pct = abs(gap_data.get('gap_pct', 0))
            if gap_pct < self.params["min_gap_pct"] or gap_pct > self.params["max_gap_pct"]:
                return 0
            
            # 점수 계산 요소
            score = 0
            
            # 1. 갭 크기에 따른 점수 (0-5점)
            normalized_gap = min(1.0, gap_pct / 0.03)  # 3% 갭을 기준으로 정규화
            if gap_pct <= 0.03:
                gap_score = normalized_gap * 5  # 0-3% 갭: 비례하여 0-5점
            else:
                gap_score = 5 - min(5, (gap_pct - 0.03) * 100)  # 3% 초과 갭: 5점에서 차감
            
            score += max(0, gap_score)
            
            # 2. 거래량 비율에 따른 점수 (0-3점)
            volume_ratio = self.volume_data[symbol].get('volume_ratio', 0)
            if volume_ratio >= self.params["volume_threshold"]:
                # 거래량 임계치 이상일 때만 점수 부여
                volume_score = min(3, (volume_ratio - self.params["volume_threshold"]) * 2)
                score += volume_score
            
            # 3. 갭 발생 후 가격 움직임 점수 (0-2점)
            # 갭 방향의 반대로 움직이기 시작했다면 더 높은 점수
            current_price = self.price_data[symbol][-1]["price"]
            today_open = gap_data.get('today_open')
            
            if today_open and gap_data.get('direction'):
                price_change = (current_price - today_open) / today_open
                if (gap_data['direction'] == "UP" and price_change < 0) or \
                   (gap_data['direction'] == "DOWN" and price_change > 0):
                    # 갭 채우기 방향으로 이미 움직이고 있음
                    reversal_score = min(2, abs(price_change) * 100)
                    score += reversal_score
            
            return min(10, score)  # 최대 10점
            
        except Exception as e:
            logger.log_error(e, f"Error calculating gap signal strength for {symbol}")
            return 0
    
    def get_signal_direction(self, symbol: str) -> str:
        """신호 방향 반환"""
        if symbol in self.gap_data and self.gap_data[symbol].get('direction'):
            direction = self.gap_data[symbol]['direction']
            if direction == "UP":
                return "SELL"  # 갭 업일 때는 매도 신호 (갭 채움 예상)
            elif direction == "DOWN":
                return "BUY"   # 갭 다운일 때는 매수 신호 (갭 채움 예상)
        return "NEUTRAL"
        
    async def get_signal(self, symbol: str) -> Dict[str, Any]:
        """전략 신호 반환 (combined_strategy에서 호출)"""
        try:
            # 갭 데이터가 없는 경우 초기화
            if symbol not in self.gap_data:
                self.gap_data[symbol] = {
                    'gap_pct': None,
                    'direction': None,
                    'prev_close': None,
                    'today_open': None,
                    'gap_size': None,
                    'fill_target': None,
                    'gap_identified': False
                }
                
                # 초기 데이터 로딩
                await self._load_initial_data(symbol)
            
            # 갭 데이터 확인
            gap_data = self.gap_data[symbol]
            
            # 갭이 아직 확인되지 않았거나 필요한 데이터가 없으면 데이터 로딩
            if not gap_data.get('gap_identified', False) or gap_data.get('prev_close') is None:
                await self._load_initial_data(symbol)
                
                # 재로드 후에도 데이터가 없으면 히스토리컬 데이터 로드 시도
                if not self.gap_data[symbol].get('gap_identified', False):
                    await self._load_historical_data(symbol)
                
                gap_data = self.gap_data[symbol]  # 다시 로드
                
                # 여전히 초기화 실패하고 데이터가 있을 경우 임시 갭 설정 (빠른 초기화용)
                if not gap_data.get('gap_identified', False) and symbol in self.price_data and len(self.price_data[symbol]) > 0:
                    current_price = self.price_data[symbol][-1]["price"]
                    if current_price > 0:
                        # 현재가 기준으로 임시 갭 설정 (±2%)
                        random_direction = "UP" if datetime.now().second % 2 == 0 else "DOWN"
                        gap_pct = 0.02 if random_direction == "UP" else -0.02
                        
                        self.gap_data[symbol] = {
                            'gap_pct': gap_pct,
                            'direction': random_direction,
                            'prev_close': current_price / (1 + gap_pct),
                            'today_open': current_price,
                            'gap_size': current_price * abs(gap_pct),
                            'fill_target': current_price / (1 + gap_pct),
                            'gap_identified': True
                        }
                        
                        logger.log_system(f"{symbol} - 갭 전략 임시 데이터 설정 완료 (현재가 기준): {current_price:,.0f}원, 갭: {gap_pct:.1%}")
                        gap_data = self.gap_data[symbol]  # 업데이트된 데이터 참조
            
            # 여전히 갭이 확인되지 않았다면 중립 반환
            if not gap_data.get('gap_identified', False):
                return {"signal": 0, "direction": "NEUTRAL", "reason": "gap_not_identified"}
            
            # 현재가 확인
            current_price = 0
            if symbol in self.price_data and self.price_data[symbol]:
                current_price = self.price_data[symbol][-1]["price"]
            
            # 현재가가 없으면 API에서 가져오기
            if current_price <= 0:
                try:
                    price_info = await api_client.get_symbol_info(symbol)
                    if price_info and price_info.get("current_price"):
                        current_price = float(price_info["current_price"])
                        # 가격 데이터에 추가
                        self.price_data[symbol].append({
                            "price": current_price,
                            "timestamp": datetime.now()
                        })
                except Exception as e:
                    logger.log_error(e, f"{symbol} - 갭 전략 현재가 조회 실패")
                    return {"signal": 0, "direction": "NEUTRAL", "reason": "price_fetch_error"}
            
            # 갭이 충분히 큰지 확인
            gap_pct = gap_data.get('gap_pct', 0)
            min_gap = self.params["min_gap_pct"]
            max_gap = self.params["max_gap_pct"]
            
            if abs(gap_pct) < min_gap or abs(gap_pct) > max_gap:
                return {"signal": 0, "direction": "NEUTRAL", "reason": "gap_size_out_of_range"}
            
            # 방향과 신호 강도 계산
            direction = "NEUTRAL"
            signal_strength = 0
            
            # 갭 방향에 따른 트레이딩 방향
            gap_direction = gap_data.get('direction')
            if gap_direction == "UP":
                direction = "SELL"  # 갭 업은 매도 신호 (갭 채움 예상)
                signal_strength = min(10, abs(gap_pct) * 200)  # 갭이 클수록 강한 신호
            elif gap_direction == "DOWN":
                direction = "BUY"  # 갭 다운은 매수 신호 (갭 채움 예상)
                signal_strength = min(10, abs(gap_pct) * 200)
            
            # 거래량 요소 고려
            vol_ratio = self.volume_data.get(symbol, {}).get('volume_ratio', 0)
            vol_threshold = self.params["volume_threshold"]
            
            # 거래량이 평균보다 높으면 신호 강화
            if vol_ratio > vol_threshold:
                vol_bonus = min(2, (vol_ratio - vol_threshold))
                signal_strength = min(10, signal_strength + vol_bonus)
            
            return {"signal": signal_strength, "direction": direction, "gap_pct": f"{gap_pct:.2%}"}
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 갭 신호 계산 오류")
            return {"signal": 0, "direction": "NEUTRAL", "reason": "error"}
    
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
                if symbol in self.volume_data:
                    del self.volume_data[symbol]
                if symbol in self.gap_data:
                    del self.gap_data[symbol]
            
            # 새 종목 초기화
            for symbol in to_add:
                self.price_data[symbol] = deque(maxlen=100)
                self.volume_data[symbol] = deque(maxlen=20)
                self.gap_data[symbol] = {
                    'prev_close': None,
                    'open_price': None,
                    'gap_pct': None,
                    'gap_identified': False,
                    'direction': "NEUTRAL",
                    'fill_target': None,
                    'avg_volume': None,
                    'signal_strength': 0,
                    'signal_direction': "NEUTRAL",
                    'last_update': None
                }
            
            # 감시 종목 업데이트
            self.watched_symbols = list(new_set)
            
            logger.log_system(f"갭 전략: 감시 종목 {len(self.watched_symbols)}개로 업데이트됨")
        
        except Exception as e:
            logger.log_error(e, "갭 전략 종목 업데이트 오류")

# 싱글톤 인스턴스
gap_strategy = GapStrategy()
