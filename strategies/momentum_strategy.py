"""
모멘텀 전략 (Momentum Strategy)
가격 변화의 방향과 강도를 측정하여 추세를 파악하는 전략
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

class MomentumStrategy:
    """모멘텀 전략 클래스"""
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
                "rsi_period": 14,           # RSI 계산 기간
                "rsi_buy_threshold": 30,    # RSI 매수 임계값
                "rsi_sell_threshold": 70,   # RSI 매도 임계값
                "ma_short_period": 5,       # 단기 이동평균 기간
                "ma_long_period": 20,       # 장기 이동평균 기간
                "stop_loss_pct": 0.02,      # 손절 비율 (2%)
                "take_profit_pct": 0.04,    # 익절 비율 (4%)
                "max_positions": 3,         # 최대 포지션 개수
                "position_size": 1000000    # 기본 포지션 크기 (100만원)
            }
            if hasattr(config["trading"], "momentum_params"):
                self.params.update(config["trading"].momentum_params)
            self.running = False
            self.paused = False
            self.watched_symbols = set()
            self.price_data = {}
            self.positions = {}
            self.indicators = {}
            self.signals = {}
            self._initialized = True
    
    async def start(self, symbols: List[str]):
        """전략 시작"""
        try:
            self.running = True
            self.paused = False
            self.watched_symbols = set(symbols)
            
            # 각 종목별 데이터 초기화
            for symbol in symbols:
                # 최대 이동평균 기간보다 더 큰 값으로 설정
                max_period = max(self.params["rsi_period"], 
                                self.params["ma_long_period"]) + 10
                self.price_data[symbol] = deque(maxlen=max_period * 10)  # 10분봉 기준 충분한 데이터 확보
                
                # 지표 초기화
                self.indicators[symbol] = {
                    'rsi': None, 
                    'ma_short': None, 
                    'ma_long': None,
                    'macd': None,
                    'macd_signal': None,
                    'prev_rsi': None,
                    'prev_ma_cross': False
                }
                
                # 웹소켓 구독
                await ws_client.subscribe_price(symbol, self._handle_price_update)
                
                # 초기 데이터 로딩 (API 호출)
                await self._load_initial_data(symbol)
            
            logger.log_system(f"Momentum strategy started for {len(symbols)} symbols")
            
            # 전략 실행 루프
            asyncio.create_task(self._strategy_loop())
            
        except Exception as e:
            logger.log_error(e, "Failed to start momentum strategy")
            await alert_system.notify_error(e, "Momentum strategy start error")
    
    async def _load_initial_data(self, symbol: str):
        # 현재 시간 가져오기
        now = datetime.now()

        # HHMMSS 형식으로 변환 (콜론 없이 시분초)
        current_time_str = now.strftime("%H%M%S")

        """초기 데이터 로딩"""
        try:
            # 분봉 데이터 조회
            price_data = api_client.get_minute_price(symbol, time_unit=current_time_str)
            if price_data.get("rt_cd") == "0":
                prices = []
                volumes = []
                timestamps = []
                
                # API 응답에서 필요한 데이터 추출 - output2가 실제 차트 데이터
                chart_data = price_data.get("output2", [])
                
                if chart_data:
                    logger.log_system(f"{symbol} - 모멘텀 전략 초기 데이터 {len(chart_data)}개 로드 성공")
                    
                    # 일반적으로 최신 데이터가 먼저 오므로, 과거->현재 순서로 처리하기 위해 reversed 사용
                    # API 응답 구조에 따라 달라질 수 있음
                    for item in reversed(chart_data):
                        # 가격 데이터 - stck_prpr 또는 clos 필드 사용
                        if "stck_prpr" in item:
                            prices.append(float(item["stck_prpr"]))
                        elif "clos" in item:  # 종가(clos) 필드가 있는 경우
                            prices.append(float(item["clos"]))
                        else:
                            continue  # 가격 데이터가 없으면 건너뜀
                        
                        # 거래량 데이터 - cntg_vol 또는 vol 필드 사용
                        if "cntg_vol" in item:
                            volumes.append(int(item["cntg_vol"]))
                        elif "vol" in item:
                            volumes.append(int(item["vol"]))
                        else:
                            volumes.append(0)  # 거래량 데이터가 없으면 0으로 설정
                        
                        # 시간 데이터 - bass_tm 또는 time 필드 사용
                        if "bass_tm" in item:
                            timestamps.append(item["bass_tm"])
                        elif "time" in item:
                            timestamps.append(item["time"])
                        else:
                            timestamps.append("")  # 시간 데이터가 없으면 빈 문자열로 설정
                    
                    # 가격 데이터 저장
                    for i in range(len(prices)):
                        # 시간 정보가 있으면 파싱, 없으면 현재 시간에서 역산
                        if timestamps[i] and len(timestamps[i]) >= 6:
                            # 시간 형식에 맞게 파싱 (예: "093000" -> 09:30:00)
                            try:
                                hour = int(timestamps[i][:2])
                                minute = int(timestamps[i][2:4])
                                second = int(timestamps[i][4:6])
                                timestamp = datetime.now().replace(hour=hour, minute=minute, second=second)
                            except ValueError:
                                # 시간 파싱 실패 시 현재 시간에서 역산
                                timestamp = datetime.now() - timedelta(minutes=len(prices)-i)
                        else:
                            # 시간 정보가 없으면 현재 시간에서 역산
                            timestamp = datetime.now() - timedelta(minutes=len(prices)-i)
                        
                        self.price_data[symbol].append({
                            "price": prices[i],
                            "volume": volumes[i],
                            "timestamp": timestamp
                        })
                    
                    # 초기 지표 계산
                    self._calculate_indicators(symbol)
                    
                    logger.log_system(f"Loaded initial data for {symbol}: {len(prices)} data points")
                else:
                    logger.log_system(f"{symbol} - 모멘텀 전략 초기 데이터 없음")
                
        except Exception as e:
            logger.log_error(e, f"Error loading initial data for {symbol}")
    
    async def _generate_test_data(self, symbol: str):
        """테스트 데이터 생성"""
        try:
            logger.log_system(f"{symbol} - 테스트 데이터 생성 시작")
            test_price = 50000  # 임시 가격
            test_data_count = max(self.params["rsi_period"], self.params["ma_long_period"]) + 10
            
            for i in range(test_data_count):
                # 랜덤하게 가격 변동 생성
                price_change = (np.random.random() - 0.5) * 0.02 * test_price
                test_price += price_change
                
                self.price_data[symbol].append({
                    "price": test_price,
                    "volume": int(np.random.random() * 10000),
                    "timestamp": datetime.now() - timedelta(minutes=test_data_count-i)
                })
            
            # 지표 계산
            self._calculate_indicators(symbol)
            logger.log_system(f"{symbol} - 테스트 데이터 {test_data_count}개 생성 및 지표 계산 완료")
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 테스트 데이터 생성 실패")
    
    async def stop(self):
        """전략 중지"""
        self.running = False
        
        # 웹소켓 구독 해제
        for symbol in self.watched_symbols:
            await ws_client.unsubscribe(symbol, "price")
        
        logger.log_system("Momentum strategy stopped")
    
    async def pause(self):
        """전략 일시 중지"""
        if not self.paused:
            self.paused = True
            logger.log_system("Momentum strategy paused")
        return True

    async def resume(self):
        """전략 재개"""
        if self.paused:
            self.paused = False
            logger.log_system("Momentum strategy resumed")
        return True
    
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
                
                # 일정 간격마다 지표 업데이트 (3초마다)
                current_time = datetime.now()
                if current_time.second % 3 == 0 and current_time.microsecond < 100000:
                    self._calculate_indicators(symbol)
                
        except Exception as e:
            logger.log_error(e, "Error handling price update in momentum strategy")
    
    def _calculate_indicators(self, symbol: str):
        """기술적 지표 계산"""
        try:
            if len(self.price_data[symbol]) < max(self.params["rsi_period"], self.params["ma_long_period"]):
                return
            
            # 가격 데이터 추출
            prices = [item["price"] for item in self.price_data[symbol]]
            
            # RSI 계산
            rsi_period = self.params["rsi_period"]
            if len(prices) >= rsi_period + 1:
                # 이전 RSI 값 저장
                self.indicators[symbol]['prev_rsi'] = self.indicators[symbol]['rsi']
                
                # 가격 변화
                delta = np.diff(prices[-rsi_period-1:])
                
                # 상승/하락 분리
                gain = np.where(delta > 0, delta, 0)
                loss = np.where(delta < 0, -delta, 0)
                
                # 평균 상승/하락
                avg_gain = np.mean(gain)
                avg_loss = np.mean(loss)
                
                # RSI 계산
                if avg_loss == 0:
                    rsi = 100
                else:
                    rs = avg_gain / avg_loss
                    rsi = 100 - (100 / (1 + rs))
                
                self.indicators[symbol]['rsi'] = rsi
            
            # 이동평균 계산
            ma_short_period = self.params["ma_short_period"]
            ma_long_period = self.params["ma_long_period"]
            
            if len(prices) >= ma_long_period:
                # 이전 이동평균 저장
                prev_ma_short = self.indicators[symbol]['ma_short']
                prev_ma_long = self.indicators[symbol]['ma_long']
                
                # 새 이동평균 계산
                ma_short = np.mean(prices[-ma_short_period:])
                ma_long = np.mean(prices[-ma_long_period:])
                
                self.indicators[symbol]['ma_short'] = ma_short
                self.indicators[symbol]['ma_long'] = ma_long
                
                # 골든 크로스/데드 크로스 감지
                prev_cross = self.indicators[symbol]['prev_ma_cross']
                current_cross = ma_short > ma_long
                
                # 크로스 상태 변화 감지
                if prev_cross is not None and prev_cross != current_cross:
                    if current_cross:
                        logger.log_system(f"Golden Cross detected for {symbol}")
                    else:
                        logger.log_system(f"Dead Cross detected for {symbol}")
                
                self.indicators[symbol]['prev_ma_cross'] = current_cross
            
            # MACD 계산 (12, 26, 9 일반적인 값)
            if len(prices) >= 26 + 9:
                ema12 = self._calculate_ema(prices, 12)
                ema26 = self._calculate_ema(prices, 26)
                
                macd_line = ema12 - ema26
                signal_line = self._calculate_ema(
                    [macd_line] if isinstance(macd_line, float) else macd_line, 9
                )
                
                self.indicators[symbol]['macd'] = macd_line
                self.indicators[symbol]['macd_signal'] = signal_line
            
        except Exception as e:
            logger.log_error(e, f"Error calculating indicators for {symbol}")
    
    def _calculate_ema(self, prices: List[float], period: int) -> float:
        """지수 이동평균 계산"""
        if len(prices) < period:
            return None
            
        # 단순 이동평균으로 시작
        sma = np.mean(prices[:period])
        
        # 승수 계산
        multiplier = 2 / (period + 1)
        
        # EMA 계산
        ema = sma
        for i in range(period, len(prices)):
            ema = (prices[i] - ema) * multiplier + ema
            
        return ema
    
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
                
                # 각 종목 분석
                for symbol in self.watched_symbols:
                    await self._analyze_and_trade(symbol)
                
                # 포지션 모니터링
                await self._monitor_positions()
                
                await asyncio.sleep(1)  # 1초 대기
                
            except Exception as e:
                logger.log_error(e, "Momentum strategy loop error")
                await asyncio.sleep(5)  # 에러 시 5초 대기
    
    async def _analyze_and_trade(self, symbol: str):
        """종목 분석 및 거래"""
        try:
            # 전략이 일시 중지 상태인지 확인
            if self.paused or order_manager.is_trading_paused():
                return
                
            # 충분한 데이터/지표 있는지 확인
            if not self.indicators.get(symbol) or not self.indicators[symbol]['rsi']:
                return
                
            # 현재가
            if not self.price_data[symbol]:
                return
                
            current_price = self.price_data[symbol][-1]["price"]
            
            # 이미 포지션 있는지 확인
            symbol_positions = self._get_symbol_positions(symbol)
            if len(symbol_positions) >= self.params.get("max_positions", 3):
                return
            
            # 현재 지표값
            indicators = self.indicators[symbol]
            rsi = indicators['rsi']
            ma_short = indicators['ma_short']
            ma_long = indicators['ma_long']
            macd = indicators['macd']
            macd_signal = indicators['macd_signal']
            
            # 매수 신호 확인
            buy_signal = False
            buy_reason = ""
            
            # RSI 매수 신호
            if rsi is not None and indicators['prev_rsi'] is not None:
                if indicators['prev_rsi'] < self.params["rsi_buy_threshold"] and rsi >= self.params["rsi_buy_threshold"]:
                    buy_signal = True
                    buy_reason = "rsi_buy"
            
            # 이동평균 골든 크로스 (단기 이평이 장기 이평 상향 돌파)
            if ma_short is not None and ma_long is not None and indicators['prev_ma_cross'] is not None:
                if not indicators['prev_ma_cross'] and ma_short > ma_long:
                    buy_signal = True
                    buy_reason = "golden_cross"
            
            # MACD 매수 신호 (MACD가 시그널 라인 상향 돌파)
            if macd is not None and macd_signal is not None:
                if macd > macd_signal and \
                   (isinstance(macd, float) or (len(macd) > 1 and macd[-2] <= macd_signal[-2])):
                    buy_signal = True
                    buy_reason = "macd_cross"
            
            # 매도 신호 확인
            sell_signal = False
            sell_reason = ""
            
            # RSI 매도 신호
            if rsi is not None and indicators['prev_rsi'] is not None:
                if indicators['prev_rsi'] > self.params["rsi_sell_threshold"] and rsi <= self.params["rsi_sell_threshold"]:
                    sell_signal = True
                    sell_reason = "rsi_sell"
            
            # 이동평균 데드 크로스 (단기 이평이 장기 이평 하향 돌파)
            if ma_short is not None and ma_long is not None and indicators['prev_ma_cross'] is not None:
                if indicators['prev_ma_cross'] and ma_short < ma_long:
                    sell_signal = True
                    sell_reason = "dead_cross"
            
            # MACD 매도 신호 (MACD가 시그널 라인 하향 돌파)
            if macd is not None and macd_signal is not None:
                if macd < macd_signal and \
                   (isinstance(macd, float) or (len(macd) > 1 and macd[-2] >= macd_signal[-2])):
                    sell_signal = True
                    sell_reason = "macd_cross"
            
            # 매매 실행
            if buy_signal:
                await self._enter_position(symbol, "BUY", current_price, buy_reason)
            elif sell_signal:
                await self._enter_position(symbol, "SELL", current_price, sell_reason)
                
        except Exception as e:
            logger.log_error(e, f"Momentum analysis error for {symbol}")
    
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
            position_size = self.params.get("position_size", 1000000)  # 100만원
            quantity = int(position_size / current_price)
            
            if quantity <= 0:
                return
            
            # 주문 실행
            result = await order_manager.place_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type="MARKET",
                strategy="momentum",
                reason=reason
            )
            
            if result["status"] == "success":
                # 손절/익절 가격 계산
                stop_loss_pct = self.params.get("stop_loss_pct", 0.02)
                take_profit_pct = self.params.get("take_profit_pct", 0.04)
                
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
                    "reason": reason
                }
                
                logger.log_system(
                    f"Momentum: Entered {side} position for {symbol} at {current_price}, "
                    f"stop: {stop_price}, target: {target_price}, reason: {reason}"
                )
                
        except Exception as e:
            logger.log_error(e, f"Momentum entry error for {symbol}")
    
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
                
                # RSI 관찰 (익절 시점에 활용)
                rsi = self.indicators[symbol].get('rsi')
                
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
                    # RSI 기반 익절 - RSI가 과매수 구간(70)에 들어가면 일부 익절
                    elif rsi is not None and rsi > self.params["rsi_sell_threshold"]:
                        should_exit = True
                        exit_reason = "rsi_overbought"
                        
                else:  # SELL
                    # 매도 포지션
                    if current_price >= position["stop_price"]:
                        should_exit = True
                        exit_reason = "stop_loss"
                    elif current_price <= position["target_price"]:
                        should_exit = True
                        exit_reason = "take_profit"
                    # RSI 기반 익절 - RSI가 과매도 구간(30)에 들어가면 일부 익절
                    elif rsi is not None and rsi < self.params["rsi_buy_threshold"]:
                        should_exit = True
                        exit_reason = "rsi_oversold"
                
                # 청산 실행
                if should_exit:
                    await self._exit_position(position_id, exit_reason)
                    
        except Exception as e:
            logger.log_error(e, "Momentum position monitoring error")
    
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
                strategy="momentum",
                reason=reason
            )
            
            if result["status"] == "success":
                # 포지션 제거
                del self.positions[position_id]
                
                logger.log_system(f"Momentum: Exited position for {symbol}, reason: {reason}")
                
        except Exception as e:
            logger.log_error(e, f"Momentum exit error for position {position_id}")
    
    def get_signal_strength(self, symbol: str) -> float:
        """신호 강도 측정 (0 ~ 10)"""
        try:
            if symbol not in self.price_data or not self.price_data[symbol]:
                return 0
                
            # 지표 계산 안된 경우
            indicators = self.indicators.get(symbol, {})
            if not indicators or indicators.get('rsi') is None:
                return 0
            
            # 점수 합계
            score = 0
            
            # RSI 기반 점수 (0-3점)
            rsi = indicators['rsi']
            if rsi < 30:
                # 과매도 상태 - 매수 신호 강도
                score += 3 * (1 - rsi/30)
            elif rsi > 70:
                # 과매수 상태 - 매도 신호 강도
                score += 3 * ((rsi-70)/30)
            
            # 이동평균 교차 기반 점수 (0-3점)
            ma_short = indicators.get('ma_short')
            ma_long = indicators.get('ma_long')
            if ma_short is not None and ma_long is not None:
                # 골든 크로스/데드 크로스 강도
                cross_ratio = abs(ma_short - ma_long) / ma_long
                if ma_short > ma_long:  # 골든 크로스 (매수)
                    score += min(3, cross_ratio * 300)
                else:  # 데드 크로스 (매도)
                    score += min(3, cross_ratio * 300)
            
            # MACD 기반 점수 (0-4점)
            macd = indicators.get('macd')
            macd_signal = indicators.get('macd_signal')
            if macd is not None and macd_signal is not None:
                # MACD와 시그널 라인 차이의 강도
                macd_value = macd if isinstance(macd, float) else macd[-1]
                signal_value = macd_signal if isinstance(macd_signal, float) else macd_signal[-1]
                
                if abs(signal_value) > 0:
                    macd_strength = abs(macd_value - signal_value) / abs(signal_value)
                    if macd_value > signal_value:  # 매수 신호
                        score += min(4, macd_strength * 20)
                    else:  # 매도 신호
                        score += min(4, macd_strength * 20)
            
            return score
            
        except Exception as e:
            logger.log_error(e, f"Error calculating momentum signal strength for {symbol}")
            return 0
    
    def get_signal_direction(self, symbol: str) -> str:
        """현재 신호 방향 반환"""
        if symbol in self.signals:
            return self.signals[symbol].get("direction", "NEUTRAL")
        return "NEUTRAL"
        
    async def get_signal(self, symbol: str) -> Dict[str, Any]:
        """전략 신호 반환 (combined_strategy에서 호출)"""
        try:
            # 1. 데이터 유효성 검증
            if not await self._ensure_valid_data(symbol):
                return {"signal": 0, "direction": "NEUTRAL", "reason": "insufficient_data"}
                
            # 2. 지표 계산 (필요시에만)
            if not self._has_valid_indicators(symbol):
                self._calculate_indicators(symbol)
                
            # 3. 신호 계산 (별도 함수로 분리)
            signal_info = self._calculate_signal_strength(symbol)
                
            # 4. 신호 정보 저장 및 반환
            self.signals[symbol] = {
                "strength": signal_info["signal"],
                "direction": signal_info["direction"],
                "last_update": datetime.now()
            }
                
            return signal_info
                
        except Exception as e:
            logger.log_error(e, f"{symbol} - 모멘텀 신호 계산 오류")
            return {"signal": 0, "direction": "NEUTRAL", "reason": "error"}
            
    async def _ensure_valid_data(self, symbol: str) -> bool:
        """종목의 데이터가 유효한지 확인하고 필요시 초기화"""
        # 가격 데이터 초기화 확인
        if symbol not in self.price_data or not self.price_data[symbol]:
            # 데이터 로드 시도
            await self._load_initial_data(symbol)
                
        # 최소 필요 데이터 확인
        min_required_data = max(self.params["rsi_period"], self.params["ma_long_period"])
        if len(self.price_data[symbol]) < min_required_data:
            # 실제 데이터가 부족하면 신호 생성을 안 함 (임시 데이터 생성 대신)
            logger.log_system(f"{symbol} - 모멘텀 전략에 필요한 충분한 데이터가 없음")
            return False
                
        # 현재가 확인
        if not await self._ensure_current_price(symbol):
            return False
                
        return True
            
    async def _ensure_current_price(self, symbol: str) -> bool:
        """현재가가 있는지 확인하고 없으면 조회"""
        current_price = 0
        if self.price_data[symbol]:
            current_price = self.price_data[symbol][-1]["price"]
                
        # 현재가가 없으면 API에서 가져오기
        if current_price <= 0:
            try:
                symbol_info = await api_client.get_symbol_info(symbol)
                if symbol_info and "current_price" in symbol_info:
                    current_price = float(symbol_info["current_price"])
                    # 가격 데이터 업데이트
                    self.price_data[symbol].append({
                        "price": current_price,
                        "volume": 0,
                        "timestamp": datetime.now()
                    })
                    return True
                return False
            except Exception as e:
                logger.log_error(e, f"{symbol} - 모멘텀 현재가 조회 실패")
                return False
        return True
            
    def _has_valid_indicators(self, symbol: str) -> bool:
        """유효한 지표가 있는지 확인"""
        indicators = self.indicators.get(symbol, {})
        return (indicators and 
                indicators.get('rsi') is not None and 
                indicators.get('ma_short') is not None and 
                indicators.get('ma_long') is not None)
            
    def _calculate_signal_strength(self, symbol: str) -> Dict[str, Any]:
        """모멘텀 신호 강도 및 방향 계산"""
        indicators = self.indicators[symbol]
        
        # 기본 변수 설정
        direction = "NEUTRAL"
        signal_strength = 0
        
        # 지표 추출
        rsi = indicators['rsi']
        rsi_buy = self.params["rsi_buy_threshold"]
        rsi_sell = self.params["rsi_sell_threshold"]
        ma_short = indicators['ma_short']
        ma_long = indicators['ma_long']
        ma_cross = ma_short > ma_long
        
        # 가중치 설정 (각 지표의 중요도)
        weights = {
            "rsi": 0.4,      # RSI 가중치 40%
            "ma_cross": 0.3, # 이동평균 교차 가중치 30%
            "macd": 0.3      # MACD 가중치 30%
        }
        
        # 1. RSI 신호 계산
        rsi_signal = 0
        rsi_direction = "NEUTRAL"
        
        if rsi < rsi_buy:
            rsi_direction = "BUY"
            rsi_signal = (rsi_buy - rsi) / rsi_buy * 10  # 0-10 점수
        elif rsi > rsi_sell:
            rsi_direction = "SELL"
            rsi_signal = (rsi - rsi_sell) / (100 - rsi_sell) * 10  # 0-10 점수
        
        # 2. 이동평균 신호 계산
        ma_signal = 0
        ma_direction = "NEUTRAL"
        
        if ma_cross:  # 골든 크로스
            ma_direction = "BUY"
            ma_diff_pct = ((ma_short/ma_long)-1)*100 if ma_long > 0 else 0
            ma_signal = min(10, ma_diff_pct * 5)  # 0-10 점수
        else:  # 데드 크로스
            ma_direction = "SELL"
            ma_diff_pct = ((ma_long/ma_short)-1)*100 if ma_short > 0 else 0
            ma_signal = min(10, ma_diff_pct * 5)  # 0-10 점수
        
        # 3. MACD 신호 계산
        macd_signal = 0
        macd_direction = "NEUTRAL"
        
        if indicators.get('macd') is not None and indicators.get('macd_signal') is not None:
            macd = indicators['macd']
            macd_sig = indicators['macd_signal']
            
            if macd > macd_sig:
                macd_direction = "BUY"
                macd_diff = abs(macd - macd_sig)
                macd_signal = min(10, macd_diff * 20 if abs(macd_sig) > 0 else 2)
            else:
                macd_direction = "SELL"
                macd_diff = abs(macd_sig - macd)
                macd_signal = min(10, macd_diff * 20 if abs(macd_sig) > 0 else 2)
        
        # 4. 종합 신호 계산 (가중 평균)
        # 각 지표의 방향이 일치하는 경우 신호 강화
        if rsi_direction == ma_direction == macd_direction:
            # 방향 일치 시 보너스
            direction = rsi_direction
            # 가중 평균 + 보너스
            signal_strength = (
                rsi_signal * weights["rsi"] +
                ma_signal * weights["ma_cross"] +
                macd_signal * weights["macd"]
            ) * 1.2  # 20% 보너스
        else:
            # 가장 강한 신호의 방향 선택
            signals = [
                (rsi_signal * weights["rsi"], rsi_direction),
                (ma_signal * weights["ma_cross"], ma_direction),
                (macd_signal * weights["macd"], macd_direction)
            ]
            
            max_signal = max(signals, key=lambda x: x[0])
            if max_signal[0] > 0:
                direction = max_signal[1]
                # 가중 평균
                signal_strength = (
                    rsi_signal * weights["rsi"] +
                    ma_signal * weights["ma_cross"] +
                    macd_signal * weights["macd"]
                )
        
        # 5. 최종 신호 강도 제한 (0-10)
        signal_strength = min(10, max(0, signal_strength))
        
        # 상세 정보 포함하여 반환
        return {
            "signal": signal_strength, 
            "direction": direction,
            "rsi": round(rsi, 1),
            "rsi_signal": round(rsi_signal, 1),
            "ma_cross": "GOLDEN" if ma_cross else "DEAD",
            "ma_signal": round(ma_signal, 1),
            "ma_diff_pct": f"{((ma_short/ma_long)-1)*100:.2f}%" if ma_long > 0 else "N/A",
            "macd_signal": round(macd_signal, 1) if indicators.get('macd') is not None else "N/A"
        }
    
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
                if symbol in self.indicators:
                    del self.indicators[symbol]
                if symbol in self.signals:
                    del self.signals[symbol]
            
            # 새 종목 초기화
            for symbol in to_add:
                self.price_data[symbol] = deque(maxlen=100)
                self.indicators[symbol] = {
                    'rsi': [],
                    'ma_short': [],
                    'ma_long': [],
                    'macd': [],
                    'macd_signal': [],
                    'macd_hist': []
                }
                self.signals[symbol] = {
                    'strength': 0,
                    'direction': "NEUTRAL",
                    'last_update': None
                }
            
            # 감시 종목 업데이트
            self.watched_symbols = list(new_set)
            
            logger.log_system(f"모멘텀 전략: 감시 종목 {len(self.watched_symbols)}개로 업데이트됨")
        
        except Exception as e:
            logger.log_error(e, "모멘텀 전략 종목 업데이트 오류")

# 싱글톤 인스턴스
momentum_strategy = MomentumStrategy() 