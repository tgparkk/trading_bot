"""
모멘텀 전략 (Momentum Strategy)
가격 변화의 방향과 강도를 측정하여 추세를 파악하는 전략
"""
import asyncio
from typing import Dict, Any, List, Optional, Deque, Tuple
from datetime import datetime, time, timedelta
from collections import deque
import numpy as np
import threading
import traceback

from config.settings import config
from core.api_client import api_client
from core.websocket_client import ws_client
from core.order_manager import order_manager
from utils.logger import logger
from monitoring.alert_system import alert_system
from core.data_cache import data_cache  # 중앙화된 데이터 캐싱 서비스 추가

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
            # 기본 파라미터 설정
            self.params = {
                "rsi_period": 14,           # RSI 계산 기간
                "rsi_buy_threshold": 30,    # RSI 매수 임계값
                "rsi_sell_threshold": 70,   # RSI 매도 임계값
                "ma_short_period": 5,       # 단기 이동평균 기간
                "ma_long_period": 20,       # 장기 이동평균 기간
                "macd_fast_period": 12,     # MACD 빠른 EMA 기간
                "macd_slow_period": 26,     # MACD 느린 EMA 기간
                "macd_signal_period": 9,    # MACD 시그널 EMA 기간
                "stop_loss_pct": 0.02,      # 손절 비율 (2%)
                "take_profit_pct": 0.04,    # 익절 비율 (4%)
                "trailing_stop_pct": 0.01,  # 트레일링 스탑 비율 (1%)
                "max_positions": 3,         # 최대 포지션 개수
                "position_size": 1000000,   # 기본 포지션 크기 (100만원)
                "min_data_points": 30,      # 최소 필요 데이터 포인트
                "indicator_update_interval": 3,  # 지표 업데이트 간격(초)
                "use_trailing_stop": True,  # 트레일링 스탑 사용 여부
                "data_source_priority": ["websocket", "cache", "api"],  # 데이터 소스 우선순위
                "risk_factor": 1.0,         # 리스크 조정 계수 (1.0 = 정상)
                "retry_attempts": 3,        # API 재시도 횟수
                "retry_delay": 1.0          # API 재시도 간격(초)
            }
            
            # 설정파일에서 파라미터 로드
            if hasattr(config["trading"], "momentum_params"):
                self.params.update(config["trading"].momentum_params)
            
            # 상태 변수 초기화
            self.running = False
            self.paused = False
            self.watched_symbols = set()
            self.price_data = {}   # {symbol: deque(price_data)}
            self.positions = {}    # {position_id: position_data}
            self.indicators = {}   # {symbol: indicator_data}
            self.signals = {}      # {symbol: signal_data}
            
            # 최근 지표 업데이트 시간 추적
            self.last_indicator_update = {}  # {symbol: datetime}
            
            # 데이터 캐시 참조 (중앙화된 데이터 관리)
            self.data_cache = data_cache
            
            # 성능 측정 통계
            self.performance_stats = {
                "signal_calculations": 0,
                "successful_trades": 0,
                "failed_trades": 0,
                "api_calls": 0,
                "api_errors": 0,
                "indicator_calculations": 0
            }
            
            # 마지막 데이터 유효성 결과 캐싱
            self.data_validity_cache = {}  # {symbol: (timestamp, is_valid)}
            
            # 초기화 완료 플래그
            self._initialized = True
            logger.log_system("모멘텀 전략 초기화 완료")
    
    async def start(self, symbols: List[str]):
        """전략 시작"""
        try:
            self.running = True
            self.paused = False
            self.watched_symbols = set(symbols)
            
            # 상태 로깅
            logger.log_system(f"모멘텀 전략 시작: {len(symbols)}개 종목")
            
            # 병렬로 각 종목 데이터 초기화
            initialization_tasks = []
            for symbol in symbols:
                initialization_tasks.append(
                    self._initialize_symbol_data(symbol)
                )
                
            # 모든 초기화 태스크 완료 대기 (병렬 처리)
            if initialization_tasks:
                initialization_results = await asyncio.gather(
                    *initialization_tasks, return_exceptions=True
                )
                
                # 에러 확인 및 로깅
                for i, result in enumerate(initialization_results):
                    if isinstance(result, Exception):
                        symbol = symbols[i] if i < len(symbols) else "unknown"
                        logger.log_error(result, f"{symbol} 모멘텀 전략 초기화 실패")
            
            # 전략 실행 루프 시작
            self.strategy_task = asyncio.create_task(self._strategy_loop())
            logger.log_system(f"모멘텀 전략 루프 시작됨 - {len(self.watched_symbols)}개 종목 감시 중")
            
        except Exception as e:
            logger.log_error(e, "모멘텀 전략 시작 실패")
            await alert_system.notify_error(e, "모멘텀 전략 시작 오류")
            self.running = False
            raise
    
    async def _initialize_symbol_data(self, symbol: str) -> bool:
        """개별 종목 데이터 초기화"""
        try:
            # 지표 계산에 필요한 최대 기간 계산
            max_period = max(
                self.params["rsi_period"],
                self.params["ma_long_period"],
                self.params["macd_slow_period"] + self.params["macd_signal_period"]
            ) + 10  # 여유분 추가
            
            # 가격 데이터 큐 초기화
            self.price_data[symbol] = deque(maxlen=max_period * 2)  # 충분한 크기로 설정
            
            # 지표 초기화
            self.indicators[symbol] = {
                'rsi': None, 
                'ma_short': None, 
                'ma_long': None,
                'macd': None,
                'macd_signal': None,
                'macd_histogram': None,
                'prev_rsi': None,
                'prev_ma_cross': False,
                'last_calculation': None  # 마지막 계산 시간
            }
            
            # 신호 초기화
            self.signals[symbol] = {
                'strength': 0,
                'direction': "NEUTRAL",
                'last_update': None,
                'components': {}  # 각 지표별 신호 컴포넌트
            }
            
            # 마지막 업데이트 시간 초기화
            self.last_indicator_update[symbol] = datetime.now() - timedelta(minutes=5)
            
            # 중앙 캐시에 종목 등록
            await self.data_cache.register_symbol(symbol, "momentum")
            
            # 웹소켓 구독
            await ws_client.subscribe_price(symbol, self._handle_price_update)
            
            # 초기 데이터 로딩 (API 호출)
            loaded = await self._load_initial_data(symbol)
            if not loaded:
                logger.log_warning(f"{symbol} - 모멘텀 전략 초기 데이터 로드 실패, 대체 데이터 사용")
                
                # 데이터 캐시에서 시도
                cached_data = await self.data_cache.get_price_history(symbol, max_period)
                if cached_data and len(cached_data) >= self.params["min_data_points"]:
                    # 캐시된 데이터 사용
                    self._process_cached_data(symbol, cached_data)
                    logger.log_system(f"{symbol} - 캐시에서 {len(cached_data)}개 데이터 로드")
                    loaded = True
                else:
                    # 테스트 데이터 생성
                    await self._generate_test_data(symbol)
                    logger.log_system(f"{symbol} - 테스트 데이터 생성됨")
                    loaded = True
            
            return loaded
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 모멘텀 전략 종목 초기화 실패")
            # 실패해도 감시 종목에는 등록
            self.watched_symbols.add(symbol)
            return False
    
    def _process_cached_data(self, symbol: str, cached_data: List[Dict[str, Any]]):
        """캐시된 데이터 처리"""
        # 캐시 데이터 포맷: [{price: float, volume: int, timestamp: datetime}, ...]
        for data_point in cached_data:
            self.price_data[symbol].append(data_point)
        
        # 지표 계산
        self._calculate_indicators(symbol)
    
    async def _load_initial_data(self, symbol: str) -> bool:
        """초기 데이터 로딩"""
        try:
            # 현재 시간 가져오기
            now = datetime.now()
            current_time_str = now.strftime("%H%M%S")
            
            # 재시도 설정
            max_attempts = self.params["retry_attempts"]
            retry_delay = self.params["retry_delay"]
            
            # 분봉 데이터 조회 (재시도 로직 포함)
            for attempt in range(max_attempts):
                try:
                    self.performance_stats["api_calls"] += 1
                    price_data = await asyncio.wait_for(
                        api_client.get_minute_price(symbol, time_unit=current_time_str),
                        timeout=5.0
                    )
                    
                    if price_data and price_data.get("rt_cd") == "0":
                        chart_data = price_data.get("output2", [])
                        
                        if not chart_data:
                            logger.log_warning(f"{symbol} - API 응답에 차트 데이터가 없음")
                            if attempt < max_attempts - 1:
                                await asyncio.sleep(retry_delay)
                                continue
                            return False
                        
                        # 데이터 처리
                        processed = self._process_price_data(symbol, chart_data)
                        if processed:
                            logger.log_system(f"{symbol} - 모멘텀 전략 초기 데이터 {len(chart_data)}개 로드 성공")
                            
                            # 중앙 캐시에 데이터 저장
                            await self.data_cache.update_price_history(symbol, list(self.price_data[symbol]))
                            
                            return True
                        else:
                            logger.log_warning(f"{symbol} - 차트 데이터 처리 실패")
                            if attempt < max_attempts - 1:
                                await asyncio.sleep(retry_delay)
                                continue
                            return False
                    else:
                        error_msg = price_data.get("msg_cd", "알 수 없는 오류") if price_data else "응답 없음"
                        logger.log_warning(f"{symbol} - API 오류 응답: {error_msg}")
                        self.performance_stats["api_errors"] += 1
                        
                        if attempt < max_attempts - 1:
                            await asyncio.sleep(retry_delay)
                            continue
                        return False
                        
                except asyncio.TimeoutError:
                    logger.log_warning(f"{symbol} - API 타임아웃 (시도 {attempt+1}/{max_attempts})")
                    self.performance_stats["api_errors"] += 1
                    
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                    return False
                    
                except Exception as e:
                    logger.log_error(e, f"{symbol} - API 호출 중 오류 (시도 {attempt+1}/{max_attempts})")
                    self.performance_stats["api_errors"] += 1
                    
                    if attempt < max_attempts - 1:
                        await asyncio.sleep(retry_delay)
                        continue
                    return False
            
            # 모든 시도 실패
            return False
                
        except Exception as e:
            logger.log_error(e, f"{symbol} - 초기 데이터 로드 중 예외 발생")
            self.performance_stats["api_errors"] += 1
            return False
    
    def _process_price_data(self, symbol: str, chart_data: List[Dict[str, Any]]) -> bool:
        """API 응답 차트 데이터 처리"""
        try:
            if not chart_data:
                return False
                
            prices = []
            volumes = []
            timestamps = []
            
            # 일반적으로 최신 데이터가 먼저 오므로, 과거->현재 순서로 처리하기 위해 reversed 사용
            for item in reversed(chart_data):
                # 가격 데이터 추출
                if "stck_prpr" in item:
                    price = self._safe_float(item["stck_prpr"])
                elif "clos" in item:  # 종가(clos) 필드가 있는 경우
                    price = self._safe_float(item["clos"])
                else:
                    continue  # 가격 데이터가 없으면 건너뜀
                
                # 유효하지 않은 가격 건너뛰기
                if price <= 0:
                    continue
                    
                prices.append(price)
                
                # 거래량 데이터 추출
                if "cntg_vol" in item:
                    volume = self._safe_int(item["cntg_vol"])
                elif "vol" in item:
                    volume = self._safe_int(item["vol"])
                else:
                    volume = 0  # 거래량 데이터가 없으면 0으로 설정
                    
                volumes.append(volume)
                
                # 시간 데이터 추출
                if "bass_tm" in item:
                    time_str = item["bass_tm"]
                elif "time" in item:
                    time_str = item["time"]
                else:
                    time_str = ""  # 시간 데이터가 없으면 빈 문자열로 설정
                    
                timestamps.append(time_str)
            
            # 충분한 데이터가 있는지 확인
            if len(prices) < self.params["min_data_points"]:
                logger.log_warning(f"{symbol} - 충분한 데이터 포인트가 없음: {len(prices)}/{self.params['min_data_points']}")
                return False
            
            # 가격 데이터 저장
            for i in range(len(prices)):
                # 시간 정보 처리
                timestamp = self._parse_time_string(timestamps[i], len(prices)-i)
                
                # 데이터 저장
                self.price_data[symbol].append({
                    "price": prices[i],
                    "volume": volumes[i],
                    "timestamp": timestamp
                })
            
            # 초기 지표 계산
            if len(self.price_data[symbol]) >= self.params["min_data_points"]:
                self._calculate_indicators(symbol)
                return True
            else:
                logger.log_warning(f"{symbol} - 지표 계산에 충분한 데이터가 없음: {len(self.price_data[symbol])}")
                return False
                
        except Exception as e:
            logger.log_error(e, f"{symbol} - 차트 데이터 처리 중 오류")
            return False
    
    def _parse_time_string(self, time_str: str, offset: int) -> datetime:
        """시간 문자열 파싱"""
        now = datetime.now()
        
        # 시간 형식에 맞게 파싱 (예: "093000" -> 09:30:00)
        if time_str and len(time_str) >= 6:
            try:
                hour = int(time_str[:2])
                minute = int(time_str[2:4])
                second = int(time_str[4:6])
                
                # 현재 날짜와 파싱된 시간으로 datetime 생성
                timestamp = now.replace(
                    hour=hour, 
                    minute=minute, 
                    second=second, 
                    microsecond=0
                )
                
                # 파싱된 시간이 미래인 경우 전날로 조정
                if timestamp > now:
                    timestamp = timestamp - timedelta(days=1)
                    
                return timestamp
            except (ValueError, IndexError):
                # 시간 파싱 실패 시 현재 시간에서 역산
                return now - timedelta(minutes=offset)
        else:
            # 시간 정보가 없으면 현재 시간에서 역산
            return now - timedelta(minutes=offset)
    
    def _safe_float(self, value: Any) -> float:
        """안전하게 float로 변환"""
        try:
            if isinstance(value, (int, float)):
                return float(value)
            elif isinstance(value, str):
                # 쉼표 제거 후 변환
                value = value.replace(',', '')
                return float(value)
            else:
                return 0.0
        except (ValueError, TypeError):
            return 0.0
    
    def _safe_int(self, value: Any) -> int:
        """안전하게 int로 변환"""
        try:
            if isinstance(value, int):
                return value
            elif isinstance(value, float):
                return int(value)
            elif isinstance(value, str):
                # 쉼표 제거 후 변환
                value = value.replace(',', '')
                return int(float(value))
            else:
                return 0
        except (ValueError, TypeError):
            return 0
    
    async def _generate_test_data(self, symbol: str) -> bool:
        """테스트 데이터 생성 (실제 API 데이터 없을 경우)"""
        try:
            logger.log_system(f"{symbol} - 테스트 데이터 생성 시작")
            
            # 현재가 조회 시도 (실제 가격 기준점으로 사용)
            base_price = 50000  # 기본값
            try:
                symbol_info = await api_client.get_symbol_info(symbol)
                if symbol_info and "current_price" in symbol_info:
                    base_price = float(symbol_info["current_price"])
            except Exception:
                # 현재가 조회 실패해도 계속 진행
                pass
            
            # 테스트 데이터 생성용 파라미터
            test_data_count = max(
                self.params["min_data_points"],
                self.params["rsi_period"] * 2,
                self.params["ma_long_period"] * 2
            )
            
            # 가격 변동성 (현재가의 1%)
            volatility = base_price * 0.01
            
            # 트렌드 방향 (-1, 0, 1 중 하나)
            trend = np.random.choice([-1, 0, 1])
            
            # 테스트 데이터 생성
            test_price = base_price
            now = datetime.now()
            
            for i in range(test_data_count):
                # 트렌드 요소
                trend_component = trend * (i / test_data_count) * volatility * 2
                
                # 랜덤 노이즈
                noise = (np.random.random() - 0.5) * volatility
                
                # 가격 변화 계산
                price_change = trend_component + noise
                test_price = max(1, test_price + price_change)
                
                # 거래량 (랜덤)
                volume = int(np.random.random() * base_price * 0.1)
                
                # 시간 (과거에서 현재 순서)
                timestamp = now - timedelta(minutes=test_data_count-i)
                
                # 데이터 저장
                self.price_data[symbol].append({
                    "price": test_price,
                    "volume": volume,
                    "timestamp": timestamp
                })
            
            # 중앙 캐시에 테스트 데이터 저장
            await self.data_cache.update_price_history(symbol, list(self.price_data[symbol]))
            
            # 지표 계산
            self._calculate_indicators(symbol)
            logger.log_system(f"{symbol} - 테스트 데이터 {test_data_count}개 생성 및 지표 계산 완료")
            
            return True
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 테스트 데이터 생성 실패")
            return False
    
    async def stop(self):
        """전략 중지"""
        if not self.running:
            return
            
        self.running = False
        logger.log_system("모멘텀 전략 정지 중...")
        
        try:
            # 웹소켓 구독 해제
            for symbol in self.watched_symbols:
                await ws_client.unsubscribe(symbol, "price")
            
            # 전략 루프 태스크 정리
            if hasattr(self, 'strategy_task') and self.strategy_task:
                self.strategy_task.cancel()
                try:
                    await self.strategy_task
                except asyncio.CancelledError:
                    pass
                
            # 실행 중인 포지션 기록 (DB 등에 저장할 수 있음)
            if self.positions:
                logger.log_system(f"모멘텀 전략 정지 시 {len(self.positions)}개 포지션 남아있음")
                
            logger.log_system("모멘텀 전략 정지 완료")
            
        except Exception as e:
            logger.log_error(e, "모멘텀 전략 정지 중 오류")
    
    async def pause(self):
        """전략 일시 중지"""
        if not self.paused:
            self.paused = True
            logger.log_system("모멘텀 전략 일시 중지됨")
        return True

    async def resume(self):
        """전략 재개"""
        if self.paused:
            self.paused = False
            logger.log_system("모멘텀 전략 재개됨")
        return True
    
    async def _handle_price_update(self, data: Dict[str, Any]):
        """실시간 체결가 업데이트 처리"""
        try:
            # 데이터 유효성 검증
            if not data or "tr_key" not in data or "stck_prpr" not in data:
                return
                
            symbol = data.get("tr_key")
            
            # 종목이 감시 대상인지 확인
            if symbol not in self.watched_symbols or symbol not in self.price_data:
                return
                
            # 종목이 일시중지 상태인지 확인
            if self.paused:
                return
                
            # 가격 데이터 추출
            try:
                price = float(data.get("stck_prpr", 0))
                volume = int(data.get("cntg_vol", 0))
                
                # 가격 유효성 검증
                if price <= 0:
                    return
                    
                # 현재 시간
                current_time = datetime.now()
                
                # 가격 데이터 저장
                self.price_data[symbol].append({
                    "price": price,
                    "volume": volume,
                    "timestamp": current_time
                })
                
                # 중앙 캐시 업데이트 (비동기 백그라운드로)
                asyncio.create_task(self.data_cache.update_last_price(
                    symbol, price, volume, current_time
                ))
                
                # 일정 간격마다 지표 업데이트
                last_update = self.last_indicator_update.get(symbol, datetime.min)
                update_interval = self.params["indicator_update_interval"]
                
                if (current_time - last_update).total_seconds() >= update_interval:
                    self._calculate_indicators(symbol)
                    self.last_indicator_update[symbol] = current_time
                
            except (ValueError, TypeError) as e:
                logger.log_debug(f"{symbol} - 웹소켓 데이터 형식 오류: {str(e)}")
                
        except Exception as e:
            logger.log_error(e, f"웹소켓 가격 업데이트 처리 중 오류")
    
    def _calculate_indicators(self, symbol: str) -> bool:
        """기술적 지표 계산"""
        try:
            # 충분한 데이터가 있는지 확인
            min_required = max(
                self.params["rsi_period"],
                self.params["ma_long_period"],
                self.params["macd_slow_period"] + self.params["macd_signal_period"]
            )
            
            if len(self.price_data[symbol]) < min_required:
                return False
            
            # 가격 데이터 추출 - np.array로 변환하여 성능 향상
            prices = np.array([item["price"] for item in self.price_data[symbol]])
            volumes = np.array([item["volume"] for item in self.price_data[symbol]])
            
            # 지표 계산 전 유효성 검사
            if len(prices) < min_required or np.any(np.isnan(prices)) or np.any(np.isinf(prices)):
                logger.log_warning(f"{symbol} - 가격 데이터 유효성 검사 실패")
                return False
            
            # 1. RSI 계산
            rsi = self._calculate_rsi(prices, self.params["rsi_period"])
            
            # 2. 이동평균 계산
            ma_short = self._calculate_ma(prices, self.params["ma_short_period"])
            ma_long = self._calculate_ma(prices, self.params["ma_long_period"])
            
            # 3. MACD 계산
            macd, macd_signal, macd_hist = self._calculate_macd(
                prices, 
                self.params["macd_fast_period"],
                self.params["macd_slow_period"],
                self.params["macd_signal_period"]
            )
            
            # 이전 지표값 백업
            prev_indicators = {
                'rsi': self.indicators[symbol]['rsi'],
                'ma_short': self.indicators[symbol]['ma_short'],
                'ma_long': self.indicators[symbol]['ma_long'],
                'prev_ma_cross': self.indicators[symbol]['prev_ma_cross']
            }
            
            # 새 지표값 저장
            self.indicators[symbol]['prev_rsi'] = prev_indicators['rsi']
            self.indicators[symbol]['rsi'] = rsi
            self.indicators[symbol]['ma_short'] = ma_short
            self.indicators[symbol]['ma_long'] = ma_long
            self.indicators[symbol]['macd'] = macd
            self.indicators[symbol]['macd_signal'] = macd_signal
            self.indicators[symbol]['macd_histogram'] = macd_hist
            self.indicators[symbol]['last_calculation'] = datetime.now()
            
            # 이동평균 교차 상태 업데이트
            if ma_short is not None and ma_long is not None:
                current_cross = ma_short > ma_long
                prev_cross = prev_indicators['prev_ma_cross']
                
                # 교차 상태 변화 감지
                if prev_cross is not None and prev_cross != current_cross:
                    if current_cross:
                        logger.log_system(f"{symbol} - 골든 크로스 감지 (단기: {ma_short:.2f}, 장기: {ma_long:.2f})")
                    else:
                        logger.log_system(f"{symbol} - 데드 크로스 감지 (단기: {ma_short:.2f}, 장기: {ma_long:.2f})")
                
                self.indicators[symbol]['prev_ma_cross'] = current_cross
            
            # 통계 업데이트
            self.performance_stats["indicator_calculations"] += 1
            
            return True
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 지표 계산 중 오류")
            # 오류 발생 시 이전 지표 값 유지
            return False
    
    def _calculate_rsi(self, prices: np.ndarray, period: int) -> Optional[float]:
        """RSI(Relative Strength Index) 계산"""
        try:
            if len(prices) <= period + 1:
                return None
                
            # 가격 변화
            deltas = np.diff(prices[-period-1:])
            
            # 상승/하락 분리
            gain = np.where(deltas > 0, deltas, 0)
            loss = np.where(deltas < 0, -deltas, 0)
            
            # 평균 상승/하락
            avg_gain = np.mean(gain)
            avg_loss = np.mean(loss)
            
            if avg_loss == 0:
                return 100.0
                
            rs = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))
            
            return rsi
            
        except Exception as e:
            logger.log_error(e, "RSI 계산 중 오류")
            return None
    
    def _calculate_ma(self, prices: np.ndarray, period: int) -> Optional[float]:
        """단순 이동평균(Simple Moving Average) 계산"""
        try:
            if len(prices) < period:
                return None
                
            return np.mean(prices[-period:])
            
        except Exception as e:
            logger.log_error(e, "이동평균 계산 중 오류")
            return None
    
    def _calculate_ema(self, prices: np.ndarray, period: int) -> Optional[float]:
        """지수 이동평균(Exponential Moving Average) 계산"""
        try:
            if len(prices) < period:
                return None
                
            # 가중치 계산
            weights = np.exp(np.linspace(-1., 0., period))
            weights /= weights.sum()
            
            # EMA 계산 (convolution)
            ema = np.convolve(prices, weights, mode='valid')
            
            return ema[-1]
            
        except Exception as e:
            logger.log_error(e, "EMA 계산 중 오류")
            return None
    
    def _calculate_macd(self, prices: np.ndarray, fast_period: int, slow_period: int, signal_period: int) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        """MACD(Moving Average Convergence Divergence) 계산"""
        try:
            if len(prices) < slow_period + signal_period:
                return None, None, None
                
            # 빠른 EMA
            ema_fast = self._calculate_ema(prices, fast_period)
            
            # 느린 EMA
            ema_slow = self._calculate_ema(prices, slow_period)
            
            if ema_fast is None or ema_slow is None:
                return None, None, None
                
            # MACD 라인
            macd_line = ema_fast - ema_slow
            
            # 시그널 라인
            # MACD 이력을 계산하여 signal_period 기간의 EMA 적용
            macd_history = []
            for i in range(signal_period):
                idx = len(prices) - slow_period - signal_period + i
                if idx >= 0:
                    fast_ema = self._calculate_ema(prices[:idx+1], fast_period)
                    slow_ema = self._calculate_ema(prices[:idx+1], slow_period)
                    if fast_ema is not None and slow_ema is not None:
                        macd_history.append(fast_ema - slow_ema)
            
            if len(macd_history) < signal_period:
                return macd_line, None, None
                
            signal_line = np.mean(macd_history)
            
            # MACD 히스토그램
            histogram = macd_line - signal_line
            
            return macd_line, signal_line, histogram
            
        except Exception as e:
            logger.log_error(e, "MACD 계산 중 오류")
            return None, None, None
    
    async def _strategy_loop(self):
        """전략 실행 루프"""
        try:
            sleep_count = 0
            
            while self.running:
                try:
                    # 장 시간 체크
                    current_time = datetime.now().time()
                    is_market_hours = time(9, 0) <= current_time <= time(15, 30)
                    
                    # 장 시간이 아닌 경우
                    if not is_market_hours:
                        # 불필요한 로그 감소 (10분마다만 로그)
                        if sleep_count % 600 == 0:
                            logger.log_system(f"모멘텀 전략: 장 시간이 아님 (현재 {current_time})")
                        
                        sleep_count += 1
                        await asyncio.sleep(1)
                        continue
                    
                    # 전략이 일시 중지된 경우
                    if self.paused or (order_manager and order_manager.is_trading_paused()):
                        await asyncio.sleep(1)
                        continue
                    
                    # 분석할 종목 무작위 선택 (매 초마다 최대 5개 종목 처리)
                    symbols_to_analyze = list(self.watched_symbols)
                    if len(symbols_to_analyze) > 5:
                        # 랜덤 샘플링하여 분석 종목 선택
                        import random
                        symbols_to_analyze = random.sample(symbols_to_analyze, 5)
                    
                    # 각 종목 분석 및 거래 판단
                    for symbol in symbols_to_analyze:
                        await self._analyze_and_trade(symbol)
                        # 과도한 API 호출 방지를 위한 짧은 딜레이
                        await asyncio.sleep(0.1)
                    
                    # 포지션 모니터링 (5초마다)
                    if datetime.now().second % 5 == 0:
                        await self._monitor_positions()
                    
                    # 다음 주기까지 대기
                    await asyncio.sleep(1)
                    
                except asyncio.CancelledError:
                    logger.log_system("모멘텀 전략 루프 취소됨")
                    break
                    
                except Exception as loop_error:
                    logger.log_error(loop_error, "모멘텀 전략 루프 내부 오류")
                    # 오류 발생 시 더 긴 대기
                    await asyncio.sleep(5)
                    
        except asyncio.CancelledError:
            logger.log_system("모멘텀 전략 루프 태스크 취소됨")
        except Exception as e:
            logger.log_error(e, "모멘텀 전략 루프 예외 발생")
    
    async def _analyze_and_trade(self, symbol: str):
        """종목 분석 및 거래"""
        try:
            # 전략이 일시 중지 상태인지 확인
            if self.paused or order_manager.is_trading_paused():
                return
                
            # 데이터 유효성 검사
            valid_data = await self._ensure_valid_data(symbol)
            if not valid_data:
                return
                
            # 이미 포지션 있는지 확인
            symbol_positions = self._get_symbol_positions(symbol)
            if len(symbol_positions) >= self.params.get("max_positions", 3):
                return
            
            # 현재가 가져오기
            current_price = self._get_current_price(symbol)
            if current_price <= 0:
                return
            
            # 최신 지표 계산
            self._calculate_indicators(symbol)
            
            # 신호 계산
            signal_info = self._calculate_signal_strength(symbol)
            
            # 신호 업데이트
            self.signals[symbol]["strength"] = signal_info["signal"]
            self.signals[symbol]["direction"] = signal_info["direction"]
            self.signals[symbol]["last_update"] = datetime.now()
            self.signals[symbol]["components"] = {
                "rsi": signal_info["rsi_signal"],
                "ma": signal_info["ma_signal"],
                "macd": signal_info["macd_signal"]
            }
            
            # 매매 결정
            signal_strength = signal_info["signal"]
            signal_direction = signal_info["direction"]
            
            # 강한 신호일 경우 거래 수행
            if signal_strength >= 7.0:  # 7.0 이상만 실행 (0-10 척도)
                if signal_direction == "BUY":
                    await self._enter_position(
                        symbol, "BUY", current_price, 
                        f"momentum_buy_signal_{signal_strength:.1f}"
                    )
                elif signal_direction == "SELL":
                    await self._enter_position(
                        symbol, "SELL", current_price, 
                        f"momentum_sell_signal_{signal_strength:.1f}"
                    )
                
        except Exception as e:
            logger.log_error(e, f"{symbol} - 모멘텀 분석 중 오류")
    
    async def _ensure_valid_data(self, symbol: str) -> bool:
        """데이터 유효성 검사 및 문제 해결"""
        try:
            # 최근 검사 결과 캐싱 활용 (10초 이내의 이전 검사 재사용)
            if symbol in self.data_validity_cache:
                timestamp, is_valid = self.data_validity_cache[symbol]
                if (datetime.now() - timestamp).total_seconds() < 10:
                    return is_valid
            
            # 1. 감시 종목인지 확인
            if symbol not in self.watched_symbols:
                self.data_validity_cache[symbol] = (datetime.now(), False)
                return False
                
            # 2. 가격 데이터 존재 확인
            if symbol not in self.price_data or not self.price_data[symbol]:
                # 데이터 로드 시도
                loaded = await self._load_initial_data(symbol)
                self.data_validity_cache[symbol] = (datetime.now(), loaded)
                return loaded
                
            # 3. 최소 필요 데이터 확인
            min_required_data = max(
                self.params["rsi_period"], 
                self.params["ma_long_period"]
            )
            
            if len(self.price_data[symbol]) < min_required_data:
                logger.log_debug(f"{symbol} - 모멘텀 전략에 필요한 충분한 데이터가 없음: "
                                f"{len(self.price_data[symbol])}/{min_required_data}")
                self.data_validity_cache[symbol] = (datetime.now(), False)
                return False
                
            # 4. 지표 계산 확인
            if symbol not in self.indicators or not self.indicators[symbol].get('rsi'):
                # 지표 계산 시도
                calculated = self._calculate_indicators(symbol)
                self.data_validity_cache[symbol] = (datetime.now(), calculated)
                return calculated
            
            # 모든 검사 통과
            self.data_validity_cache[symbol] = (datetime.now(), True)
            return True
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 데이터 유효성 검사 중 오류")
            self.data_validity_cache[symbol] = (datetime.now(), False)
            return False
    
    def _get_current_price(self, symbol: str) -> float:
        """현재가 가져오기 (캐시 및 웹소켓 데이터 활용)"""
        try:
            # 1. 로컬 가격 데이터 확인
            if symbol in self.price_data and self.price_data[symbol]:
                current_price = self.price_data[symbol][-1]["price"]
                if current_price > 0:
                    return current_price
            
            # 2. 중앙 캐시 확인
            cache_price = self.data_cache.get_last_price(symbol)
            if cache_price and cache_price > 0:
                return cache_price
            
            # 가격 정보가 없음
            return 0.0
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 현재가 조회 중 오류")
            return 0.0
    
    def _get_symbol_positions(self, symbol: str) -> List[str]:
        """특정 종목의 포지션 ID 목록 반환"""
        return [
            position_id for position_id, position in self.positions.items()
            if position["symbol"] == symbol
        ]
    
    def _calculate_signal_strength(self, symbol: str) -> Dict[str, Any]:
        """모멘텀 신호 강도 및 방향 계산"""
        try:
            # 통계 업데이트
            self.performance_stats["signal_calculations"] += 1
            
            # 지표 데이터 유효성 확인
            indicators = self.indicators.get(symbol, {})
            if not indicators or indicators.get('rsi') is None:
                return {
                    "signal": 0, 
                    "direction": "NEUTRAL",
                    "rsi": 0,
                    "rsi_signal": 0,
                    "ma_signal": 0,
                    "macd_signal": 0
                }
            
            # 기본 변수 설정
            direction = "NEUTRAL"
            signal_strength = 0
            
            # 지표 추출
            rsi = indicators['rsi']
            rsi_buy = self.params["rsi_buy_threshold"]
            rsi_sell = self.params["rsi_sell_threshold"]
            ma_short = indicators['ma_short']
            ma_long = indicators['ma_long']
            ma_cross = indicators.get('prev_ma_cross', False)
            macd = indicators['macd']
            macd_signal = indicators['macd_signal']
            
            # 가중치 설정
            weights = {
                "rsi": 0.4,      # RSI 가중치 40%
                "ma_cross": 0.3, # 이동평균 교차 가중치 30%
                "macd": 0.3      # MACD 가중치 30%
            }
            
            # 리스크 팩터
            risk_factor = self.params["risk_factor"]
            
            # 1. RSI 신호 계산
            rsi_signal = 0
            rsi_direction = "NEUTRAL"
            
            if rsi is not None:
                if rsi < rsi_buy:
                    rsi_direction = "BUY"
                    rsi_signal = (rsi_buy - rsi) / rsi_buy * 10 * risk_factor  # 0-10+ 점수
                elif rsi > rsi_sell:
                    rsi_direction = "SELL"
                    rsi_signal = (rsi - rsi_sell) / (100 - rsi_sell) * 10 * risk_factor  # 0-10+ 점수
            
            # 2. 이동평균 신호 계산
            ma_signal = 0
            ma_direction = "NEUTRAL"
            
            if ma_short is not None and ma_long is not None:
                if ma_short > ma_long:  # 골든 크로스
                    ma_direction = "BUY"
                    ma_diff_pct = ((ma_short/ma_long)-1)*100 if ma_long > 0 else 0
                    ma_signal = min(10, ma_diff_pct * 5 * risk_factor)  # 0-10 점수
                else:  # 데드 크로스
                    ma_direction = "SELL"
                    ma_diff_pct = ((ma_long/ma_short)-1)*100 if ma_short > 0 else 0
                    ma_signal = min(10, ma_diff_pct * 5 * risk_factor)  # 0-10 점수
            
            # 3. MACD 신호 계산
            macd_signal_value = 0
            macd_direction = "NEUTRAL"
            
            if macd is not None and macd_signal is not None:
                if macd > macd_signal:
                    macd_direction = "BUY"
                    macd_diff = abs(macd - macd_signal)
                    macd_signal_value = min(10, macd_diff * 20 * risk_factor if abs(macd_signal) > 0 else 2 * risk_factor)
                else:
                    macd_direction = "SELL"
                    macd_diff = abs(macd_signal - macd)
                    macd_signal_value = min(10, macd_diff * 20 * risk_factor if abs(macd_signal) > 0 else 2 * risk_factor)
            
            # 4. 종합 신호 계산 (가중 평균)
            # 각 지표의 방향이 일치하는 경우 신호 강화
            if rsi_direction == ma_direction == macd_direction and rsi_direction != "NEUTRAL":
                # 방향 일치 시 보너스
                direction = rsi_direction
                # 가중 평균 + 보너스
                signal_strength = (
                    rsi_signal * weights["rsi"] +
                    ma_signal * weights["ma_cross"] +
                    macd_signal_value * weights["macd"]
                ) * 1.2  # 20% 보너스
            else:
                # 가장 강한 신호의 방향 선택
                signals = [
                    (rsi_signal * weights["rsi"], rsi_direction),
                    (ma_signal * weights["ma_cross"], ma_direction),
                    (macd_signal_value * weights["macd"], macd_direction)
                ]
                
                max_signal = max(signals, key=lambda x: x[0] if x[1] != "NEUTRAL" else 0)
                
                if max_signal[0] > 0:
                    direction = max_signal[1]
                    # 가중 평균
                    signal_strength = (
                        rsi_signal * weights["rsi"] +
                        ma_signal * weights["ma_cross"] +
                        macd_signal_value * weights["macd"]
                    )
            
            # 5. 최종 신호 강도 제한 (0-10)
            signal_strength = min(10, max(0, signal_strength))
            
            # 상세 정보 포함하여 반환
            return {
                "signal": signal_strength, 
                "direction": direction,
                "rsi": round(rsi, 1) if rsi is not None else 0,
                "rsi_signal": round(rsi_signal, 1),
                "ma_cross": "GOLDEN" if ma_cross else "DEAD" if ma_short is not None and ma_long is not None else "NONE",
                "ma_signal": round(ma_signal, 1),
                "ma_diff_pct": f"{((ma_short/ma_long)-1)*100:.2f}%" if ma_short is not None and ma_long is not None and ma_long > 0 else "N/A",
                "macd_signal": round(macd_signal_value, 1) if macd is not None else 0
            }
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 신호 강도 계산 중 오류")
            # 기본값 반환
            return {
                "signal": 0, 
                "direction": "NEUTRAL",
                "rsi": 0,
                "rsi_signal": 0,
                "ma_signal": 0,
                "macd_signal": 0
            }
    
    async def _enter_position(self, symbol: str, side: str, current_price: float, reason: str):
        """포지션 진입"""
        try:
            # 주문 수량 계산
            position_size = self.params.get("position_size", 1000000)  # 100만원
            
            # 리스크 수준에 따른 포지션 크기 조정
            risk_level = self._calculate_market_risk(symbol)
            adjusted_position = position_size * (1 - risk_level * 0.5)  # 리스크에 따라 50%까지 축소 가능
            
            # 주문 수량 계산
            quantity = int(adjusted_position / current_price)
            
            # 수량이 0인 경우 무시
            if quantity <= 0:
                logger.log_system(f"{symbol} - 주문 수량이 0이하 ({quantity}), 주문 취소")
                return
            
            # 주문 실행
            try:
                result = await order_manager.place_order(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    order_type="MARKET",  # 시장가 주문
                    strategy="momentum",
                    reason=reason
                )
            except Exception as order_error:
                logger.log_error(order_error, f"{symbol} - 주문 실행 중 오류")
                self.performance_stats["failed_trades"] += 1
                return
            
            # 주문 결과 처리
            if result and result.get("status") == "success":
                # 주문 성공
                self.performance_stats["successful_trades"] += 1
                
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
                    "reason": reason,
                    # 트레일링 스탑 관련 필드
                    "original_stop": stop_price,
                    "trailing_activated": False,
                    "highest_price": current_price if side == "BUY" else None,
                    "lowest_price": current_price if side == "SELL" else None
                }
                
                # 로그 기록
                logger.log_system(
                    f"모멘텀: {symbol} {side} 포지션 진입 - 가격: {current_price:,.0f}, "
                    f"수량: {quantity}, 손절: {stop_price:,.0f}, 익절: {target_price:,.0f}, "
                    f"이유: {reason}"
                )
                
                # 알림 전송
                asyncio.create_task(alert_system.notify_trade(
                    symbol=symbol,
                    side=side,
                    price=current_price,
                    quantity=quantity,
                    strategy="momentum",
                    reason=reason
                ))
                
            else:
                # 주문 실패
                error_reason = result.get("reason", "알 수 없는 오류") if result else "주문 결과가 없음"
                logger.log_system(f"모멘텀: {symbol} {side} 주문 실패 - {error_reason}")
                self.performance_stats["failed_trades"] += 1
                
        except Exception as e:
            logger.log_error(e, f"{symbol} - 포지션 진입 중 오류")
            self.performance_stats["failed_trades"] += 1
    
    def _calculate_market_risk(self, symbol: str) -> float:
        """시장 리스크 수준 계산 (0-1)"""
        try:
            # 현재 지표 확인
            indicators = self.indicators.get(symbol, {})
            
            # 지표가 없으면 중간 리스크 반환
            if not indicators or not indicators.get('rsi'):
                return 0.5
            
            # RSI 확인 - 극단치에 가까울수록 리스크 증가
            rsi = indicators.get('rsi', 50)
            rsi_risk = 0
            
            if rsi <= 30:
                # 과매도 상태는 리스크 낮음 (매수 관점)
                rsi_risk = 0.3
            elif rsi >= 70:
                # 과매수 상태는 리스크 높음 (매수 관점)
                rsi_risk = 0.7
            else:
                # 중간 상태는 중간 리스크
                rsi_risk = 0.5
            
            # MACD 확인
            macd = indicators.get('macd')
            macd_signal = indicators.get('macd_signal')
            macd_risk = 0.5  # 기본값
            
            if macd is not None and macd_signal is not None:
                macd_diff = macd - macd_signal
                
                if macd_diff > 0 and macd > 0:
                    # 강한 상승 추세 (지속성 있을 수 있으나 고점 위험)
                    macd_risk = 0.4
                elif macd_diff > 0 and macd < 0:
                    # 반등 신호 (리스크 낮음)
                    macd_risk = 0.3
                elif macd_diff < 0 and macd > 0:
                    # 상승 추세 약화 (리스크 높음)
                    macd_risk = 0.7
                elif macd_diff < 0 and macd < 0:
                    # 하락 추세 (높은 리스크)
                    macd_risk = 0.6
            
            # 종합 리스크 (가중치 적용)
            combined_risk = rsi_risk * 0.6 + macd_risk * 0.4
            
            return combined_risk
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 리스크 계산 중 오류")
            return 0.5  # 오류 시 중간 리스크 반환
    
    async def _monitor_positions(self):
        """포지션 모니터링"""
        try:
            # 모니터링할 포지션이 없으면 건너뜀
            if not self.positions:
                return
                
            for position_id, position in list(self.positions.items()):
                symbol = position["symbol"]
                
                # 현재 가격 확인
                if symbol not in self.price_data or not self.price_data[symbol]:
                    continue
                
                current_price = self.price_data[symbol][-1]["price"]
                if current_price <= 0:
                    continue
                    
                side = position["side"]
                entry_time = position["entry_time"]
                entry_price = position["entry_price"]
                stop_price = position["stop_price"]
                target_price = position["target_price"]
                
                # 포지션 보유 시간
                hold_time_minutes = (datetime.now() - entry_time).total_seconds() / 60
                
                # 트레일링 스탑 업데이트
                if self.params["use_trailing_stop"]:
                    self._update_trailing_stop(position, current_price)
                
                # 현재 수익률 계산
                if side == "BUY":
                    profit_pct = (current_price - entry_price) / entry_price * 100
                else:  # SELL
                    profit_pct = (entry_price - current_price) / entry_price * 100
                
                # 청산 조건 확인
                should_exit = False
                exit_reason = ""
                
                # RSI 확인 (익절 시점에 활용)
                indicators = self.indicators.get(symbol, {})
                rsi = indicators.get('rsi')
                
                # 손절/익절 조건 확인
                if side == "BUY":
                    # 매수 포지션
                    if current_price <= stop_price:
                        should_exit = True
                        exit_reason = "stop_loss"
                    elif current_price >= target_price:
                        should_exit = True
                        exit_reason = "take_profit"
                    # RSI 기반 익절 - RSI가 과매수 구간(70)에 들어가면 일부 익절
                    elif rsi is not None and rsi > self.params["rsi_sell_threshold"] and profit_pct > 1.0:
                        should_exit = True
                        exit_reason = "rsi_overbought"
                        
                else:  # SELL
                    # 매도 포지션
                    if current_price >= stop_price:
                        should_exit = True
                        exit_reason = "stop_loss"
                    elif current_price <= target_price:
                        should_exit = True
                        exit_reason = "take_profit"
                    # RSI 기반 익절 - RSI가 과매도 구간(30)에 들어가면 일부 익절
                    elif rsi is not None and rsi < self.params["rsi_buy_threshold"] and profit_pct > 1.0:
                        should_exit = True
                        exit_reason = "rsi_oversold"
                
                # 시간 기반 청산 (2시간 이상 보유)
                if hold_time_minutes >= 120:
                    should_exit = True
                    exit_reason = "time_limit"
                
                # 시그널 변화 기반 청산
                current_signal = self.signals.get(symbol, {}).get("direction", "NEUTRAL")
                if current_signal != "NEUTRAL" and current_signal != side:
                    # 반대 방향 신호면서 충분히 강한 경우
                    signal_strength = self.signals.get(symbol, {}).get("strength", 0)
                    if signal_strength >= 7.0:  # 강한 반대 신호
                        should_exit = True
                        exit_reason = "signal_reversal"
                
                # 청산 실행
                if should_exit:
                    await self._exit_position(position_id, exit_reason, current_price, profit_pct)
                
        except Exception as e:
            logger.log_error(e, "포지션 모니터링 중 오류")
    
    def _update_trailing_stop(self, position: Dict[str, Any], current_price: float):
        """트레일링 스탑 업데이트"""
        try:
            if not self.params["use_trailing_stop"]:
                return
                
            side = position["side"]
            original_stop = position["original_stop"]
            trailing_pct = self.params.get("trailing_stop_pct", 0.01)  # 기본값 1%
            
            # 추적 가격 업데이트
            if side == "BUY":
                # 매수 포지션인 경우 최고가 추적
                if position["highest_price"] is None or current_price > position["highest_price"]:
                    position["highest_price"] = current_price
                    
                # 이익 중이면 트레일링 스탑 활성화
                entry_price = position["entry_price"]
                if current_price > entry_price * 1.01:  # 1% 이상 이익일 때 활성화
                    position["trailing_activated"] = True
                
                # 트레일링 스탑이 활성화된 경우
                if position["trailing_activated"]:
                    # 새 손절가 = 최고가 - (최고가 * 트레일링 비율)
                    new_stop = position["highest_price"] * (1 - trailing_pct)
                    
                    # 기존 손절가보다 높을 경우에만 업데이트
                    if new_stop > position["stop_price"]:
                        # 손절가 업데이트
                        position["stop_price"] = new_stop
                        
            else:  # SELL
                # 매도 포지션인 경우 최저가 추적
                if position["lowest_price"] is None or current_price < position["lowest_price"]:
                    position["lowest_price"] = current_price
                    
                # 이익 중이면 트레일링 스탑 활성화
                entry_price = position["entry_price"]
                if current_price < entry_price * 0.99:  # 1% 이상 이익일 때 활성화
                    position["trailing_activated"] = True
                
                # 트레일링 스탑이 활성화된 경우
                if position["trailing_activated"]:
                    # 새 손절가 = 최저가 + (최저가 * 트레일링 비율)
                    new_stop = position["lowest_price"] * (1 + trailing_pct)
                    
                    # 기존 손절가보다 낮을 경우에만 업데이트
                    if new_stop < position["stop_price"]:
                        # 손절가 업데이트
                        position["stop_price"] = new_stop
                    
        except Exception as e:
            logger.log_error(e, "트레일링 스탑 업데이트 중 오류")
    
    async def _exit_position(self, position_id: str, reason: str, current_price: float = None, profit_pct: float = None):
        """포지션 청산"""
        try:
            # 포지션 정보 확인
            if position_id not in self.positions:
                logger.log_warning(f"포지션 ID {position_id} 정보 없음, 청산 불가")
                return
                
            position = self.positions[position_id]
            symbol = position["symbol"]
            exit_side = "SELL" if position["side"] == "BUY" else "BUY"
            
            # 현재가 확인
            if current_price is None or current_price <= 0:
                if symbol in self.price_data and self.price_data[symbol]:
                    current_price = self.price_data[symbol][-1]["price"]
                else:
                    logger.log_warning(f"{symbol} - 현재가 정보 없음, 청산 계속 진행")
                    current_price = position["entry_price"]  # 임시 대체
            
            # 수익률 계산
            if profit_pct is None:
                entry_price = position["entry_price"]
                if position["side"] == "BUY":
                    profit_pct = (current_price - entry_price) / entry_price * 100
                else:
                    profit_pct = (entry_price - current_price) / entry_price * 100
            
            # 주문 실행
            try:
                result = await order_manager.place_order(
                    symbol=symbol,
                    side=exit_side,
                    quantity=position["quantity"],
                    order_type="MARKET",
                    strategy="momentum",
                    reason=reason
                )
            except Exception as order_error:
                logger.log_error(order_error, f"{symbol} - 청산 주문 실행 중 오류")
                self.performance_stats["failed_trades"] += 1
                # 에러 발생해도 포지션 정보는 유지
                return
            
            # 주문 결과 처리
            if result and result.get("status") == "success":
                # 청산 성공
                self.performance_stats["successful_trades"] += 1
                
                # 포지션 제거
                del self.positions[position_id]
                
                # 로그 기록
                logger.log_system(
                    f"모멘텀: {symbol} {position['side']} 포지션 청산 완료 - 진입: {position['entry_price']:,.0f}, "
                    f"청산: {current_price:,.0f}, 손익: {profit_pct:.2f}%, 사유: {reason}"
                )
                
                # 알림 전송
                asyncio.create_task(alert_system.notify_trade(
                    symbol=symbol,
                    side=exit_side,
                    price=current_price,
                    quantity=position["quantity"],
                    strategy="momentum",
                    reason=f"청산: {reason}",
                    profit_pct=f"{profit_pct:.2f}%"
                ))
                
            else:
                # 청산 실패
                error_reason = result.get("reason", "알 수 없는 오류") if result else "주문 결과가 없음"
                logger.log_system(f"모멘텀: {symbol} {exit_side} 청산 실패 - {error_reason}")
                self.performance_stats["failed_trades"] += 1
                
        except Exception as e:
            logger.log_error(e, f"포지션 {position_id} 청산 중 오류")
            # 에러 발생해도 포지션 정보는 유지
    
    async def get_signal(self, symbol: str) -> Dict[str, Any]:
        """전략 신호 반환 (combined_strategy에서 호출)"""
        try:
            # 1. 데이터 유효성 검증
            valid_data = await self._ensure_valid_data(symbol)
            if not valid_data:
                return {"signal": 0, "direction": "NEUTRAL", "reason": "insufficient_data"}
                
            # 2. 지표 계산 (필요시에만)
            if not self._has_valid_indicators(symbol):
                self._calculate_indicators(symbol)
                
            # 3. 신호 계산
            signal_info = self._calculate_signal_strength(symbol)
                
            # 4. 신호 정보 저장 및 반환
            self.signals[symbol] = {
                "strength": signal_info["signal"],
                "direction": signal_info["direction"],
                "last_update": datetime.now(),
                "components": {
                    "rsi": signal_info["rsi_signal"],
                    "ma": signal_info["ma_signal"],
                    "macd": signal_info["macd_signal"]
                }
            }
            
            # 5. 결과 반환 (combined_strategy에서 사용할 형식)
            return {
                "signal": signal_info["signal"],
                "direction": signal_info["direction"],
                "rsi": signal_info["rsi"],
                "rsi_signal": signal_info["rsi_signal"],
                "ma_cross": signal_info["ma_cross"],
                "ma_signal": signal_info["ma_signal"],
                "macd_signal": signal_info["macd_signal"]
            }
                
        except Exception as e:
            logger.log_error(e, f"{symbol} - 모멘텀 신호 계산 오류")
            trace = traceback.format_exc()
            logger.log_debug(f"스택 트레이스: {trace}")
            return {"signal": 0, "direction": "NEUTRAL", "reason": "error"}
            
    def _has_valid_indicators(self, symbol: str) -> bool:
        """유효한 지표가 있는지 확인"""
        indicators = self.indicators.get(symbol, {})
        
        # 필수 지표 확인
        has_valid = (
            indicators and 
            indicators.get('rsi') is not None and 
            indicators.get('ma_short') is not None and 
            indicators.get('ma_long') is not None
        )
        
        # 최근 계산 시점 확인 (1분 내 계산되었는지)
        if has_valid and indicators.get('last_calculation'):
            last_calc = indicators['last_calculation']
            if (datetime.now() - last_calc).total_seconds() <= 60:
                return True
            # 오래된 지표면 재계산 필요
            return False
            
        return has_valid
    
    async def update_symbols(self, new_symbols: List[str]):
        """종목 목록 업데이트"""
        try:
            logger.log_system(f"모멘텀 전략: 종목 목록 업데이트 시작 ({len(new_symbols)}개)")
            
            # 새로운 종목 집합
            new_set = set(new_symbols)
            
            # 현재 감시 중인 종목 집합
            current_set = self.watched_symbols
            
            # 제거할 종목들
            to_remove = current_set - new_set
            
            # 추가할 종목들
            to_add = new_set - current_set
            
            # 제거 작업
            for symbol in to_remove:
                # 웹소켓 구독 해제
                await ws_client.unsubscribe(symbol, "price")
                
                # 데이터 정리
                if symbol in self.price_data:
                    del self.price_data[symbol]
                if symbol in self.indicators:
                    del self.indicators[symbol]
                if symbol in self.signals:
                    del self.signals[symbol]
                if symbol in self.last_indicator_update:
                    del self.last_indicator_update[symbol]
                if symbol in self.data_validity_cache:
                    del self.data_validity_cache[symbol]
            
            # 신규 종목 초기화
            initialization_tasks = []
            for symbol in to_add:
                initialization_tasks.append(
                    self._initialize_symbol_data(symbol)
                )
            
            # 모든 초기화 병렬 실행
            if initialization_tasks:
                await asyncio.gather(*initialization_tasks, return_exceptions=True)
            
            # 감시 종목 업데이트
            self.watched_symbols = new_set
            
            # 완료 로그
            logger.log_system(
                f"모멘텀 전략: 종목 목록 업데이트 완료 - "
                f"추가: {len(to_add)}개, 제거: {len(to_remove)}개, 총: {len(self.watched_symbols)}개"
            )
        
        except Exception as e:
            logger.log_error(e, "모멘텀 전략 종목 업데이트 오류")

# 싱글톤 인스턴스
momentum_strategy = MomentumStrategy()