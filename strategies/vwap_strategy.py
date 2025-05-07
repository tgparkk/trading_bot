"""
VWAP 기반 전략 (VWAP Based Strategy)
거래량 가중 평균 가격(VWAP)을 기준으로 매매하는 전략
"""
import asyncio
from typing import Dict, Any, List, Optional, Deque
from datetime import datetime, time, timedelta
from collections import deque
import numpy as np

from config.settings import config
from core.api_client import api_client
from core.websocket_client import ws_client
from core.order_manager import order_manager
from utils.logger import logger
from monitoring.alert_system import alert_system

class VWAPStrategy:
    """VWAP 기반 전략 클래스"""
    
    def __init__(self):
        # get 메서드 대신 직접 속성 접근 또는 기본값 설정
        self.params = {
            "std_dev_multiplier": 2,        # 표준편차 승수 (밴드 폭)
            "entry_threshold": 0.003,       # 진입 임계값 (0.3%)
            "exit_threshold": 0.005,        # 이탈 임계값 (0.5%)
            "stop_loss_pct": 0.01,          # 손절 비율 (1%)
            "take_profit_pct": 0.02,        # 익절 비율 (2%)
            "max_positions": 3,             # 최대 포지션 개수
            "position_size": 1000000,       # 기본 포지션 크기 (100만원)
            "reset_daily": True             # VWAP 일일 리셋 여부
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
        
    async def start(self, symbols: List[str]):
        """전략 시작"""
        try:
            self.running = True
            self.paused = False
            self.watched_symbols = set(symbols)
            
            # 각 종목별 데이터 초기화
            for symbol in symbols:
                self.price_data[symbol] = deque(maxlen=2000)  # 충분히 많은 데이터 저장
                self.vwap_data[symbol] = {
                    'vwap': None,
                    'upper_band': None, 
                    'lower_band': None,
                    'std_dev': None,
                    'cumulative_volume': 0,
                    'cumulative_volume_price': 0,
                    'price_volume_squared': 0,  # 표준편차 계산용
                    'last_calculation_time': None
                }
                
                # 웹소켓 구독
                await ws_client.subscribe_price(symbol, self._handle_price_update)
                
                # 장 초반 데이터 로드 (필요한 경우)
                # await self._load_initial_data(symbol)
            
            logger.log_system(f"VWAP strategy started for {len(symbols)} symbols")
            
            # 전략 실행 루프
            asyncio.create_task(self._strategy_loop())
            
        except Exception as e:
            logger.log_error(e, "Failed to start VWAP strategy")
            await alert_system.notify_error(e, "VWAP strategy start error")
    
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
                if self.vwap_data[symbol]['last_calculation_time'] is None or \
                   (timestamp - self.vwap_data[symbol]['last_calculation_time']).total_seconds() >= 3:
                    self._update_vwap(symbol, price, volume)
                
        except Exception as e:
            logger.log_error(e, "Error handling price update in VWAP strategy")
    
    def _reset_vwap_data(self):
        """일일 VWAP 데이터 리셋"""
        for symbol in self.watched_symbols:
            self.vwap_data[symbol] = {
                'vwap': None,
                'upper_band': None, 
                'lower_band': None,
                'std_dev': None,
                'cumulative_volume': 0,
                'cumulative_volume_price': 0,
                'price_volume_squared': 0,  # 표준편차 계산용
                'last_calculation_time': None
            }
            
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
            vwap_data['cumulative_volume'] += volume
            vwap_data['cumulative_volume_price'] += volume * price
            
            # 제곱항 업데이트 (표준편차 계산용)
            vwap_data['price_volume_squared'] += volume * (price ** 2)
            
            # VWAP 계산
            if vwap_data['cumulative_volume'] > 0:
                vwap = vwap_data['cumulative_volume_price'] / vwap_data['cumulative_volume']
                vwap_data['vwap'] = vwap
                
                # 표준편차 계산
                if vwap_data['cumulative_volume'] > 1:
                    mean_price_squared = vwap ** 2
                    variance = vwap_data['price_volume_squared'] / vwap_data['cumulative_volume'] - mean_price_squared
                    std_dev = max(0, variance) ** 0.5  # 음수일 경우 0으로 처리
                    vwap_data['std_dev'] = std_dev
                    
                    # 밴드 계산
                    multiplier = self.params['std_dev_multiplier']
                    vwap_data['upper_band'] = vwap + (std_dev * multiplier)
                    vwap_data['lower_band'] = vwap - (std_dev * multiplier)
            
            vwap_data['last_calculation_time'] = datetime.now()
            
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
                        if self.vwap_data[symbol]['vwap'] is not None:
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
            
            vwap = vwap_data['vwap']
            upper_band = vwap_data['upper_band']
            lower_band = vwap_data['lower_band']
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
                    "vwap_at_entry": self.vwap_data[symbol]['vwap']
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
                current_vwap = self.vwap_data[symbol].get('vwap')
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
                
            # VWAP 데이터 있는지 확인
            vwap_data = self.vwap_data.get(symbol, {})
            if not vwap_data.get('vwap') or not vwap_data.get('upper_band') or not vwap_data.get('lower_band'):
                return 0
            
            # 현재가와 이전가
            if len(self.price_data[symbol]) < 2:
                return 0
                
            current_price = self.price_data[symbol][-1]["price"]
            prev_price = self.price_data[symbol][-2]["price"]
            
            vwap = vwap_data['vwap']
            upper_band = vwap_data['upper_band']
            lower_band = vwap_data['lower_band']
            
            # 점수 계산
            score = 0
            
            # 1. VWAP 크로싱 점수 (0-5점)
            vwap_distance = abs(current_price - vwap) / vwap
            
            # VWAP 크로싱 감지 (상향/하향)
            if (prev_price < vwap and current_price > vwap) or (prev_price > vwap and current_price < vwap):
                # 크로싱 발생 - 기본 3점
                cross_score = 3
                
                # 크로싱 후 거리에 따라 추가 점수
                distance_score = min(2, vwap_distance * 100)
                
                score += cross_score + distance_score
            else:
                # 크로싱이 아닌 경우, VWAP와의 거리에 반비례하는 점수
                proximity_score = max(0, 1 - (vwap_distance * 100))
                score += proximity_score
            
            # 2. 밴드 관련 점수 (0-5점)
            band_width = (upper_band - lower_band) / vwap  # 밴드 폭 (상대적)
            
            # 밴드 터치 또는 접근
            if current_price >= upper_band or current_price <= lower_band:
                # 밴드 극단에 위치 - 반전 가능성
                band_score = 3
                
                # 밴드 폭이 넓을수록 신호 강도 증가
                width_score = min(2, band_width * 50)
                
                score += band_score + width_score
            else:
                # 밴드 내부에 위치한 경우, 밴드 가장자리 접근도에 따른 점수
                band_distance = min(
                    abs(current_price - upper_band) / (upper_band - vwap),
                    abs(current_price - lower_band) / (vwap - lower_band)
                )
                proximity_score = max(0, 3 * (1 - band_distance))
                score += proximity_score
            
            return min(10, score)  # 최대 10점
            
        except Exception as e:
            logger.log_error(e, f"Error calculating VWAP signal strength for {symbol}")
            return 0
    
    def get_signal_direction(self, symbol: str) -> str:
        """신호 방향 (BUY/SELL/NEUTRAL)"""
        try:
            if symbol not in self.price_data or not self.price_data[symbol]:
                return "NEUTRAL"
                
            # VWAP 데이터 있는지 확인
            vwap_data = self.vwap_data.get(symbol, {})
            if not vwap_data.get('vwap') or not vwap_data.get('upper_band') or not vwap_data.get('lower_band'):
                return "NEUTRAL"
            
            # 현재가와 이전가
            if len(self.price_data[symbol]) < 2:
                return "NEUTRAL"
                
            current_price = self.price_data[symbol][-1]["price"]
            prev_price = self.price_data[symbol][-2]["price"]
            
            vwap = vwap_data['vwap']
            upper_band = vwap_data['upper_band']
            lower_band = vwap_data['lower_band']
            entry_threshold = self.params['entry_threshold']
            
            # 매수 신호
            if (prev_price < vwap and current_price > vwap * (1 + entry_threshold)) or \
               (current_price < lower_band * (1 + entry_threshold) and current_price > prev_price):
                return "BUY"
            
            # 매도 신호
            if (prev_price > vwap and current_price < vwap * (1 - entry_threshold)) or \
               (current_price > upper_band * (1 - entry_threshold) and current_price < prev_price):
                return "SELL"
            
            # 가격이 VWAP보다 상당히 높고 상승 중인 경우 - 매수 편향
            if current_price > vwap * 1.01 and current_price > prev_price:
                return "BUY"
            
            # 가격이 VWAP보다 상당히 낮고 하락 중인 경우 - 매도 편향
            if current_price < vwap * 0.99 and current_price < prev_price:
                return "SELL"
            
            return "NEUTRAL"
            
        except Exception:
            return "NEUTRAL"

# 싱글톤 인스턴스
vwap_strategy = VWAPStrategy() 