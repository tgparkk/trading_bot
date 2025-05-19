"""
VWAP 기반 전략 (VWAP Based Strategy)
거래량 가중 평균 가격(VWAP)을 기준으로 매매하는 전략
"""
import asyncio
from typing import Dict, Any, List, Optional, Deque
from datetime import datetime, time, timedelta
from collections import deque
import numpy as np
import threading

from config.settings import config
from core.api_client import api_client
from core.websocket_client import ws_client
from core.order_manager import order_manager
from utils.logger import logger
from monitoring.alert_system import alert_system

class VWAPStrategy:
    """VWAP 기반 전략 클래스"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_initialized') or not self._initialized:
            # get 메서드 대신 직접 속성 접근 또는 기본값 설정
            self.params = {
                "std_dev_multiplier": 2,        # 표준편차 승수 (밴드 폭)
                "entry_threshold": 0.003,       # 진입 임계값 (0.3%)
                "exit_threshold": 0.005,        # 이탈 임계값 (0.5%)
                "stop_loss_pct": 0.01,          # 손절 비율 (1%)
                "take_profit_pct": 0.02,        # 익절 비율 (2%)
                "max_positions": 3,             # 최대 포지션 개수
                "position_size": 1000000,       # 기본 포지션 크기 (100만원)
                "reset_daily": True,            # VWAP 일일 리셋 여부
                "band_factor": 0.005             # 임시 VWAP 계산용 밴드 폭
            }
            
            # 설정에 vwap_params가 있으면 업데이트
            if hasattr(config["trading"], "vwap_params"):
                self.params.update(config["trading"].vwap_params)
            
            self.running = False
            self.paused = False
            self.watched_symbols = set()
            self.price_data = {}              # {symbol: deque of price data}
            self.vwap_data = {}               # {symbol: {'vwap': float, 'upper_band': float, 'lower_band': float}}
            self.positions = {}               # {position_id: position_data}
            self.last_reset_day = datetime.now().date()
            self.initialization_complete = {}  # {symbol: bool}
            self._initialized = True
        
    async def start(self, symbols: List[str]):
        """전략 시작"""
        try:
            self.running = True
            self.paused = False
            self.watched_symbols = set(symbols)
            
            # 각 종목별 데이터 초기화
            for symbol in symbols:
                self.price_data[symbol] = deque(maxlen=2000)  # 충분히 많은 데이터 저장
                self.vwap_data[symbol] = deque(maxlen=2000)
                self.initialization_complete[symbol] = False
                
                # 웹소켓 구독
                await ws_client.subscribe_price(symbol, self._handle_price_update)
                
                # 초기 데이터 로드
                await self._load_initial_data(symbol)
            
            logger.log_system(f"VWAP strategy started for {len(symbols)} symbols")
            
            # 전략 실행 루프
            asyncio.create_task(self._strategy_loop())
            
        except Exception as e:
            logger.log_error(e, "Failed to start VWAP strategy")
            await alert_system.notify_error(e, "VWAP strategy start error")
    
    async def _load_initial_data(self, symbol: str):
        """초기 데이터 로딩"""
        try:
            # 기존 데이터 초기화
            self.price_data[symbol] = deque(maxlen=2000)
            self.vwap_data[symbol] = deque(maxlen=2000)
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
                logger.log_warning(f"{symbol} - VWAP 전략 초기 데이터 부족")
                return
            
            # output2가 실제 차트 데이터를 담고 있음
            chart_data = data.get("output2", [])
            if not chart_data or len(chart_data) < 20:  # 최소 20개 데이터 필요
                logger.log_warning(f"{symbol} - VWAP 전략 초기 데이터 부족")
                return
            
            # 데이터 저장 및 VWAP 계산
            cumulative_pv = 0
            cumulative_volume = 0
            
            for item in chart_data:
                price = float(item["stck_prpr"])
                volume = int(item.get("cntg_vol", 0))
                
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
                    "price": price,
                    "volume": volume
                })
                
                # VWAP 계산
                cumulative_pv += price * volume
                cumulative_volume += volume
                vwap = cumulative_pv / cumulative_volume if cumulative_volume > 0 else price
                
                self.vwap_data[symbol].append({
                    "timestamp": timestamp,
                    "vwap": vwap
                })
            
            if len(self.vwap_data[symbol]) >= 20:
                self.initialization_complete[symbol] = True
                logger.log_system(f"{symbol} - VWAP 전략 초기화 완료")
                logger.log_system(f"초기 VWAP: {self.vwap_data[symbol][-1]['vwap']:,.0f}")
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - VWAP 전략 초기 데이터 로딩 오류")
    
    async def stop(self):
        """전략 중지"""
        self.running = False
        
        # 웹소켓 구독 해제
        for symbol in self.watched_symbols:
            await ws_client.unsubscribe(symbol, "price")
        
        logger.log_system("VWAP strategy stopped")
    
    async def pause(self):
        """전략 일시 중지"""
        if not self.paused:
            self.paused = True
            logger.log_system("VWAP strategy paused")
        return True

    async def resume(self):
        """전략 재개"""
        if self.paused:
            self.paused = False
            logger.log_system("VWAP strategy resumed")
        return True
    
    async def _handle_price_update(self, data: Dict[str, Any]):
        """실시간 체결가 업데이트 처리"""
        try:
            symbol = data.get("tr_key")
            price = float(data.get("stck_prpr", 0))
            volume = int(data.get("cntg_vol", 0))
            
            if symbol in self.price_data and price > 0 and volume > 0:
                timestamp = datetime.now()
                self.price_data[symbol].append({
                    "price": price,
                    "volume": volume,
                    "timestamp": timestamp
                })
                
                # 현재 날짜 확인 - 일일 리셋 필요한지
                current_date = timestamp.date()
                if self.params["reset_daily"] and current_date > self.last_reset_day:
                    self._reset_vwap_data()
                    self.last_reset_day = current_date
                
                # VWAP 계산 업데이트 (일정 간격마다)
                if self.vwap_data[symbol][-1]["vwap"] is None or \
                   (timestamp - self.vwap_data[symbol][-1]["timestamp"]).total_seconds() >= 3:
                    self._update_vwap(symbol, price, volume)
                
        except Exception as e:
            logger.log_error(e, "Error handling price update in VWAP strategy")
    
    def _reset_vwap_data(self):
        """일일 VWAP 데이터 리셋"""
        for symbol in self.watched_symbols:
            self.vwap_data[symbol] = deque(maxlen=2000)
            
            # 가격 데이터는 유지하되 당일 데이터만 남기기
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            self.price_data[symbol] = deque(
                [data for data in self.price_data[symbol] if data["timestamp"] >= today_start],
                maxlen=self.price_data[symbol].maxlen
            )
            
        logger.log_system("VWAP data reset for new trading day")
    
    def _update_vwap(self, symbol: str, price: float, volume: int):
        """VWAP 및 밴드 업데이트"""
        try:
            vwap_data = self.vwap_data[symbol]
            
            # 누적 거래량 및 누적 거래대금 업데이트
            vwap_data.append({
                "timestamp": datetime.now(),
                "vwap": price
            })
            
            # VWAP 계산
            if len(vwap_data) > 0:
                vwap = vwap_data[-1]["vwap"]
                
                # 표준편차 계산
                if len(self.price_data[symbol]) > 1:
                    mean_price_squared = vwap ** 2
                    variance = (price ** 2) - mean_price_squared
                    std_dev = max(0, variance) ** 0.5  # 음수일 경우 0으로 처리
                    
                    # 밴드 계산
                    multiplier = self.params['std_dev_multiplier']
                    upper_band = vwap + (std_dev * multiplier)
                    lower_band = vwap - (std_dev * multiplier)
                
                else:
                    upper_band = vwap
                    lower_band = vwap
            
        except Exception as e:
            logger.log_error(e, f"Error updating VWAP for {symbol}")
    
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
                
                # 데이터가 충분히 쌓였을 때만 (9:20 이후) 트레이딩 실행
                if current_time >= time(9, 20):
                    for symbol in self.watched_symbols:
                        if self.vwap_data[symbol][-1]["vwap"] is not None:
                            await self._analyze_and_trade(symbol)
                
                # 포지션 모니터링
                await self._monitor_positions()
                
                await asyncio.sleep(1)  # 1초 대기
                
            except Exception as e:
                logger.log_error(e, "VWAP strategy loop error")
                await asyncio.sleep(5)  # 에러 시 5초 대기
    
    async def _analyze_and_trade(self, symbol: str):
        """종목 분석 및 거래"""
        try:
            # 전략이 일시 중지 상태인지 확인
            if self.paused or order_manager.is_trading_paused():
                return
                
            # VWAP 데이터 있는지 확인
            vwap_data = self.vwap_data.get(symbol, {})
            if not vwap_data.get('vwap') or not vwap_data.get('upper_band') or not vwap_data.get('lower_band'):
                return
            
            # 충분한 데이터 있는지 확인
            if not self.price_data[symbol]:
                return
                
            # 현재가
            current_price = self.price_data[symbol][-1]["price"]
            
            # 이미 포지션 있는지 확인
            symbol_positions = self._get_symbol_positions(symbol)
            if len(symbol_positions) >= self.params["max_positions"]:
                return
            
            vwap = vwap_data[-1]["vwap"]
            upper_band = vwap_data[-1]["upper_band"]
            lower_band = vwap_data[-1]["lower_band"]
            entry_threshold = self.params['entry_threshold']
            
            # 이전 가격 확인 (방향성 확인용)
            if len(self.price_data[symbol]) < 2:
                return
                
            prev_price = self.price_data[symbol][-2]["price"]
            
            # 매수 조건: 가격이 VWAP 아래에서 상향 돌파
            buy_condition = False
            if prev_price < vwap and current_price > vwap * (1 + entry_threshold):
                buy_condition = True
            
            # 추가 매수 조건: 가격이 밴드 하단에서 반등
            additional_buy_condition = False
            if current_price < lower_band * (1 + entry_threshold) and current_price > prev_price:
                additional_buy_condition = True
            
            # 매도 조건: 가격이 VWAP 위에서 하향 돌파
            sell_condition = False
            if prev_price > vwap and current_price < vwap * (1 - entry_threshold):
                sell_condition = True
            
            # 추가 매도 조건: 가격이 밴드 상단에서 하락
            additional_sell_condition = False
            if current_price > upper_band * (1 - entry_threshold) and current_price < prev_price:
                additional_sell_condition = True
            
            # 매매 실행
            if buy_condition or additional_buy_condition:
                reason = "vwap_cross_up" if buy_condition else "lower_band_reversal"
                await self._enter_position(symbol, "BUY", current_price, reason)
                
            elif sell_condition or additional_sell_condition:
                reason = "vwap_cross_down" if sell_condition else "upper_band_reversal"
                await self._enter_position(symbol, "SELL", current_price, reason)
                
        except Exception as e:
            logger.log_error(e, f"VWAP analysis error for {symbol}")
    
    def _get_symbol_positions(self, symbol: str) -> List[str]:
        """특정 종목의 포지션 ID 목록 반환"""
        return [
            position_id for position_id, position in self.positions.items()
            if position["symbol"] == symbol
        ]
    
    async def _enter_position(self, symbol: str, side: str, current_price: float, reason: str):
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
                strategy="vwap",
                reason=reason
            )
            
            if result["status"] == "success":
                # 손절/익절 가격 계산
                stop_loss_pct = self.params["stop_loss_pct"]
                take_profit_pct = self.params["take_profit_pct"]
                
                if side == "BUY":
                    stop_price = current_price * (1 - stop_loss_pct)
                    target_price = current_price * (1 + take_profit_pct)
                else:  # SELL
                    stop_price = current_price * (1 + stop_loss_pct)
                    target_price = current_price * (1 - take_profit_pct)
                
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
                    "reason": reason,
                    "vwap_at_entry": vwap
                }
                
                logger.log_system(
                    f"VWAP: Entered {side} position for {symbol} at {current_price}, "
                    f"stop: {stop_price}, target: {target_price}, reason: {reason}"
                )
                
        except Exception as e:
            logger.log_error(e, f"VWAP entry error for {symbol}")
    
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
                vwap_at_entry = position.get("vwap_at_entry")
                
                # 현재 VWAP 확인
                current_vwap = self.vwap_data[symbol][-1]["vwap"]
                exit_threshold = self.params['exit_threshold']
                
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
                    # VWAP 기반 청산 - 가격이 VWAP를 하향 돌파하면서 VWAP가 변경됨
                    elif current_vwap and vwap_at_entry and current_price < current_vwap * (1 - exit_threshold) and current_vwap != vwap_at_entry:
                        should_exit = True
                        exit_reason = "vwap_reversal"
                        
                else:  # SELL
                    # 매도 포지션
                    if current_price >= position["stop_price"]:
                        should_exit = True
                        exit_reason = "stop_loss"
                    elif current_price <= position["target_price"]:
                        should_exit = True
                        exit_reason = "take_profit"
                    # VWAP 기반 청산 - 가격이 VWAP를 상향 돌파하면서 VWAP가 변경됨
                    elif current_vwap and vwap_at_entry and current_price > current_vwap * (1 + exit_threshold) and current_vwap != vwap_at_entry:
                        should_exit = True
                        exit_reason = "vwap_reversal"
                
                # 청산 실행
                if should_exit:
                    await self._exit_position(position_id, exit_reason)
                    
        except Exception as e:
            logger.log_error(e, "VWAP position monitoring error")
    
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
                strategy="vwap",
                reason=reason
            )
            
            if result["status"] == "success":
                # 포지션 제거
                del self.positions[position_id]
                
                logger.log_system(f"VWAP: Exited position for {symbol}, reason: {reason}")
                
        except Exception as e:
            logger.log_error(e, f"VWAP exit error for position {position_id}")
    
    def get_signal_strength(self, symbol: str) -> float:
        """신호 강도 측정 (0 ~ 10)"""
        try:
            if symbol not in self.price_data or not self.price_data[symbol]:
                return 0
                
            # 초기화 안 된 경우 초기화 시도
            if not self.initialization_complete.get(symbol, False):
                # 비동기 초기화를 동기적으로 실행
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self._load_initial_data(symbol))
                return 0
            
            current_price = self.price_data[symbol][-1]["price"]
            current_vwap = self.vwap_data[symbol][-1]["vwap"]
            
            # VWAP 대비 가격 편차 계산
            price_deviation = (current_price - current_vwap) / current_vwap
            
            # 상단 밴드 돌파 점수
            if price_deviation > self.params["upper_band"]:
                # 얼마나 많이 돌파했는지 계산 (최대 2%)
                breakout_pct = (price_deviation - self.params["upper_band"]) / self.params["upper_band"]
                return min(10, breakout_pct * 500)  # 최대 2% 돌파 시 10점
                
            # 하단 밴드 돌파 점수
            elif price_deviation < -self.params["lower_band"]:
                # 얼마나 많이 돌파했는지 계산 (최대 2%)
                breakout_pct = (abs(price_deviation) - self.params["lower_band"]) / self.params["lower_band"]
                return min(10, breakout_pct * 500)  # 최대 2% 돌파 시 10점
            
            # VWAP 근접도 점수 (밴드까지 남은 거리, 최대 0.5%)
            upper_proximity = (self.params["upper_band"] - price_deviation) / self.params["upper_band"]
            lower_proximity = (price_deviation + self.params["lower_band"]) / self.params["lower_band"]
            
            if upper_proximity < 0.005:  # 상단 밴드에 0.5% 내로 근접
                return min(5, (0.005 - upper_proximity) * 1000)  # 최대 5점
                
            if lower_proximity < 0.005:   # 하단 밴드에 0.5% 내로 근접
                return min(5, (0.005 - lower_proximity) * 1000)  # 최대 5점
            
            return 0  # 신호 없음
            
        except Exception as e:
            logger.log_error(e, f"Error calculating VWAP signal strength for {symbol}")
            return 0
    
    def get_signal_direction(self, symbol: str) -> str:
        """신호 방향 반환"""
        if symbol not in self.price_data or not self.price_data[symbol]:
            return "NEUTRAL"
            
        if symbol not in self.vwap_data or not self.vwap_data[symbol].get('vwap'):
            return "NEUTRAL"
            
        current_price = self.price_data[symbol][-1]["price"]
        vwap_data = self.vwap_data[symbol]
        vwap = vwap_data[-1]["vwap"]
        upper_band = vwap_data[-1]["upper_band"]
        lower_band = vwap_data[-1]["lower_band"]
        
        # 밴드 돌파로 신호 판단
        if upper_band and current_price > upper_band:
            return "SELL"  # 상단 밴드 돌파 시 매도
        elif lower_band and current_price < lower_band:
            return "BUY"   # 하단 밴드 돌파 시 매수
        
        # VWAP 돌파로 신호 판단
        threshold = self.params["entry_threshold"]
        price_diff_pct = (current_price - vwap) / vwap
        
        if abs(price_diff_pct) >= threshold:
            return "SELL" if price_diff_pct > 0 else "BUY"
            
        return "NEUTRAL"
        
    async def get_signal(self, symbol: str) -> Dict[str, Any]:
        """전략 신호 반환 (combined_strategy에서 호출)"""
        try:
            # 종목 데이터가 없으면 초기화
            if symbol not in self.price_data:
                self.price_data[symbol] = deque(maxlen=2000)
                logger.log_system(f"VWAP 전략: {symbol} 가격 데이터 구조 초기화됨")
                
            if symbol not in self.vwap_data:
                self.vwap_data[symbol] = deque(maxlen=2000)
                logger.log_system(f"VWAP 전략: {symbol} VWAP 데이터 구조 초기화됨")
            
            # VWAP 데이터가 없으면 초기화
            if self.vwap_data[symbol][-1]["vwap"] is None:
                # 적극적인 초기화 시도
                await self._load_initial_data(symbol)
                
                # 초기화 후에도 데이터가 없으면 임시 VWAP 설정 (빠른 초기화용)
                if (symbol in self.price_data and len(self.price_data[symbol]) > 0 and 
                    symbol in self.vwap_data and self.vwap_data[symbol][-1]["vwap"] is None):
                    current_price = self.price_data[symbol][-1]["price"]
                    if current_price > 0:
                        # 임시 VWAP 데이터 설정
                        band_factor = self.params["band_factor"]
                        self.vwap_data[symbol].append({
                            "timestamp": datetime.now(),
                            "vwap": current_price,
                            "upper_band": current_price * (1 + band_factor),
                            "lower_band": current_price * (1 - band_factor)
                        })
                        logger.log_system(f"{symbol} - VWAP 임시 데이터 설정 완료 (현재가 기준): {current_price:,.0f}원")
            
            # 충분한 데이터가 있는지 확인
            if symbol not in self.price_data or not self.price_data[symbol]:
                return {"signal": 0, "direction": "NEUTRAL", "reason": "no_price_data"}
            
            # VWAP 데이터 확인
            vwap_data = self.vwap_data.get(symbol)
            if not vwap_data or len(vwap_data) == 0:
                return {"signal": 0, "direction": "NEUTRAL", "reason": "no_vwap_data"}
            
            # 현재 VWAP 값 확인
            current_vwap = vwap_data[-1]["vwap"] if vwap_data else None
            if not current_vwap:
                return {"signal": 0, "direction": "NEUTRAL", "reason": "no_vwap_value"}
            
            # 현재가 확인
            current_price = 0
            if self.price_data[symbol]:
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
                            "timestamp": datetime.now(),
                            "volume": 100  # 기본 거래량
                        })
                except Exception as e:
                    logger.log_error(e, f"{symbol} - VWAP 현재가 조회 실패")
                    return {"signal": 0, "direction": "NEUTRAL", "reason": "price_fetch_error"}
            
            # VWAP 데이터
            vwap = current_vwap
            upper_band = vwap * (1 + self.params["std_dev_multiplier"] * self.params["band_factor"])
            lower_band = vwap * (1 - self.params["std_dev_multiplier"] * self.params["band_factor"])
            
            # 방향과 신호 강도 계산
            direction = "NEUTRAL"
            signal_strength = 0
            
            # 밴드 돌파 확인
            if current_price > upper_band:
                # 상단 밴드 돌파 - 매도 신호
                direction = "SELL"
                band_diff_pct = (current_price - upper_band) / upper_band
                signal_strength = min(10, band_diff_pct * 200)  # 0.5% 돌파 시 최대 강도
                
            elif current_price < lower_band:
                # 하단 밴드 돌파 - 매수 신호
                direction = "BUY"
                band_diff_pct = (lower_band - current_price) / lower_band
                signal_strength = min(10, band_diff_pct * 200)  # 0.5% 돌파 시 최대 강도
            
            # VWAP 레벨 확인 (밴드 돌파가 없을 경우)
            if direction == "NEUTRAL":
                price_diff_pct = (current_price - vwap) / vwap
                
                # VWAP 대비 위치로 방향 결정
                if abs(price_diff_pct) >= self.params["entry_threshold"]:
                    if price_diff_pct > 0:
                        direction = "SELL"  # VWAP 상향 돌파는 매도 (평균 회귀 예상)
                    else:
                        direction = "BUY"   # VWAP 하향 돌파는 매수 (반등 예상)
                    
                    signal_strength = min(8, abs(price_diff_pct) * 100)
            
            # 거래량 요소 고려
            avg_volume = 0
            current_volume = 0
            
            if len(self.price_data[symbol]) > 10:
                # 볼륨 데이터가 있는지 확인
                if "volume" in self.price_data[symbol][-1]:
                    current_volume = self.price_data[symbol][-1]["volume"]
                    # 볼륨 데이터가 있는 항목만 수집
                    volume_items = [item for item in list(self.price_data[symbol])[-10:] if "volume" in item]
                    if volume_items:
                        avg_volume = np.mean([item["volume"] for item in volume_items])
            
            if avg_volume > 0 and current_volume > avg_volume * 1.5:
                vol_bonus = min(2, (current_volume / avg_volume - 1.5) * 2)
                signal_strength = min(10, signal_strength + vol_bonus)
            
            # VWAP 정보 추가
            vwap_info = {
                "vwap": vwap,
                "upper_band": upper_band,
                "lower_band": lower_band,
                "price_to_vwap": f"{((current_price/vwap)-1)*100:.2f}%"
            }
            
            return {
                "signal": signal_strength, 
                "direction": direction,
                "vwap_info": vwap_info
            }
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - VWAP 신호 계산 오류")
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
                if symbol in self.vwap_data:
                    del self.vwap_data[symbol]
            
            # 새 종목 초기화
            for symbol in to_add:
                self.price_data[symbol] = deque(maxlen=2000)
                self.vwap_data[symbol] = deque(maxlen=2000)
            
            # 감시 종목 업데이트
            self.watched_symbols = list(new_set)
            
            logger.log_system(f"VWAP 전략: 감시 종목 {len(self.watched_symbols)}개로 업데이트됨")
        
        except Exception as e:
            logger.log_error(e, "VWAP 전략 종목 업데이트 오류")

# 싱글톤 인스턴스
vwap_strategy = VWAPStrategy()
