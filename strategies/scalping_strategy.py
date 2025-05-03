"""
초단기 스캘핑 전략
"""
import asyncio
from typing import Dict, Any, List
from datetime import datetime, timedelta
from collections import deque
from config.settings import config
from core.api_client import api_client
from core.websocket_client import ws_client
from core.order_manager import order_manager
from utils.logger import logger
from monitoring.alert_system import alert_system

class ScalpingStrategy:
    """초단기 스캘핑 전략"""
    
    def __init__(self):
        self.params = config["trading"].scalping_params
        self.running = False
        self.watched_symbols = set()
        self.price_data = {}  # {symbol: deque of tick data}
        self.volume_data = {}  # {symbol: deque of volume data}
        self.positions = {}  # {symbol: position_data}
        
    async def start(self, symbols: List[str]):
        """전략 시작"""
        try:
            self.running = True
            self.watched_symbols = set(symbols)
            
            # 각 종목별 데이터 초기화
            for symbol in symbols:
                self.price_data[symbol] = deque(maxlen=self.params["tick_window"])
                self.volume_data[symbol] = deque(maxlen=self.params["tick_window"])
                
                # 웹소켓 구독
                await ws_client.subscribe_price(symbol, self._handle_price_update)
                await ws_client.subscribe_orderbook(symbol, self._handle_orderbook_update)
            
            logger.log_system(f"Scalping strategy started for {len(symbols)} symbols")
            
            # 전략 실행 루프
            asyncio.create_task(self._strategy_loop())
            
        except Exception as e:
            logger.log_error(e, "Failed to start scalping strategy")
            await alert_system.notify_error(e, "Scalping strategy start error")
    
    async def stop(self):
        """전략 중지"""
        self.running = False
        
        # 웹소켓 구독 해제
        for symbol in self.watched_symbols:
            await ws_client.unsubscribe(symbol, "price")
            await ws_client.unsubscribe(symbol, "orderbook")
        
        logger.log_system("Scalping strategy stopped")
    
    async def _handle_price_update(self, data: Dict[str, Any]):
        """실시간 체결가 업데이트 처리"""
        try:
            symbol = data.get("tr_key")
            price = float(data.get("stck_prpr", 0))
            volume = int(data.get("cntg_vol", 0))
            
            if symbol in self.price_data:
                self.price_data[symbol].append({
                    "price": price,
                    "volume": volume,
                    "timestamp": datetime.now()
                })
                
        except Exception as e:
            logger.log_error(e, "Error handling price update")
    
    async def _handle_orderbook_update(self, data: Dict[str, Any]):
        """실시간 호가 업데이트 처리"""
        try:
            symbol = data.get("tr_key")
            
            # 호가 데이터 분석
            total_bid_volume = sum([
                int(data.get(f"bidp_rsqn{i}", 0)) 
                for i in range(1, 11)
            ])
            
            total_ask_volume = sum([
                int(data.get(f"askp_rsqn{i}", 0)) 
                for i in range(1, 11)
            ])
            
            if symbol in self.volume_data:
                self.volume_data[symbol].append({
                    "bid_volume": total_bid_volume,
                    "ask_volume": total_ask_volume,
                    "ratio": total_bid_volume / total_ask_volume if total_ask_volume > 0 else 0,
                    "timestamp": datetime.now()
                })
                
        except Exception as e:
            logger.log_error(e, "Error handling orderbook update")
    
    async def _strategy_loop(self):
        """전략 실행 루프"""
        while self.running:
            try:
                for symbol in self.watched_symbols:
                    await self._analyze_and_trade(symbol)
                
                # 포지션 모니터링
                await self._monitor_positions()
                
                await asyncio.sleep(1)  # 1초 대기
                
            except Exception as e:
                logger.log_error(e, "Strategy loop error")
                await asyncio.sleep(5)  # 에러 시 5초 대기
    
    async def _analyze_and_trade(self, symbol: str):
        """종목 분석 및 거래"""
        try:
            # 데이터 충분한지 확인
            if len(self.price_data[symbol]) < self.params["tick_window"]:
                return
            
            # 가격 변동성 분석
            price_volatility = self._calculate_volatility(symbol)
            
            # 거래량 급증 감지
            volume_surge = self._detect_volume_surge(symbol)
            
            # 모멘텀 분석
            momentum = self._calculate_momentum(symbol)
            
            # 진입 조건 체크
            if await self._check_entry_conditions(symbol, price_volatility, 
                                                volume_surge, momentum):
                await self._enter_position(symbol)
            
            # 청산 조건 체크
            if symbol in self.positions:
                await self._check_exit_conditions(symbol)
                
        except Exception as e:
            logger.log_error(e, f"Analysis error for {symbol}")
    
    def _calculate_volatility(self, symbol: str) -> float:
        """변동성 계산"""
        prices = [tick["price"] for tick in self.price_data[symbol]]
        if len(prices) < 2:
            return 0
        
        price_changes = []
        for i in range(1, len(prices)):
            change = (prices[i] - prices[i-1]) / prices[i-1]
            price_changes.append(abs(change))
        
        return sum(price_changes) / len(price_changes) if price_changes else 0
    
    def _detect_volume_surge(self, symbol: str) -> float:
        """거래량 급증 감지"""
        volumes = [tick["volume"] for tick in self.price_data[symbol]]
        if len(volumes) < self.params["tick_window"]:
            return 0
        
        recent_volume = sum(volumes[-5:]) / 5  # 최근 5틱 평균
        average_volume = sum(volumes) / len(volumes)  # 전체 평균
        
        return recent_volume / average_volume if average_volume > 0 else 0
    
    def _calculate_momentum(self, symbol: str) -> float:
        """모멘텀 계산"""
        prices = [tick["price"] for tick in self.price_data[symbol]]
        if len(prices) < 2:
            return 0
        
        return (prices[-1] - prices[0]) / prices[0]
    
    async def _check_entry_conditions(self, symbol: str, volatility: float, 
                                    volume_surge: float, momentum: float) -> bool:
        """진입 조건 확인"""
        # 이미 포지션이 있으면 스킵
        if symbol in self.positions:
            return False
        
        # 변동성이 임계값 이상
        if volatility < self.params["price_change_threshold"]:
            return False
        
        # 거래량 급증
        if volume_surge < self.params["volume_multiplier"]:
            return False
        
        # 모멘텀 체크
        if abs(momentum) < self.params["price_change_threshold"]:
            return False
        
        # 매수/매도 신호
        buy_signal = momentum > 0 and volume_surge > self.params["volume_multiplier"]
        sell_signal = momentum < 0 and volume_surge > self.params["volume_multiplier"]
        
        return buy_signal or sell_signal
    
    async def _enter_position(self, symbol: str):
        """포지션 진입"""
        try:
            # 현재가 조회
            current_price = self.price_data[symbol][-1]["price"]
            
            # 모멘텀 방향 확인
            momentum = self._calculate_momentum(symbol)
            side = "BUY" if momentum > 0 else "SELL"
            
            # 주문 수량 계산
            position_value = self.params.get("position_size", 1000000)  # 100만원
            quantity = int(position_value / current_price)
            
            # 주문 실행
            result = await order_manager.place_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type="MARKET",
                strategy="scalping",
                reason=f"momentum_{momentum:.4f}_volume_{self._detect_volume_surge(symbol):.2f}"
            )
            
            if result["status"] == "success":
                self.positions[symbol] = {
                    "entry_price": current_price,
                    "entry_time": datetime.now(),
                    "side": side,
                    "quantity": quantity
                }
                
                logger.log_system(
                    f"Entered {side} position for {symbol} at {current_price}"
                )
                
        except Exception as e:
            logger.log_error(e, f"Entry error for {symbol}")
    
    async def _check_exit_conditions(self, symbol: str):
        """청산 조건 확인"""
        try:
            position = self.positions[symbol]
            current_price = self.price_data[symbol][-1]["price"]
            
            # 수익률 계산
            if position["side"] == "BUY":
                pnl_rate = (current_price - position["entry_price"]) / position["entry_price"]
            else:
                pnl_rate = (position["entry_price"] - current_price) / position["entry_price"]
            
            # 시간 경과
            holding_time = (datetime.now() - position["entry_time"]).total_seconds()
            
            # 청산 조건
            should_exit = False
            exit_reason = ""
            
            # 손절
            if pnl_rate <= -self.params["stop_loss"]:
                should_exit = True
                exit_reason = "stop_loss"
            
            # 익절
            elif pnl_rate >= self.params["take_profit"]:
                should_exit = True
                exit_reason = "take_profit"
            
            # 시간 만료
            elif holding_time >= self.params["hold_time"]:
                should_exit = True
                exit_reason = "time_exit"
            
            # 청산 실행
            if should_exit:
                await self._exit_position(symbol, exit_reason)
                
        except Exception as e:
            logger.log_error(e, f"Exit check error for {symbol}")
    
    async def _exit_position(self, symbol: str, reason: str):
        """포지션 청산"""
        try:
            position = self.positions[symbol]
            exit_side = "SELL" if position["side"] == "BUY" else "BUY"
            
            result = await order_manager.place_order(
                symbol=symbol,
                side=exit_side,
                quantity=position["quantity"],
                order_type="MARKET",
                strategy="scalping",
                reason=reason
            )
            
            if result["status"] == "success":
                del self.positions[symbol]
                logger.log_system(
                    f"Exited position for {symbol}, reason: {reason}"
                )
                
        except Exception as e:
            logger.log_error(e, f"Exit error for {symbol}")
    
    async def _monitor_positions(self):
        """포지션 모니터링"""
        try:
            for symbol, position in list(self.positions.items()):
                # 현재 가격 확인
                if symbol not in self.price_data or not self.price_data[symbol]:
                    continue
                
                current_price = self.price_data[symbol][-1]["price"]
                
                # 수익률 계산
                if position["side"] == "BUY":
                    pnl_rate = (current_price - position["entry_price"]) / position["entry_price"]
                else:
                    pnl_rate = (position["entry_price"] - current_price) / position["entry_price"]
                
                # 급락/급등 시 긴급 청산
                if abs(pnl_rate) > 0.05:  # 5% 이상 변동
                    await self._exit_position(symbol, "emergency_exit")
                    await alert_system.notify_large_movement(
                        symbol, pnl_rate, self._detect_volume_surge(symbol)
                    )
                    
        except Exception as e:
            logger.log_error(e, "Position monitoring error")

# 싱글톤 인스턴스
scalping_strategy = ScalpingStrategy()
