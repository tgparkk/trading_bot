"""
통합 트레이딩 전략 (Combined Trading Strategy)
여러 전략의 신호를 결합하여 강도와 방향성을 종합적으로 분석하는 전략
"""
import asyncio
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, time, timedelta
from collections import deque
import numpy as np
import os

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
            "buy_threshold": 5.0,       # 매수 신호 임계값 (0~10) - 5.0으로 낮춤 (원래 6.0)
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
            if score >= 3.0:
                logger.log_system(
                    f"Combined signal for {symbol}: {direction} (Score: {score:.2f}, "
                    f"Agreements: {agreements['BUY']}/{agreements['SELL']})"
                )
                
                # trade.log에도 신호 기록
                logger.log_trade(
                    action="SIGNAL",
                    symbol=symbol,
                    price=0,
                    quantity=0,
                    reason=f"{direction} 신호 강도: {score:.1f}/10.0",
                    score=f"{score:.1f}",
                    direction=direction,
                    buy_agreements=agreements['BUY'],
                    sell_agreements=agreements['SELL'],
                    time=datetime.now().strftime("%H:%M:%S")
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
            
            # 매수/매도 기준값
            buy_threshold = self.params["buy_threshold"]
            sell_threshold = self.params["sell_threshold"]
            min_agreement = self.params["min_agreement"]
            
            # 매수/매도 판단 정보 trade.log에 자세히 기록
            strategies = self.signals[symbol]['strategies']
            
            # 평가 상태 결정
            evaluation_status = "NEUTRAL"
            failure_reason = []
            
            if direction == "BUY":
                if score >= buy_threshold:
                    evaluation_status = "BUY_SIGNAL"
                else:
                    evaluation_status = "BUY_THRESHOLD_FAIL"
                    failure_reason.append(f"매수점수 미달 (목표: {buy_threshold:.1f}, 실제: {score:.1f})")
            elif direction == "SELL":
                if score >= sell_threshold:
                    evaluation_status = "SELL_SIGNAL"
                else:
                    evaluation_status = "SELL_THRESHOLD_FAIL"
                    failure_reason.append(f"매도점수 미달 (목표: {sell_threshold:.1f}, 실제: {score:.1f})")
            else:  # NEUTRAL
                evaluation_status = "NEUTRAL"
                buy_count = agreements.get("BUY", 0)
                sell_count = agreements.get("SELL", 0)
                failure_reason.append(f"방향성 불명확 (매수동의: {buy_count}, 매도동의: {sell_count}, 최소필요: {min_agreement})")
            
            # 매수/매도/중립/점수미달 등 모든 상태에 대해 상세 로깅
            logger.log_trade(
                action="TRADE_EVALUATION",
                symbol=symbol,
                price=current_price,
                quantity=0,
                reason=f"{evaluation_status} - 점수: {score:.1f}/10.0, 임계값(매수:{buy_threshold}/매도:{sell_threshold}), 방향: {direction}",
                score=f"{score:.1f}",
                buy_threshold=f"{buy_threshold}",
                sell_threshold=f"{sell_threshold}",
                direction=direction,
                status=evaluation_status,
                failure_reasons=", ".join(failure_reason) if failure_reason else "",
                breakout=f"{strategies['breakout']['signal']:.1f}({strategies['breakout']['direction']})",
                momentum=f"{strategies['momentum']['signal']:.1f}({strategies['momentum']['direction']})",
                gap=f"{strategies['gap']['signal']:.1f}({strategies['gap']['direction']})",
                vwap=f"{strategies['vwap']['signal']:.1f}({strategies['vwap']['direction']})",
                volume=f"{strategies['volume']['signal']:.1f}({strategies['volume']['direction']})",
                buy_agreements=agreements.get('BUY', 0),
                sell_agreements=agreements.get('SELL', 0),
                time=datetime.now().strftime("%H:%M:%S")
            )
            
            # 매수 신호
            if direction == "BUY" and score >= buy_threshold:
                logger.log_system(f"[BUY_SIGNAL] {symbol} - 점수 {score:.1f} >= 임계값 {buy_threshold} - 매수 신호 감지!")
                await self._enter_position(symbol, "BUY", current_price, score, agreements)
            
            # 매도 신호
            elif direction == "SELL" and score >= sell_threshold:
                logger.log_system(f"[SELL_SIGNAL] {symbol} - 점수 {score:.1f} >= 임계값 {sell_threshold} - 매도 신호 감지!")
                await self._enter_position(symbol, "SELL", current_price, score, agreements)
            
            # 매수/매도 조건 미충족 이유 로그 기록
            else:
                failure_reason = []
                
                # 점수 미달 여부 체크
                if direction == "BUY" and score < buy_threshold:
                    failure_reason.append(f"매수점수 미달 (목표: {buy_threshold:.1f}, 실제: {score:.1f})")
                elif direction == "SELL" and score < sell_threshold:
                    failure_reason.append(f"매도점수 미달 (목표: {sell_threshold:.1f}, 실제: {score:.1f})")
                
                # 방향성 중립 체크
                if direction == "NEUTRAL":
                    buy_count = agreements.get("BUY", 0)
                    sell_count = agreements.get("SELL", 0)
                    failure_reason.append(f"방향성 불명확 (매수동의: {buy_count}, 매도동의: {sell_count}, 최소필요: {min_agreement})")
                
                # 상세 로그를 system.log에 남김
                if failure_reason:
                    strategies = self.signals[symbol]['strategies']
                    strategy_details = (
                        f"전략별 점수: "
                        f"브레이크아웃={strategies['breakout']['signal']:.1f}({strategies['breakout']['direction']}), "
                        f"모멘텀={strategies['momentum']['signal']:.1f}({strategies['momentum']['direction']}), "
                        f"갭={strategies['gap']['signal']:.1f}({strategies['gap']['direction']}), "
                        f"VWAP={strategies['vwap']['signal']:.1f}({strategies['vwap']['direction']}), "
                        f"볼륨={strategies['volume']['signal']:.1f}({strategies['volume']['direction']})"
                    )
                    
                    # 매수 조건 미달 정보를 system.log에 명확하게 기록
                    detail_msg = f"[TRADE_ANALYSIS] {symbol} - {direction} 방향 - 종합점수: {score:.1f}, " + ", ".join(failure_reason)
                    detail_msg += f" | 전략별 점수: BR={strategies['breakout']['signal']:.1f}({strategies['breakout']['direction']}), "
                    detail_msg += f"MM={strategies['momentum']['signal']:.1f}({strategies['momentum']['direction']}), "
                    detail_msg += f"GAP={strategies['gap']['signal']:.1f}({strategies['gap']['direction']}), "
                    detail_msg += f"VWAP={strategies['vwap']['signal']:.1f}({strategies['vwap']['direction']}), "
                    detail_msg += f"VOL={strategies['volume']['signal']:.1f}({strategies['volume']['direction']})"
                    
                    # 시스템 로그에 직접 기록
                    logger.log_system(detail_msg)
                
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
                
                # 시스템 로그에 매매 성공 기록
                logger.log_system(
                    f"[{side}_SUCCESS] {symbol} - {quantity}주 {price}원에 성공, "
                    f"점수: {score:.1f}, 합의수: {sum(agreements.values())}, "
                    f"손절가: {stop_price:.0f}, 익절가: {target_price:.0f}"
                )
                
                # trade.log에도 매매 성공 기록
                logger.log_trade(
                    action=side,
                    symbol=symbol,
                    price=price,
                    quantity=quantity,
                    reason=f"{side} 신호에 따른 매매 성공",
                    score=f"{score:.1f}",
                    position_id=position_id,
                    stop_price=f"{stop_price:.0f}",
                    target_price=f"{target_price:.0f}",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="SUCCESS"
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
            # 종목 스캔 시작 로그 (백엔드에 표시)
            logger.log_system("=" * 50)
            logger.log_system(f"🔍 종목 스캔 시작 - 통합 전략 update_symbols 호출")
            logger.log_system("=" * 50)
            
            # 종목 스캔 시작 trade 로그 기록
            logger.log_trade(
                action="SYMBOL_SCAN_START",
                symbol="SYSTEM",
                price=0,
                quantity=0,
                reason=f"통합 전략 종목 스캔 시작",
                scan_type="통합 전략",
                time=datetime.now().strftime("%H:%M:%S"),
                status="START"
            )
            
            # 환경 변수 확인 - SKIP_WEBSOCKET이 설정되어 있는지 직접 확인
            skip_websocket = os.environ.get('SKIP_WEBSOCKET', '').lower() in ('true', 't', '1', 'yes', 'y')
            
            # 스킵 여부 로그
            if skip_websocket:
                logger.log_system(f"⚠️ SKIP_WEBSOCKET=True 환경 변수 감지됨 - 웹소켓 연결 없이 종목 업데이트를 진행합니다.")
            
            if not new_symbols:
                # 빈 종목 리스트인 경우 fail 로그
                logger.log_system("⚠️ 업데이트할 관심 종목이 없습니다. 기존 종목 유지.")
                logger.log_trade(
                    action="SYMBOL_SCAN_FAILED",
                    symbol="SYSTEM",
                    price=0,
                    quantity=0,
                    reason=f"업데이트할 관심 종목이 없음",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="FAIL"
                )
                return
                
            logger.log_system(f"통합 전략 - 관심 종목 업데이트 시작: {len(new_symbols)}개 종목")
            start_time = datetime.now()
            
            # 새로운 종목 집합
            new_set = set(new_symbols)
            
            # 구독 해제할 종목들 (기존에 있던 종목 중 새로운 목록에 없는 것)
            to_unsubscribe = self.watched_symbols - new_set
            
            # 새로 구독할 종목들 (새로운 목록에 있던 종목 중 기존에 없던 것)
            to_subscribe = new_set - self.watched_symbols
            
            logger.log_system(f"구독 해제 대상: {len(to_unsubscribe)}개, 새로 구독 대상: {len(to_subscribe)}개")
            
            # 구독 해제
            for symbol in to_unsubscribe:
                try:
                    await ws_client.unsubscribe(symbol, "price")
                    if symbol in self.price_data:
                        del self.price_data[symbol]
                    if symbol in self.signals:
                        del self.signals[symbol]
                except Exception as e:
                    logger.log_error(e, f"Failed to unsubscribe from {symbol}")
                    # 에러가 발생해도 계속 진행
            
            # 새로 구독 (최대 세 개씩 처리하며 잠시 대기하여 서버 부하 감소)
            subscribed_count = 0
            failed_count = 0
            batch_size = 3
            
            # 종목을 batch_size 단위로 나누어 처리
            for i in range(0, len(to_subscribe), batch_size):
                batch = list(to_subscribe)[i:i+batch_size]
                
                for symbol in batch:
                    try:
                        # 가격 데이터와 시그널 초기화
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
                        
                        # 웹소켓 구독 - SKIP_WEBSOCKET 상태에 따라 처리
                        if skip_websocket:
                            # 웹소켓 구독 건너뛰기
                            logger.log_system(f"SKIP_WEBSOCKET=True 설정으로 {symbol} 웹소켓 구독 건너뜀")
                            # 콜백 정보 직접 설정
                            callback_key = f"H0STCNT0|{symbol}"
                            ws_client.callbacks[callback_key] = self._handle_price_update
                            # 구독 정보 직접 추가
                            ws_client.subscriptions[symbol] = {"type": "price", "callback": self._handle_price_update}
                            # 성공으로 처리
                            subscribed_count += 1
                        else:
                            # 실제 웹소켓 구독 시도
                            subscription_result = await ws_client.subscribe_price(symbol, self._handle_price_update)
                            if subscription_result:
                                subscribed_count += 1
                            else:
                                failed_count += 1
                                logger.log_system(f"웹소켓 구독 실패: {symbol}")
                    except Exception as e:
                        failed_count += 1
                        logger.log_error(e, f"Failed to subscribe to {symbol}")
                        # 에러가 발생해도 계속 진행
                
                # 배치 처리 후 잠시 대기 (서버 부하 방지)
                if i + batch_size < len(to_subscribe):
                    await asyncio.sleep(0.5)
            
            # 진행 상황 로그
            if skip_websocket:
                logger.log_system(f"종목 업데이트 완료 (SKIP_WEBSOCKET=True): 성공={subscribed_count}, 실패={failed_count}")
            else:
                logger.log_system(f"웹소켓 구독 처리 완료: 성공={subscribed_count}, 실패={failed_count}")
            
            # 개별 전략 업데이트 (에러 처리 강화)
            strategy_update_results = {
                "breakout": False,
                "momentum": False,
                "gap": False, 
                "vwap": False,
                "volume": False
            }
            
            # 개별 전략 업데이트 시작 로그
            logger.log_system("개별 전략 업데이트 시작...")
            
            try:
                await breakout_strategy.update_symbols(new_symbols)
                strategy_update_results["breakout"] = True
                logger.log_system("✅ 브레이크아웃 전략 업데이트 성공")
            except Exception as e:
                logger.log_error(e, "❌ Failed to update symbols in breakout strategy")
                
            try:
                await momentum_strategy.update_symbols(new_symbols)
                strategy_update_results["momentum"] = True
                logger.log_system("✅ 모멘텀 전략 업데이트 성공")
            except Exception as e:
                logger.log_error(e, "❌ Failed to update symbols in momentum strategy")
                
            try:
                await gap_strategy.update_symbols(new_symbols)
                strategy_update_results["gap"] = True
                logger.log_system("✅ 갭 전략 업데이트 성공")
            except Exception as e:
                logger.log_error(e, "❌ Failed to update symbols in gap strategy")
                
            try:
                await vwap_strategy.update_symbols(new_symbols)
                strategy_update_results["vwap"] = True
                logger.log_system("✅ VWAP 전략 업데이트 성공")
            except Exception as e:
                logger.log_error(e, "❌ Failed to update symbols in vwap strategy")
                
            try:
                await volume_strategy.update_symbols(new_symbols)
                strategy_update_results["volume"] = True
                logger.log_system("✅ 볼륨 전략 업데이트 성공")
            except Exception as e:
                logger.log_error(e, "❌ Failed to update symbols in volume strategy")
            
            # 관심 종목 업데이트
            self.watched_symbols = new_set
            
            # 완료 시간 계산 및 상세 로그
            end_time = datetime.now()
            duration_ms = (end_time - start_time).total_seconds() * 1000
            
            success_strategies = [name for name, result in strategy_update_results.items() if result]
            failed_strategies = [name for name, result in strategy_update_results.items() if not result]
            
            # 종목 스캔 완료 구분선 추가
            logger.log_system("=" * 50)
            
            # 상세 로그 추가
            if failed_strategies:
                # 일부 전략 실패 시
                logger.log_system(
                    f"⚠️ 통합 전략 관심 종목 업데이트 부분 완료: {len(self.watched_symbols)}개 종목 "
                    f"(구독 성공: {subscribed_count}, 실패: {failed_count}, 소요시간: {duration_ms:.0f}ms)"
                )
                logger.log_system(f"성공한 전략: {', '.join(success_strategies)}")
                logger.log_system(f"실패한 전략: {', '.join(failed_strategies)}")
            else:
                # 모든 전략 성공 시
                logger.log_system(
                    f"✅ 통합 전략 관심 종목 업데이트 완전 성공: {len(self.watched_symbols)}개 종목 "
                    f"(구독 성공: {subscribed_count}, 실패: {failed_count}, 소요시간: {duration_ms:.0f}ms)"
                )
                logger.log_system(f"모든 전략 업데이트 성공")
            
            # 종목 스캔 완료 구분선 추가
            logger.log_system("=" * 50)
            
            # 스캔 상태 결정
            scan_status = "SUCCESS" if len(failed_strategies) == 0 else "PARTIAL"
            if len(success_strategies) == 0:
                scan_status = "FAIL"
            
            # trade.log에 스캔 결과 기록
            logger.log_trade(
                action="SYMBOL_SCAN_COMPLETE",
                symbol="SYSTEM",
                price=0,
                quantity=len(self.watched_symbols),
                reason=f"통합 전략 관심 종목 업데이트 완료",
                watched_symbols=len(self.watched_symbols),
                new_subscriptions=subscribed_count,
                failed_subscriptions=failed_count,
                success_strategies=",".join(success_strategies),
                failed_strategies=",".join(failed_strategies),
                duration_ms=f"{duration_ms:.0f}",
                time=end_time.strftime("%H:%M:%S"),
                status=scan_status
            )
            
        except Exception as e:
            # 종목 스캔 실패 로그
            logger.log_system("=" * 50)
            logger.log_system(f"❌ 종목 스캔 실패 - 통합 전략 오류: {str(e)}")
            logger.log_system("=" * 50)
            
            logger.log_error(e, "Failed to update symbols in combined strategy")
            
            # 실패 로그 기록
            logger.log_trade(
                action="SYMBOL_SCAN_FAILED",
                symbol="SYSTEM", 
                price=0,
                quantity=0,
                reason=f"통합 전략 오류: {str(e)}",
                time=datetime.now().strftime("%H:%M:%S"),
                status="FAIL"
            )
            
            await alert_system.notify_error(e, "Symbol update error in combined strategy")

# 싱글톤 인스턴스
combined_strategy = CombinedStrategy()

async def pause():
    """모듈 레벨에서 전략 일시 중지"""
    return await combined_strategy.pause()

async def resume():
    """모듈 레벨에서 전략 재개"""
    return await combined_strategy.resume() 