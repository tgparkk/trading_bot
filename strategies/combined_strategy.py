"""
통합 트레이딩 전략 (Combined Trading Strategy)
여러 전략의 신호를 결합하여 강도와 방향성을 종합적으로 분석하는 전략
"""
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, time, timedelta
from collections import deque
import numpy as np

from config.settings import config
from core.api_client import api_client
from core.websocket_client import ws_client
from core.order_manager import order_manager
from utils.logger import logger
from monitoring.alert_system import alert_system

# 개별 전략 임포트
from strategies.breakout_strategy import breakout_strategy
from strategies.momentum_strategy import momentum_strategy
from strategies.gap_strategy import gap_strategy
from strategies.vwap_strategy import vwap_strategy
from strategies.volume_spike_strategy import volume_strategy


class CombinedStrategy:
    """통합 트레이딩 전략 클래스"""
    
    def __init__(self):
        # get 메서드 대신 직접 속성 접근 또는 기본값 설정
        self.params = {
            # 각 전략별 가중치 (0~1 사이 값, 합계 1.0)
            "breakout_weight": 0.2,     # 브레이크아웃 전략 가중치
            "momentum_weight": 0.25,    # 모멘텀 전략 가중치
            "gap_weight": 0.2,          # 갭 트레이딩 전략 가중치
            "vwap_weight": 0.2,         # VWAP 전략 가중치
            "volume_weight": 0.15,      # 볼륨 스파이크 전략 가중치
            
            # 매매 신호 기준
            "buy_threshold": 6.0,       # 매수 신호 임계값 (0~10)
            "sell_threshold": 6.0,      # 매도 신호 임계값 (0~10)
            "min_agreement": 2,         # 최소 몇 개 전략이 일치해야 하는지
            
            # 포지션 관리
            "stop_loss_pct": 0.015,     # 손절 비율 (1.5%)
            "take_profit_pct": 0.025,   # 익절 비율 (2.5%)
            "max_positions": 5,         # 최대 포지션 개수
            "position_size": 1000000,   # 기본 포지션 크기 (100만원)
            "trailing_stop": True,      # 트레일링 스탑 사용 여부
            "trailing_pct": 0.005       # 트레일링 스탑 비율 (0.5%)
        }
        
        # 설정에 combined_params가 있으면 업데이트
        if hasattr(config["trading"], "combined_params"):
            self.params.update(config["trading"].combined_params)
            
        self.running = False
        self.paused = False
        self.watched_symbols = set()
        self.positions = {}             # {position_id: position_data}
        self.signals = {}               # {symbol: {'score': float, 'direction': str, 'strategies': {}}}
        self.price_data = {}            # {symbol: deque of price data}
        
        # 가중치 정규화
        self._normalize_weights()
        
    def _normalize_weights(self):
        """가중치 합이 1이 되도록 정규화"""
        weights = [
            self.params["breakout_weight"],
            self.params["momentum_weight"],
            self.params["gap_weight"],
            self.params["vwap_weight"],
            self.params["volume_weight"]
        ]
        
        total = sum(weights)
        if total > 0:
            self.params["breakout_weight"] /= total
            self.params["momentum_weight"] /= total
            self.params["gap_weight"] /= total
            self.params["vwap_weight"] /= total
            self.params["volume_weight"] /= total
        
    async def start(self, symbols: List[str]):
        """전략 시작"""
        try:
            self.running = True
            self.paused = False
            self.watched_symbols = set(symbols)
            
            # 각 종목별 데이터 초기화
            for symbol in symbols:
                self.price_data[symbol] = deque(maxlen=100)  # 최근 가격 데이터
                self.signals[symbol] = {
                    'score': 0,
                    'direction': "NEUTRAL",
                    'strategies': {
                        'breakout': {'signal': 0, 'direction': "NEUTRAL"},
                        'momentum': {'signal': 0, 'direction': "NEUTRAL"},
                        'gap': {'signal': 0, 'direction': "NEUTRAL"},
                        'vwap': {'signal': 0, 'direction': "NEUTRAL"},
                        'volume': {'signal': 0, 'direction': "NEUTRAL"}
                    },
                    'last_update': None
                }
                
                # 웹소켓 구독
                await ws_client.subscribe_price(symbol, self._handle_price_update)
            
            # 개별 전략 시작
            await self._start_individual_strategies(symbols)
            
            logger.log_system(f"Combined strategy started for {len(symbols)} symbols")
            
            # 전략 실행 루프
            asyncio.create_task(self._strategy_loop())
            
        except Exception as e:
            logger.log_error(e, "Failed to start combined strategy")
            await alert_system.notify_error(e, "Combined strategy start error")
    
    async def _start_individual_strategies(self, symbols: List[str]):
        """개별 전략 시작"""
        try:
            # 각 전략 시작
            await breakout_strategy.start(symbols)
            await momentum_strategy.start(symbols)
            await gap_strategy.start(symbols)
            await vwap_strategy.start(symbols)
            await volume_strategy.start(symbols)
            
            logger.log_system("All individual strategies started")
            
        except Exception as e:
            logger.log_error(e, "Failed to start individual strategies")
            await alert_system.notify_error(e, "Individual strategies start error")
    
    async def stop(self):
        """전략 중지"""
        self.running = False
        
        # 웹소켓 구독 해제
        for symbol in self.watched_symbols:
            await ws_client.unsubscribe(symbol, "price")
        
        # 각 전략 중지
        await breakout_strategy.stop()
        await momentum_strategy.stop()
        await gap_strategy.stop()
        await vwap_strategy.stop()
        await volume_strategy.stop()
        
        logger.log_system("Combined strategy stopped")
    
    async def pause(self):
        """전략 일시 중지"""
        if not self.paused:
            self.paused = True
            
            # 개별 전략도 일시 중지
            await breakout_strategy.pause()
            await momentum_strategy.pause()
            await gap_strategy.pause()
            await vwap_strategy.pause()
            await volume_strategy.pause()
            
            logger.log_system("Combined strategy paused")
        return True

    async def resume(self):
        """전략 재개"""
        if self.paused:
            self.paused = False
            
            # 개별 전략도 재개
            await breakout_strategy.resume()
            await momentum_strategy.resume()
            await gap_strategy.resume()
            await vwap_strategy.resume()
            await volume_strategy.resume()
            
            logger.log_system("Combined strategy resumed")
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
                
                # 주기적으로 신호 업데이트
                last_update = self.signals[symbol].get('last_update')
                if last_update is None or (timestamp - last_update).total_seconds() >= 10:
                    await self._update_signals(symbol)
                    self.signals[symbol]['last_update'] = timestamp
                
        except Exception as e:
            logger.log_error(e, "Error handling price update in combined strategy")
    
    async def _update_signals(self, symbol: str):
        """각 전략별 신호 업데이트"""
        try:
            # 각 전략별 신호 강도와 방향 가져오기
            breakout_signal = breakout_strategy.get_signal_strength(symbol)
            breakout_direction = breakout_strategy.get_signal_direction(symbol)
            
            momentum_signal = momentum_strategy.get_signal_strength(symbol)
            momentum_direction = momentum_strategy.get_signal_direction(symbol)
            
            gap_signal = gap_strategy.get_signal_strength(symbol)
            gap_direction = gap_strategy.get_signal_direction(symbol)
            
            vwap_signal = vwap_strategy.get_signal_strength(symbol)
            vwap_direction = vwap_strategy.get_signal_direction(symbol)
            
            volume_signal = volume_strategy.get_signal_strength(symbol)
            volume_direction = volume_strategy.get_signal_direction(symbol)
            
            # 신호 저장
            self.signals[symbol]['strategies'] = {
                'breakout': {'signal': breakout_signal, 'direction': breakout_direction},
                'momentum': {'signal': momentum_signal, 'direction': momentum_direction},
                'gap': {'signal': gap_signal, 'direction': gap_direction},
                'vwap': {'signal': vwap_signal, 'direction': vwap_direction},
                'volume': {'signal': volume_signal, 'direction': volume_direction}
            }
            
            # 종합 점수 및 방향 계산
            score, direction, agreements = self._calculate_combined_signal(symbol)
            
            self.signals[symbol]['score'] = score
            self.signals[symbol]['direction'] = direction
            self.signals[symbol]['agreements'] = agreements
            
            # 로그 (신호 강도가 충분히 강한 경우에만)
            if score >= 4.0:
                logger.log_system(
                    f"Combined signal for {symbol}: {direction} (Score: {score:.2f}, "
                    f"Agreements: {agreements['BUY']}/{agreements['SELL']})"
                )
            
        except Exception as e:
            logger.log_error(e, f"Error updating signals for {symbol}")
    
    def _calculate_combined_signal(self, symbol: str) -> Tuple[float, str, Dict[str, int]]:
        """개별 신호를 종합하여 최종 신호 강도와 방향 계산"""
        strategies = self.signals[symbol]['strategies']
        
        # 가중 점수 계산
        breakout_score = strategies['breakout']['signal'] * self.params["breakout_weight"]
        momentum_score = strategies['momentum']['signal'] * self.params["momentum_weight"]
        gap_score = strategies['gap']['signal'] * self.params["gap_weight"]
        vwap_score = strategies['vwap']['signal'] * self.params["vwap_weight"]
        volume_score = strategies['volume']['signal'] * self.params["volume_weight"]
        
        # 종합 점수 (0-10)
        total_score = breakout_score + momentum_score + gap_score + vwap_score + volume_score
        
        # 방향성 투표
        buy_votes = 0
        sell_votes = 0
        
        # 각 전략별로 매수/매도 의견 카운트
        if strategies['breakout']['direction'] == "BUY":
            buy_votes += 1
        elif strategies['breakout']['direction'] == "SELL":
            sell_votes += 1
            
        if strategies['momentum']['direction'] == "BUY":
            buy_votes += 1
        elif strategies['momentum']['direction'] == "SELL":
            sell_votes += 1
            
        if strategies['gap']['direction'] == "BUY":
            buy_votes += 1
        elif strategies['gap']['direction'] == "SELL":
            sell_votes += 1
            
        if strategies['vwap']['direction'] == "BUY":
            buy_votes += 1
        elif strategies['vwap']['direction'] == "SELL":
            sell_votes += 1
            
        if strategies['volume']['direction'] == "BUY":
            buy_votes += 1
        elif strategies['volume']['direction'] == "SELL":
            sell_votes += 1
        
        # 최종 신호 방향 결정
        direction = "NEUTRAL"
        if buy_votes >= self.params["min_agreement"] and buy_votes > sell_votes:
            direction = "BUY"
        elif sell_votes >= self.params["min_agreement"] and sell_votes > buy_votes:
            direction = "SELL"
        
        # 매수/매도 의견 수가 같으면 신호 강도로 결정
        if buy_votes == sell_votes and buy_votes >= self.params["min_agreement"]:
            # 각 매수/매도 신호의 총 강도 계산
            buy_strength = 0
            sell_strength = 0
            
            if strategies['breakout']['direction'] == "BUY":
                buy_strength += strategies['breakout']['signal'] * self.params["breakout_weight"]
            elif strategies['breakout']['direction'] == "SELL":
                sell_strength += strategies['breakout']['signal'] * self.params["breakout_weight"]
                
            if strategies['momentum']['direction'] == "BUY":
                buy_strength += strategies['momentum']['signal'] * self.params["momentum_weight"]
            elif strategies['momentum']['direction'] == "SELL":
                sell_strength += strategies['momentum']['signal'] * self.params["momentum_weight"]
                
            if strategies['gap']['direction'] == "BUY":
                buy_strength += strategies['gap']['signal'] * self.params["gap_weight"]
            elif strategies['gap']['direction'] == "SELL":
                sell_strength += strategies['gap']['signal'] * self.params["gap_weight"]
                
            if strategies['vwap']['direction'] == "BUY":
                buy_strength += strategies['vwap']['signal'] * self.params["vwap_weight"]
            elif strategies['vwap']['direction'] == "SELL":
                sell_strength += strategies['vwap']['signal'] * self.params["vwap_weight"]
                
            if strategies['volume']['direction'] == "BUY":
                buy_strength += strategies['volume']['signal'] * self.params["volume_weight"]
            elif strategies['volume']['direction'] == "SELL":
                sell_strength += strategies['volume']['signal'] * self.params["volume_weight"]
            
            # 강도가 더 높은 쪽으로 결정
            if buy_strength > sell_strength:
                direction = "BUY"
            elif sell_strength > buy_strength:
                direction = "SELL"
        
        agreements = {"BUY": buy_votes, "SELL": sell_votes}
        return total_score, direction, agreements
    
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
                
                # 거래 신호 확인 및 실행
                for symbol in self.watched_symbols:
                    await self._check_and_trade(symbol)
                
                # 포지션 모니터링
                await self._monitor_positions()
                
                await asyncio.sleep(1)  # 1초 대기
                
            except Exception as e:
                logger.log_error(e, "Combined strategy loop error")
                await asyncio.sleep(5)  # 에러 시 5초 대기
    
    async def _check_and_trade(self, symbol: str):
        """신호에 따른 포지션 진입 확인"""
        try:
            # 이미 포지션 있는지 확인
            symbol_positions = self._get_symbol_positions(symbol)
            if len(symbol_positions) >= self.params["max_positions"]:
                return
            
            # 현재가
            if not self.price_data[symbol]:
                return
                
            current_price = self.price_data[symbol][-1]["price"]
            
            # 신호 계산
            score, direction, agreements = self._calculate_combined_signal(symbol)
            
            # 매수 신호
            if direction == "BUY" and score >= self.params["buy_threshold"]:
                await self._enter_position(symbol, "BUY", current_price, score, agreements)
            
            # 매도 신호
            elif direction == "SELL" and score >= self.params["sell_threshold"]:
                await self._enter_position(symbol, "SELL", current_price, score, agreements)
                
        except Exception as e:
            logger.log_error(e, f"Error checking trade signals for {symbol}")
    
    def _get_symbol_positions(self, symbol: str) -> List[str]:
        """특정 종목의 포지션 ID 목록 반환"""
        return [
            position_id for position_id, position in self.positions.items()
            if position["symbol"] == symbol
        ]
    
    async def _enter_position(self, symbol: str, side: str, price: float, 
                           score: float, agreements: Dict[str, int]):
        """포지션 진입"""
        try:
            # 주문 수량 계산
            position_size = self.params["position_size"]  # 100만원
            quantity = int(position_size / price)
            
            if quantity <= 0:
                return
            
            # 주문 실행
            result = await order_manager.place_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type="MARKET",
                strategy="combined",
                reason=f"combined_signal_{score:.1f}"
            )
            
            if result["status"] == "success":
                # 손절/익절 가격 계산
                stop_loss_pct = self.params["stop_loss_pct"]
                take_profit_pct = self.params["take_profit_pct"]
                
                if side == "BUY":
                    stop_price = price * (1 - stop_loss_pct)
                    target_price = price * (1 + take_profit_pct)
                else:  # SELL
                    stop_price = price * (1 + stop_loss_pct)
                    target_price = price * (1 - take_profit_pct)
                
                # 포지션 저장
                position_id = result.get("order_id", str(datetime.now().timestamp()))
                self.positions[position_id] = {
                    "symbol": symbol,
                    "entry_price": price,
                    "entry_time": datetime.now(),
                    "side": side,
                    "quantity": quantity,
                    "stop_price": stop_price,
                    "target_price": target_price,
                    "score": score,
                    "agreements": agreements,
                    "original_stop": stop_price,  # 트레일링 스탑용
                    "highest_price": price if side == "BUY" else None,
                    "lowest_price": price if side == "SELL" else None
                }
                
                logger.log_system(
                    f"Combined: Entered {side} position for {symbol} at {price}, "
                    f"score: {score:.1f}, agreements: {sum(agreements.values())}, "
                    f"stop: {stop_price}, target: {target_price}"
                )
                
        except Exception as e:
            logger.log_error(e, f"Combined strategy entry error for {symbol}")
    
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
                
                # 트레일링 스탑 업데이트
                if self.params["trailing_stop"]:
                    self._update_trailing_stop(position, current_price)
                
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
                
                # 시간 제한 (최대 2시간)
                hold_time = (datetime.now() - entry_time).total_seconds() / 60
                if hold_time >= 120:  # 2시간
                    should_exit = True
                    exit_reason = "time_expired"
                
                # 시그널 변화 확인 (방향 반전 시 청산)
                signal = self.signals.get(symbol, {})
                current_direction = signal.get('direction', 'NEUTRAL')
                
                if current_direction != "NEUTRAL" and current_direction != side:
                    # 시그널 강도가 충분히 강한 경우에만
                    if signal.get('score', 0) >= self.params[f"{current_direction.lower()}_threshold"]:
                        should_exit = True
                        exit_reason = "signal_reversal"
                
                # 청산 실행
                if should_exit:
                    await self._exit_position(position_id, exit_reason)
                    
        except Exception as e:
            logger.log_error(e, "Combined position monitoring error")
    
    def _update_trailing_stop(self, position: Dict[str, Any], current_price: float):
        """트레일링 스탑 업데이트"""
        try:
            if not self.params["trailing_stop"]:
                return
                
            side = position["side"]
            initial_stop = position["original_stop"]
            trailing_pct = self.params["trailing_pct"]
            
            if side == "BUY":
                # 매수 포지션일 경우 현재가가 진입가보다 상승했다면 손절가 상향 조정
                new_stop = current_price * (1 - trailing_pct)
                if new_stop > position["stop_price"] and new_stop > initial_stop:
                    position["stop_price"] = new_stop
                    
            else:  # SELL
                # 매도 포지션일 경우 현재가가 진입가보다 하락했다면 손절가 하향 조정
                new_stop = current_price * (1 + trailing_pct)
                if new_stop < position["stop_price"] and new_stop < initial_stop:
                    position["stop_price"] = new_stop
                    
        except Exception as e:
            logger.log_error(e, "Error updating trailing stop")
    
    async def _exit_position(self, position_id: str, reason: str):
        """포지션 청산"""
        try:
            position = self.positions[position_id]
            symbol = position["symbol"]
            side = position["side"]
            exit_side = "SELL" if side == "BUY" else "BUY"
            
            result = await order_manager.place_order(
                symbol=symbol,
                side=exit_side,
                quantity=position["quantity"],
                order_type="MARKET",
                strategy="combined",
                reason=reason
            )
            
            if result["status"] == "success":
                # 손익 계산
                entry_price = position["entry_price"]
                exit_price = self.price_data[symbol][-1]["price"]
                pnl_pct = 0
                
                if side == "BUY":
                    pnl_pct = (exit_price - entry_price) / entry_price * 100
                else:
                    pnl_pct = (entry_price - exit_price) / entry_price * 100
                
                # 청산 로그
                logger.log_system(
                    f"통합 전략: {symbol} {side} 포지션 청산 (진입: {entry_price:,.0f}, "
                    f"청산: {exit_price:,.0f}, 손익: {pnl_pct:.2f}%, 사유: {reason})"
                )
                
                # 포지션 제거
                del self.positions[position_id]
                
        except Exception as e:
            logger.log_error(e, f"Combined exit error for position {position_id}")
    
    def get_strategy_status(self, symbol: str = None) -> Dict:
        """전략 상태 정보 반환"""
        try:
            result = {
                "running": self.running,
                "paused": self.paused,
                "symbols": len(self.watched_symbols),
                "positions": len(self.positions),
                "position_details": {},
                "signals": {}
            }
            
            # 포지션 정보
            for pos_id, pos in self.positions.items():
                symbol = pos["symbol"]
                result["position_details"][pos_id] = {
                    "symbol": symbol,
                    "side": pos["side"],
                    "entry_price": pos["entry_price"],
                    "stop_price": pos["stop_price"],
                    "target_price": pos["target_price"],
                    "entry_time": pos["entry_time"].strftime("%H:%M:%S"),
                    "hold_time": (datetime.now() - pos["entry_time"]).total_seconds() / 60
                }
            
            # 특정 심볼에 대한 상세 정보 요청인 경우
            if symbol and symbol in self.signals:
                result["signals"][symbol] = {
                    "score": self.signals[symbol]["score"],
                    "direction": self.signals[symbol]["direction"],
                    "agreements": self.signals[symbol].get("agreements", {}),
                    "strategies": self.signals[symbol]["strategies"]
                }
            # 아니면 모든 심볼의 요약 정보
            elif not symbol:
                for sym in self.watched_symbols:
                    if sym in self.signals:
                        result["signals"][sym] = {
                            "score": self.signals[sym]["score"],
                            "direction": self.signals[sym]["direction"],
                            "agreements": self.signals[sym].get("agreements", {})
                        }
            
            return result
            
        except Exception as e:
            logger.log_error(e, "Error getting strategy status")
            return {"error": str(e)}
            
    async def update_symbols(self, new_symbols: List[str]):
        """관심 종목 업데이트"""
        try:
            # 새로운 종목 집합
            new_set = set(new_symbols)
            
            # 구독 해제할 종목들 (기존에 있던 종목 중 새로운 목록에 없는 것)
            to_unsubscribe = self.watched_symbols - new_set
            
            # 새로 구독할 종목들 (새로운 목록에 있던 종목 중 기존에 없던 것)
            to_subscribe = new_set - self.watched_symbols
            
            # 구독 해제
            for symbol in to_unsubscribe:
                await ws_client.unsubscribe(symbol, "price")
                if symbol in self.price_data:
                    del self.price_data[symbol]
                if symbol in self.signals:
                    del self.signals[symbol]
            
            # 새로 구독
            for symbol in to_subscribe:
                self.price_data[symbol] = deque(maxlen=100)
                self.signals[symbol] = {
                    'score': 0,
                    'direction': "NEUTRAL",
                    'strategies': {
                        'breakout': {'signal': 0, 'direction': "NEUTRAL"},
                        'momentum': {'signal': 0, 'direction': "NEUTRAL"},
                        'gap': {'signal': 0, 'direction': "NEUTRAL"},
                        'vwap': {'signal': 0, 'direction': "NEUTRAL"},
                        'volume': {'signal': 0, 'direction': "NEUTRAL"}
                    },
                    'last_update': None
                }
                await ws_client.subscribe_price(symbol, self._handle_price_update)
            
            # 개별 전략 업데이트
            await breakout_strategy.update_symbols(new_symbols)
            await momentum_strategy.update_symbols(new_symbols)
            await gap_strategy.update_symbols(new_symbols)
            await vwap_strategy.update_symbols(new_symbols)
            await volume_strategy.update_symbols(new_symbols)
            
            # 관심 종목 업데이트
            self.watched_symbols = new_set
            
            logger.log_system(f"Updated watched symbols in combined strategy: {len(self.watched_symbols)}")
            
        except Exception as e:
            logger.log_error(e, "Failed to update symbols in combined strategy")
            await alert_system.notify_error(e, "Symbol update error in combined strategy")

# 싱글톤 인스턴스
combined_strategy = CombinedStrategy()

async def pause():
    """모듈 레벨에서 전략 일시 중지"""
    return await combined_strategy.pause()

async def resume():
    """모듈 레벨에서 전략 재개"""
    return await combined_strategy.resume() 