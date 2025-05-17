"""
갭 트레이딩 전략 (Gap Trading Strategy)
장 시작 시 전일 종가와 당일 시가의 차이(갭)를 활용하는 전략
"""
import asyncio
import threading
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, time, timedelta
from collections import deque
import numpy as np
import traceback

from config.settings import config
from core.api_client import api_client
from core.websocket_client import ws_client
from core.order_manager import order_manager
from utils.logger import logger
from monitoring.alert_system import alert_system
from core.data_cache import data_cache  # 중앙화된 데이터 캐싱 서비스 추가

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
            # 기본 파라미터 설정
            self.params = {
                "min_gap_pct": 0.007,        # 최소 갭 비율 (0.7%)
                "max_gap_pct": 0.05,         # 최대 갭 비율 (5%)
                "gap_fill_pct": 0.75,        # 갭 채움 목표 비율 (75%)
                "volume_threshold": 1.5,     # 거래량 임계치 (평균 대비)
                "stop_loss_pct": 0.5,        # 추가 갭 확대 시 손절 비율 (갭의 50%)
                "max_positions": 3,          # 최대 포지션 개수
                "position_size": 1000000,    # 기본 포지션 크기 (100만원)
                "skip_news_symbols": True,   # 뉴스 있는 종목 스킵 여부
                "hold_time_minutes": 120,    # 최대 보유 시간 (분)
                "allow_short": False,        # 숏 포지션 허용 여부 (기본값: 비허용)
                "use_trailing_stop": True,   # 트레일링 스탑 사용 여부
                "trailing_pct": 0.005,       # 트레일링 스탑 비율 (0.5%)
                "minimum_profit_pct": 0.01,  # 트레일링 적용 최소 수익률 (1%)
                "retry_attempts": 3,         # API 재시도 횟수
                "retry_delay": 1.0,          # API 재시도 간격(초)
                "min_data_points": 5,        # 최소 필요 데이터 포인트
                "price_update_interval": 3,  # 가격 업데이트 간격(초)
                "check_interval": 1.0        # 주기적 체크 간격(초)
            }
            
            # 설정에 gap_params가 있으면 업데이트
            if hasattr(config["trading"], "gap_params"):
                self.params.update(config["trading"].gap_params)
                
            # 상태 변수 초기화
            self.running = False
            self.paused = False
            self.watched_symbols = set()
            self.price_data = {}           # {symbol: deque of price data}
            self.gap_data = {}             # {symbol: {'gap_pct': float, 'direction': str, 'prev_close': float}}
            self.positions = {}            # {position_id: position_data}
            self.volume_data = {}          # {symbol: {'avg_volume': float, 'volume_ratio': float}}
            
            # 최근 데이터 업데이트 시간 추적
            self.last_update = {}          # {symbol: datetime}
            
            # 데이터 캐시 참조 (중앙화된 데이터 관리)
            self.data_cache = data_cache
            
            # 성능 측정 통계
            self.performance_stats = {
                "signal_calculations": 0,
                "successful_trades": 0,
                "failed_trades": 0,
                "api_calls": 0,
                "api_errors": 0
            }
            
            # 데이터 유효성 캐시
            self.data_validity_cache = {}  # {symbol: (timestamp, is_valid)}
            
            self._initialized = True
            logger.log_system("갭 트레이딩 전략 초기화 완료")
        
    def _safe_get(self, data, *keys, default=None):
        """중첩 딕셔너리에서 안전하게 값 가져오기"""
        current = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return default
            current = current[key]
        return current

    def _safe_float(self, value, default=0.0):
        """안전하게 float로 변환"""
        try:
            if value is None:
                return default
            if isinstance(value, str):
                # 쉼표 제거 후 변환
                value = value.replace(',', '')
            return float(value)
        except (ValueError, TypeError):
            return default

    def _safe_int(self, value, default=0):
        """안전하게 int로 변환"""
        try:
            if value is None:
                return default
            if isinstance(value, str):
                # 쉼표 제거 후 변환
                value = value.replace(',', '')
            return int(float(value))  # float 변환 후 int로 변환 (소수점 처리)
        except (ValueError, TypeError):
            return default

    def _safe_divide(self, numerator, denominator, default=0.0):
        """안전한 나눗셈 (분모가 0일 경우 기본값 반환)"""
        try:
            if denominator == 0 or denominator is None:
                return default
            return numerator / denominator
        except (ZeroDivisionError, TypeError):
            return default

    async def start(self, symbols: List[str]):
        """전략 시작"""
        try:
            self.running = True
            self.paused = False
            self.watched_symbols = set(symbols)
            
            # 상태 로깅
            logger.log_system(f"갭 트레이딩 전략 시작: {len(symbols)}개 종목")
            
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
                        logger.log_error(result, f"{symbol} 갭 전략 초기화 실패")
            
            # 전략 실행 루프 시작
            self.strategy_task = asyncio.create_task(self._strategy_loop())
            logger.log_system(f"갭 트레이딩 전략 루프 시작됨 - {len(self.watched_symbols)}개 종목 감시 중")
            
        except Exception as e:
            logger.log_error(e, "갭 트레이딩 전략 시작 실패")
            await alert_system.notify_error(e, "갭 전략 시작 오류")
            self.running = False
    
    async def _initialize_symbol_data(self, symbol: str) -> bool:
        """개별 종목 데이터 초기화"""
        try:
            # 데이터 구조 초기화
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
            
            # 마지막 업데이트 시간 초기화
            self.last_update[symbol] = datetime.now() - timedelta(minutes=5)
            
            # 중앙 캐시에 종목 등록
            await self.data_cache.register_symbol(symbol, "gap")
            
            # 웹소켓 구독
            await ws_client.subscribe_price(symbol, self._handle_price_update)
            
            # 전일 종가 및 거래량 데이터 로드
            await self._load_historical_data(symbol)
            
            return True
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 갭 전략 종목 초기화 실패")
            # 실패해도 감시 종목에는 등록
            self.watched_symbols.add(symbol)
            return False
    
    async def _load_initial_data(self, symbol: str) -> bool:
        """초기 데이터 로딩"""
        try:
            logger.log_system(f"갭 전략 - {symbol} 초기 데이터 로딩 시작")
            
            # 초기화되지 않은 경우를 대비한 기본 데이터 구조 설정
            self._ensure_data_structures(symbol)
            
            # 1. 중앙 캐시에서 데이터 확인
            cached_price = self.data_cache.get_last_price(symbol)
            if cached_price:
                # 가격 데이터에 추가
                self.price_data[symbol].append({
                    "price": cached_price,
                    "volume": 0,
                    "timestamp": datetime.now()
                })
                logger.log_system(f"갭 전략 - {symbol} 캐시에서 현재가 로드: {cached_price:,.0f}원")
            else:
                # 2. API에서 현재가 정보 조회
                price_info = await self._fetch_symbol_info(symbol)
                if price_info and "current_price" in price_info:
                    current_price = self._safe_float(price_info["current_price"])
                    
                    # 가격 데이터에 추가
                    self.price_data[symbol].append({
                        "price": current_price,
                        "volume": self._safe_int(price_info.get("volume", 0)),
                        "timestamp": datetime.now()
                    })
                    logger.log_system(f"갭 전략 - {symbol} API에서 현재가 로드: {current_price:,.0f}원")
                    
                    # 중앙 캐시 업데이트
                    await self.data_cache.update_last_price(
                        symbol, current_price, 0, datetime.now()
                    )
            
            # 3. 일봉 데이터 조회 및 처리
            await self._process_daily_data(symbol)
            
            return True
            
        except Exception as e:
            logger.log_error(e, f"갭 전략 - {symbol} 초기 데이터 로딩 오류")
            return False
    
    async def _fetch_symbol_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """종목 정보 조회 (재시도 로직 포함)"""
        max_attempts = self.params["retry_attempts"]
        retry_delay = self.params["retry_delay"]
        
        for attempt in range(max_attempts):
            try:
                self.performance_stats["api_calls"] += 1
                symbol_info = await asyncio.wait_for(
                    api_client.get_symbol_info(symbol),
                    timeout=5.0
                )
                return symbol_info
            except asyncio.TimeoutError:
                logger.log_warning(f"{symbol} - API 타임아웃 (시도 {attempt+1}/{max_attempts})")
                self.performance_stats["api_errors"] += 1
                
                if attempt < max_attempts - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                return None
                
            except Exception as e:
                logger.log_error(e, f"{symbol} - API 호출 중 오류 (시도 {attempt+1}/{max_attempts})")
                self.performance_stats["api_errors"] += 1
                
                if attempt < max_attempts - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                return None
                
        return None
    
    def _ensure_data_structures(self, symbol: str):
        """종목 데이터 구조 초기화 확인"""
        # gap_data 초기화
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
        
        # price_data 초기화
        if symbol not in self.price_data:
            self.price_data[symbol] = deque(maxlen=300)
            
        # volume_data 초기화
        if symbol not in self.volume_data:
            self.volume_data[symbol] = {
                'avg_volume': None,
                'volume_ratio': None,
                'volumes': deque(maxlen=20)
            }
    
    async def _process_daily_data(self, symbol: str) -> bool:
        """일봉 데이터 조회 및 처리"""
        try:
            # 1. 캐시에서 일봉 데이터 확인
            cached_daily_data = await self.data_cache.get_daily_data(symbol)
            if cached_daily_data:
                logger.log_system(f"갭 전략 - {symbol} 캐시에서 일봉 데이터 로드")
                daily_data = cached_daily_data
            else:
                # 2. API에서 일봉 데이터 조회
                try:
                    self.performance_stats["api_calls"] += 1
                    price_data = await asyncio.wait_for(
                        api_client.get_daily_price(symbol),
                        timeout=5.0
                    )
                    
                    if price_data.get("rt_cd") != "0":
                        logger.log_warning(f"갭 전략 - {symbol} 일봉 데이터 API 오류: {price_data.get('msg_cd', '알 수 없는 오류')}")
                        return False
                    
                    # 캐시에 저장
                    await self.data_cache.store_daily_data(symbol, price_data)
                    daily_data = price_data
                    
                except (asyncio.TimeoutError, Exception) as e:
                    logger.log_error(e, f"갭 전략 - {symbol} 일봉 데이터 조회 실패")
                    self.performance_stats["api_errors"] += 1
                    return False
            
            # 일봉 데이터 처리
            return self._extract_daily_data(symbol, daily_data)
                
        except Exception as e:
            logger.log_error(e, f"갭 전략 - {symbol} 일봉 데이터 처리 오류")
            return False
    
    def _extract_daily_data(self, symbol: str, price_data: Dict[str, Any]) -> bool:
        """일봉 데이터에서 필요한 정보 추출"""
        try:
            # API 응답 구조에 맞게 수정
            daily_data = []
            if "output2" in price_data and price_data["output2"]:
                daily_data = price_data["output2"]
            elif "output" in price_data and "lst" in price_data["output"]:
                daily_data = price_data["output"]["lst"]
            
            if not daily_data or len(daily_data) <= 1:
                logger.log_warning(f"갭 전략 - {symbol} 일봉 데이터 부족")
                return False
                
            # 전일 데이터 (첫 번째가 오늘, 두 번째가 어제)
            prev_day = daily_data[1]
            prev_close = self._safe_float(prev_day.get("stck_clpr"))
            
            if prev_close > 0:
                self.gap_data[symbol]['prev_close'] = prev_close
                
                # 거래량 데이터 수집
                volumes = []
                for day_data in daily_data[1:21]:  # 최근 20일
                    vol = self._safe_int(self._safe_get(day_data, "acml_vol", default=0))
                    if vol > 0:
                        volumes.append(vol)
                
                if volumes:
                    avg_volume = np.mean(volumes)
                    self.volume_data[symbol]['avg_volume'] = avg_volume
                    self.volume_data[symbol]['volumes'] = deque(volumes, maxlen=20)
                    
                logger.log_system(f"갭 전략 - {symbol} 전일 종가 로드: {prev_close:,.0f}원")
            
            # 오늘 시가 조회
            if "output1" in price_data and price_data["output1"]:
                today_open = self._safe_float(self._safe_get(price_data, "output1", "stck_oprc"))
                if today_open > 0:
                    self.gap_data[symbol]['today_open'] = today_open
                    
                    # 갭 계산
                    if self.gap_data[symbol]['prev_close'] and today_open > 0:
                        prev_close = self.gap_data[symbol]['prev_close']
                        gap_pct = self._safe_divide(today_open - prev_close, prev_close)
                        gap_size = abs(today_open - prev_close)
                        direction = "UP" if gap_pct > 0 else "DOWN"
                        
                        self.gap_data[symbol].update({
                            'gap_pct': gap_pct,
                            'gap_size': gap_size,
                            'direction': direction,
                            'gap_identified': True
                        })
                        
                        # 갭 채움 목표 가격 계산
                        fill_pct = self.params["gap_fill_pct"]
                        if direction == "UP":
                            fill_target = today_open - (gap_size * fill_pct)
                        else:  # DOWN
                            fill_target = today_open + (gap_size * fill_pct)
                            
                        self.gap_data[symbol]['fill_target'] = fill_target
                        
                        logger.log_system(f"갭 전략 - {symbol} 갭 확인: {direction} {gap_pct:.1%} "
                                        f"(시가: {today_open:,.0f}원, 전일종가: {prev_close:,.0f}원)")
                        return True
            
            return False
            
        except Exception as e:
            logger.log_error(e, f"갭 전략 - {symbol} 일봉 데이터 추출 오류")
            return False
    
    async def _load_historical_data(self, symbol: str) -> bool:
        """과거 데이터 로드 (전일 종가, 거래량 등)"""
        try:
            # 초기화되지 않은 경우를 대비한 기본 데이터 구조 설정
            self._ensure_data_structures(symbol)
            
            # 1. 캐시에서 일봉 데이터 확인
            cached_daily_data = await self.data_cache.get_daily_data(symbol)
            if cached_daily_data:
                logger.log_system(f"갭 전략 - {symbol} 캐시에서 과거 데이터 로드")
                return self._extract_daily_data(symbol, cached_daily_data)
                
            # 2. API에서 일봉 데이터 조회
            try:
                self.performance_stats["api_calls"] += 1
                price_data = await asyncio.wait_for(
                    api_client.get_daily_price(symbol),
                    timeout=5.0
                )
                
                if price_data.get("rt_cd") != "0":
                    logger.log_warning(f"갭 전략 - {symbol} 과거 데이터 API 오류: {price_data.get('msg_cd', '알 수 없는 오류')}")
                    return False
                
                # 캐시에 저장
                await self.data_cache.store_daily_data(symbol, price_data)
                
                # 데이터 추출
                return self._extract_daily_data(symbol, price_data)
                
            except (asyncio.TimeoutError, Exception) as e:
                logger.log_error(e, f"갭 전략 - {symbol} 과거 데이터 조회 실패")
                self.performance_stats["api_errors"] += 1
                return False
                
        except Exception as e:
            logger.log_error(e, f"갭 전략 - {symbol} 과거 데이터 로딩 오류")
            return False
    
    async def stop(self):
        """전략 중지"""
        if not self.running:
            return
            
        self.running = False
        logger.log_system("갭 트레이딩 전략 정지 중...")
        
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
                
            # 실행 중인 포지션 기록
            if self.positions:
                logger.log_system(f"갭 전략 정지 시 {len(self.positions)}개 포지션 남아있음")
                
            logger.log_system("갭 트레이딩 전략 정지 완료")
            
        except Exception as e:
            logger.log_error(e, "갭 트레이딩 전략 정지 중 오류")
    
    async def pause(self):
        """전략 일시 중지"""
        if not self.paused:
            self.paused = True
            logger.log_system("갭 트레이딩 전략 일시 중지됨")
        return True

    async def resume(self):
        """전략 재개"""
        if self.paused:
            self.paused = False
            logger.log_system("갭 트레이딩 전략 재개됨")
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
            
            # 가격 및 거래량 데이터 추출
            price = self._safe_float(data.get("stck_prpr"))
            volume = self._safe_int(data.get("cntg_vol", 0))
            
            # 가격 유효성 검증
            if price <= 0:
                return
                
            # 현재 시간
            timestamp = datetime.now()
            
            # 가격 데이터 저장
            self.price_data[symbol].append({
                "price": price,
                "volume": volume,
                "timestamp": timestamp
            })
            
            # 중앙 캐시 업데이트 (비동기 백그라운드로)
            asyncio.create_task(self.data_cache.update_last_price(
                symbol, price, volume, timestamp
            ))
            
            # 장 시작 시간에 갭 확인
            current_time = timestamp.time()
            if time(9, 0) <= current_time <= time(9, 10) and not self.gap_data[symbol]['gap_identified']:
                # 첫 체결가를 당일 시가로 간주하여 갭 확인
                self._identify_gap(symbol, price, timestamp)
            
            # 거래량 누적 및 비율 업데이트 (주기적으로)
            if timestamp.second % self.params["price_update_interval"] == 0:
                self._update_volume_ratio(symbol)
                
        except Exception as e:
            logger.log_error(e, f"웹소켓 가격 업데이트 처리 중 오류: {symbol}")
    
    def _identify_gap(self, symbol: str, price: float, timestamp: datetime) -> bool:
        """갭 확인 및 정보 저장"""
        try:
            # 갭 정보가 이미 확인되었는지 체크
            if self.gap_data[symbol]['gap_identified']:
                return True
                
            # 전일 종가가 없으면 갭 확인 불가
            prev_close = self.gap_data[symbol]['prev_close']
            if not prev_close or prev_close <= 0:
                return False
                
            # 현재가를 당일 시가로 간주
            self.gap_data[symbol]['today_open'] = price
            
            # 갭 크기 계산
            gap_pct = self._safe_divide(price - prev_close, prev_close)
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
                f"갭 확인: {symbol} - {direction} 갭 {gap_pct:.2%}, "
                f"시가: {price:,.0f}원, 전일종가: {prev_close:,.0f}원, "
                f"채움 목표: {fill_target:,.0f}원"
            )
            
            return True
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 갭 확인 중 오류")
            return False
    
    def _update_volume_ratio(self, symbol: str):
        """거래량 비율 업데이트"""
        try:
            if symbol not in self.price_data or not self.price_data[symbol]:
                return
                
            # 현재까지의 누적 거래량 계산
            current_total_volume = sum(item.get("volume", 0) for item in self.price_data[symbol])
            
            # 평균 거래량이 있는 경우 비율 계산
            if symbol in self.volume_data and self.volume_data[symbol]['avg_volume'] and self.volume_data[symbol]['avg_volume'] > 0:
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
                volume_ratio = self._safe_divide(current_total_volume, expected_volume, default=1.0)
                
                self.volume_data[symbol]['volume_ratio'] = volume_ratio
                
        except Exception as e:
            logger.log_error(e, f"{symbol} - 거래량 비율 업데이트 중 오류")
    
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
                            logger.log_system(f"갭 전략: 장 시간이 아님 (현재 {current_time})")
                        
                        sleep_count += 1
                        await asyncio.sleep(1)
                        continue
                    
                    # 전략이 일시 중지된 경우
                    if self.paused or (order_manager and order_manager.is_trading_paused()):
                        await asyncio.sleep(1)
                        continue
                    
                    # 9:05 이후에만 트레이딩 실행 (갭 확인 후)
                    if current_time >= time(9, 5):
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
                    await asyncio.sleep(self.params["check_interval"])
                    
                except asyncio.CancelledError:
                    logger.log_system("갭 전략 루프 취소됨")
                    break
                    
                except Exception as loop_error:
                    logger.log_error(loop_error, "갭 전략 루프 내부 오류")
                    # 오류 발생 시 더 긴 대기
                    await asyncio.sleep(5)
                    
        except asyncio.CancelledError:
            logger.log_system("갭 전략 루프 태스크 취소됨")
        except Exception as e:
            logger.log_error(e, "갭 전략 루프 예외 발생")
    
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
                
            # 현재가 가져오기
            current_price = self._get_current_price(symbol)
            if current_price <= 0:
                return
            
            # 갭 데이터 분석
            gap_data = self.gap_data.get(symbol, {})
            
            # 갭 크기가 임계값 내에 있는지 확인
            gap_pct = self._safe_float(gap_data.get('gap_pct'))
            if abs(gap_pct) < self.params["min_gap_pct"] or abs(gap_pct) > self.params["max_gap_pct"]:
                return
            
            # 거래량 충분한지 확인
            volume_ratio = self._safe_float(self._safe_get(self.volume_data, symbol, 'volume_ratio'))
            if volume_ratio < self.params["volume_threshold"]:
                return
            
            # 이미 포지션 있는지 확인
            symbol_positions = self._get_symbol_positions(symbol)
            if len(symbol_positions) >= self.params["max_positions"]:
                return
            
            # 갭 채움 목표 가격 확인
            fill_target = gap_data.get('fill_target')
            if not fill_target:
                return
            
            # 조건 확인 - 개선된 거래 로직
            if gap_data['direction'] == "UP":
                # 갭 업일 때 거래 로직
                if current_price <= fill_target:
                    # 매수: 갭이 이미 채워지고 있으면 반등을 노린 매수
                    await self._enter_position(symbol, "BUY", current_price, gap_data)
                elif self.params.get("allow_short", False):  # 숏 허용 설정이 있을 경우
                    # 매도: 갭 발생 초기에 숏 포지션 진입 (갭이 채워질 것으로 예상)
                    if current_price >= gap_data['today_open'] * 1.01:  # 시가보다 1% 이상 상승 시
                        await self._enter_position(symbol, "SELL", current_price, gap_data)
            
            elif gap_data['direction'] == "DOWN":
                # 갭 다운일 때 거래 로직
                if current_price >= fill_target:
                    # 매수: 갭이 채워지고 있으면 매수
                    await self._enter_position(symbol, "BUY", current_price, gap_data)
                elif self.params.get("allow_short", False):  # 숏 허용 설정이 있을 경우
                    # 매도: 갭 발생 초기에 추가 하락이 예상될 경우 숏 포지션
                    if current_price <= gap_data['today_open'] * 0.99:  # 시가보다 1% 이상 하락 시
                        await self._enter_position(symbol, "SELL", current_price, gap_data)
                
        except Exception as e:
            logger.log_error(e, f"{symbol} - 갭 분석 중 오류")
    
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
                
            # 2. 갭 데이터가 확인되었는지 체크
            gap_data = self.gap_data.get(symbol, {})
            if not gap_data.get('gap_identified', False):
                # 갭 데이터 로드 시도
                gap_loaded = await self._load_historical_data(symbol)
                self.data_validity_cache[symbol] = (datetime.now(), gap_loaded)
                return gap_loaded
            
            # 3. 가격 데이터 존재 확인
            if symbol not in self.price_data or not self.price_data[symbol]:
                # 가격 데이터 로드 시도
                price_loaded = await self._load_initial_data(symbol)
                self.data_validity_cache[symbol] = (datetime.now(), price_loaded)
                return price_loaded
                
            # 4. 최소 필요 데이터 확인
            if len(self.price_data[symbol]) < self.params["min_data_points"]:
                self.data_validity_cache[symbol] = (datetime.now(), False)
                return False
                
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
    
    async def _enter_position(self, symbol: str, side: str, current_price: float, gap_data: Dict[str, Any]):
        """포지션 진입"""
        try:
            # 주문 수량 계산
            position_size = self.params["position_size"]  # 100만원
            
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
                    strategy="gap",
                    reason=f"gap_fill_{gap_data['direction'].lower()}"
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
                stop_loss_pct = self.params["stop_loss_pct"]
                gap_size = gap_data['gap_size']
                
                if side == "BUY":
                    # 매수 포지션 손절/익절 설정
                    stop_price = current_price - (gap_size * stop_loss_pct)
                    take_profit = gap_data['prev_close']  # 전일 종가가 익절 목표
                else:  # SELL
                    # 매도 포지션 손절/익절 설정
                    stop_price = current_price + (gap_size * stop_loss_pct)
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
                    "gap_pct": gap_data['gap_pct'],
                    "original_stop": stop_price,  # 트레일링 스탑용
                    "trailing_stop": stop_price,  # 트레일링 스탑 기준가
                    "trailing_activated": False,  # 트레일링 활성화 여부
                    "highest_price": current_price if side == "BUY" else None,
                    "lowest_price": current_price if side == "SELL" else None
                }
                
                # 로그 기록
                logger.log_system(
                    f"※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※\n"
                    f"갭 전략: {side} 포지션 진입 성공! {symbol} at {current_price:,.0f}, "
                    f"목표: {take_profit:,.0f}, 손절: {stop_price:,.0f}\n"
                    f"※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※"
                )
                
                # 알림 전송
                asyncio.create_task(alert_system.notify_trade(
                    symbol=symbol,
                    side=side,
                    price=current_price,
                    quantity=quantity,
                    strategy="gap",
                    reason=f"gap_fill_{gap_data['direction'].lower()}"
                ))
                
            else:
                # 주문 실패
                self.performance_stats["failed_trades"] += 1
                error_reason = result.get("reason", "알 수 없는 오류") if result else "주문 결과가 없음"
                logger.log_system(f"갭 전략: {symbol} {side} 주문 실패 - {error_reason}")
                
        except Exception as e:
            logger.log_error(e, f"{symbol} - 포지션 진입 중 오류")
            self.performance_stats["failed_trades"] += 1
    
    def _calculate_market_risk(self, symbol: str) -> float:
        """시장 리스크 수준 계산 (0-1)"""
        try:
            # 갭 데이터 확인
            gap_data = self.gap_data.get(symbol, {})
            
            # 갭이 없으면 중간 리스크 반환
            if not gap_data.get('gap_identified', False):
                return 0.5
            
            # 갭 크기 및 방향 확인
            gap_pct = abs(self._safe_float(gap_data.get('gap_pct')))
            direction = gap_data.get('direction')
            
            # 리스크 계산
            if direction == "UP":
                if gap_pct < 0.01:  # 작은 갭은 저위험
                    return 0.3
                elif gap_pct < 0.03:  # 중간 갭은 중위험
                    return 0.5
                else:  # 큰 갭은 고위험
                    return 0.7
            else:  # DOWN
                if gap_pct < 0.01:  # 작은 갭은 저위험
                    return 0.3
                elif gap_pct < 0.03:  # 중간 갭은 중위험
                    return 0.5
                else:  # 큰 갭은 고위험
                    return 0.7
                
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
                current_price = self._get_current_price(symbol)
                if current_price <= 0:
                    continue
                    
                side = position["side"]
                entry_time = position["entry_time"]
                entry_price = position["entry_price"]
                stop_price = position["stop_price"]
                target_price = position["target_price"]
                
                # 포지션 보유 시간
                hold_time_minutes = (datetime.now() - entry_time).total_seconds() / 60
                max_hold_time = self.params["hold_time_minutes"]
                
                # 트레일링 스탑 업데이트
                if self.params.get("use_trailing_stop", True):
                    self._update_trailing_stop(position, current_price)
                
                # 현재 수익률 계산
                if side == "BUY":
                    profit_pct = (current_price - entry_price) / entry_price * 100
                else:  # SELL
                    profit_pct = (entry_price - current_price) / entry_price * 100
                
                # 청산 조건 확인
                should_exit = False
                exit_reason = ""
                
                # 손절/익절 조건 확인
                if side == "BUY":
                    # 매수 포지션
                    if current_price <= stop_price:
                        should_exit = True
                        exit_reason = "stop_loss"
                    elif current_price >= target_price:
                        should_exit = True
                        exit_reason = "gap_filled"
                        
                else:  # SELL
                    # 매도 포지션
                    if current_price >= stop_price:
                        should_exit = True
                        exit_reason = "stop_loss"
                    elif current_price <= target_price:
                        should_exit = True
                        exit_reason = "gap_filled"
                
                # 트레일링 스탑 확인
                if position.get("trailing_activated", False):
                    trailing_stop = position.get("trailing_stop")
                    if side == "BUY" and current_price <= trailing_stop:
                        should_exit = True
                        exit_reason = "trailing_stop"
                    elif side == "SELL" and current_price >= trailing_stop:
                        should_exit = True
                        exit_reason = "trailing_stop"
                
                # 보유 시간 초과 확인
                if hold_time_minutes >= max_hold_time:
                    should_exit = True
                    exit_reason = "time_limit"
                
                # 청산 실행
                if should_exit:
                    logger.log_system(f"갭 전략 - {symbol} 청산 조건 감지: {exit_reason}, 손익률={profit_pct:.2f}%")
                    await self._exit_position(position_id, exit_reason, current_price, profit_pct)
                    
        except Exception as e:
            logger.log_error(e, "갭 전략 포지션 모니터링 오류")

    def _update_trailing_stop(self, position: Dict[str, Any], current_price: float):
        """트레일링 스탑 업데이트"""
        try:
            if not self.params.get("use_trailing_stop", True):
                return
                
            side = position["side"]
            entry_price = position["entry_price"]
            trailing_pct = self.params.get("trailing_pct", 0.005)
            min_profit_pct = self.params.get("minimum_profit_pct", 0.01)
            
            # 수익률 계산
            if side == "BUY":
                profit_pct = self._safe_divide(current_price - entry_price, entry_price)
                
                # 신규 고점 갱신 및 트레일링 스탑 업데이트
                if profit_pct >= min_profit_pct:  # 최소 수익 도달 시 활성화
                    # 트레일링 활성화
                    position["trailing_activated"] = True
                    
                    # 최고가 갱신 시 트레일링 스탑 업데이트
                    if position.get("highest_price", 0) < current_price:
                        position["highest_price"] = current_price
                        new_stop = current_price * (1 - trailing_pct)
                        
                        # 기존 손절가보다 높을 경우에만 업데이트
                        if new_stop > position.get("trailing_stop", 0):
                            position["trailing_stop"] = new_stop
                            logger.log_system(f"갭 전략 - {position['symbol']} 트레일링 스탑 상향 조정: {new_stop:,.0f}원")
                    
            else:  # SELL
                profit_pct = self._safe_divide(entry_price - current_price, entry_price)
                
                # 신규 저점 갱신 및 트레일링 스탑 업데이트
                if profit_pct >= min_profit_pct:  # 최소 수익 도달 시 활성화
                    # 트레일링 활성화
                    position["trailing_activated"] = True
                    
                    # 최저가 갱신 시 트레일링 스탑 업데이트
                    lowest_price = position.get("lowest_price")
                    if lowest_price is None or lowest_price > current_price:
                        position["lowest_price"] = current_price
                        new_stop = current_price * (1 + trailing_pct)
                        
                        # 기존 손절가보다 낮을 경우에만 업데이트
                        if not position.get("trailing_stop") or new_stop < position["trailing_stop"]:
                            position["trailing_stop"] = new_stop
                            logger.log_system(f"갭 전략 - {position['symbol']} 트레일링 스탑 하향 조정: {new_stop:,.0f}원")
                    
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
                current_price = self._get_current_price(symbol)
            
            # 현재가가 여전히 없는 경우 포지션 가격 사용
            if current_price <= 0:
                current_price = position["entry_price"]
            
            # 손익률 계산
            entry_price = position["entry_price"]
            if profit_pct is None:
                if position["side"] == "BUY":
                    profit_pct = (current_price - entry_price) / entry_price * 100
                else:
                    profit_pct = (entry_price - current_price) / entry_price * 100
            
            # 상세 로그
            logger.log_system(
                f"갭 전략 - {symbol} {position['side']} 포지션 청산 시도: "
                f"진입가={entry_price:,.0f}원, 현재가={current_price:,.0f}원, "
                f"손익률={profit_pct:.2f}%, 사유={reason}"
            )
            
            try:
                result = await order_manager.place_order(
                    symbol=symbol,
                    side=exit_side,
                    quantity=position["quantity"],
                    order_type="MARKET",
                    strategy="gap",
                    reason=reason
                )
            except Exception as order_error:
                logger.log_error(order_error, f"{symbol} - 청산 주문 실행 중 오류")
                self.performance_stats["failed_trades"] += 1
                return
            
            # 주문 결과 처리
            if result and result.get("status") == "success":
                # 청산 성공
                self.performance_stats["successful_trades"] += 1
                
                # 청산 성공 상세 로그
                logger.log_system(
                    f"※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※\n"
                    f"갭 전략: {symbol} {position['side']} 포지션 청산 성공! 진입가={entry_price:,.0f}원, "
                    f"청산가={current_price:,.0f}원, 손익률={profit_pct:.2f}%, 사유={reason}\n"
                    f"※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※"
                )
                
                # 알림 전송
                asyncio.create_task(alert_system.notify_trade(
                    symbol=symbol,
                    side=exit_side,
                    price=current_price,
                    quantity=position["quantity"],
                    strategy="gap",
                    reason=f"청산: {reason}",
                    profit_pct=f"{profit_pct:.2f}%"
                ))
                
                # 포지션 제거
                del self.positions[position_id]
                
            else:
                # 청산 실패
                self.performance_stats["failed_trades"] += 1
                error_reason = result.get("reason", "알 수 없는 오류") if result else "주문 결과가 없음"
                logger.log_warning(f"갭 전략 - {symbol} 청산 주문 실패: {error_reason}")
                
        except Exception as e:
            logger.log_error(e, f"포지션 {position_id} 청산 중 오류")
    
    async def get_signal(self, symbol: str) -> Dict[str, Any]:
        """전략 신호 반환 (combined_strategy에서 호출)"""
        try:
            # 성능 통계 업데이트
            self.performance_stats["signal_calculations"] += 1
            
            # 1. 데이터 유효성 검증
            valid_data = await self._ensure_valid_data(symbol)
            if not valid_data:
                return {"signal": 0, "direction": "NEUTRAL", "reason": "insufficient_data"}
                
            # 2. 갭 데이터 확인
            gap_data = self.gap_data.get(symbol, {})
            if not gap_data.get('gap_identified', False):
                return {"signal": 0, "direction": "NEUTRAL", "reason": "gap_not_identified"}
            
            # 3. 갭 크기가 임계값 내에 있는지 확인
            gap_pct = self._safe_float(gap_data.get('gap_pct'))
            min_gap = self.params["min_gap_pct"]
            max_gap = self.params["max_gap_pct"]
            
            if abs(gap_pct) < min_gap or abs(gap_pct) > max_gap:
                return {"signal": 0, "direction": "NEUTRAL", "reason": "gap_size_out_of_range"}
            
            # 4. 현재가 확인
            current_price = self._get_current_price(symbol)
            if current_price <= 0:
                return {"signal": 0, "direction": "NEUTRAL", "reason": "invalid_price"}
            
            # 5. 방향과 신호 강도 계산
            direction = "NEUTRAL"
            signal_strength = 0
            
            # 갭 방향에 따른 트레이딩 방향
            gap_direction = gap_data.get('direction')
            if gap_direction is None:
                return {"signal": 0, "direction": "NEUTRAL", "reason": "direction_not_defined"}
                
            # 6. 갭 방향에 따른 매매 신호 결정
            if gap_direction == "UP":
                direction = "SELL"  # 갭 업은 매도 신호 (갭 채움 예상)
                signal_strength = min(10, abs(gap_pct) * 200)  # 갭이 클수록 강한 신호
                
                # 갭 채움 진행 정도에 따른 신호 조정
                if current_price <= gap_data['fill_target']:
                    # 이미 갭이 채워지고 있으면 신호 약화
                    fill_progress = (gap_data['today_open'] - current_price) / gap_data['gap_size']
                    signal_strength = signal_strength * (1 - fill_progress)
                    
            elif gap_direction == "DOWN":
                direction = "BUY"  # 갭 다운은 매수 신호 (갭 채움 예상)
                signal_strength = min(10, abs(gap_pct) * 200)
                
                # 갭 채움 진행 정도에 따른 신호 조정
                if current_price >= gap_data['fill_target']:
                    # 이미 갭이 채워지고 있으면 신호 약화
                    fill_progress = (current_price - gap_data['today_open']) / gap_data['gap_size']
                    signal_strength = signal_strength * (1 - fill_progress)
            
            # 7. 거래량 요소 고려
            vol_ratio = self._safe_float(self._safe_get(self.volume_data, symbol, 'volume_ratio'))
            vol_threshold = self.params["volume_threshold"]
            
            # 거래량이 평균보다 높으면 신호 강화
            if vol_ratio > vol_threshold:
                vol_bonus = min(2, (vol_ratio - vol_threshold))
                signal_strength = min(10, signal_strength + vol_bonus)
            
            # 8. 최종 신호 반환
            return {
                "signal": signal_strength, 
                "direction": direction, 
                "gap_pct": f"{gap_pct:.2%}",
                "gap_direction": gap_direction,
                "fill_target": gap_data['fill_target'],
                "volume_ratio": vol_ratio
            }
            
        except Exception as e:
            logger.log_error(e, f"{symbol} - 갭 신호 계산 오류")
            trace = traceback.format_exc()
            logger.log_debug(f"스택 트레이스: {trace}")
            return {"signal": 0, "direction": "NEUTRAL", "reason": "error"}
    
    async def update_symbols(self, new_symbols: List[str]):
        """종목 목록 업데이트"""
        try:
            logger.log_system(f"갭 전략: 종목 목록 업데이트 시작 ({len(new_symbols)}개)")
            
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
                
                # 데이터 캐시에서 등록 해제
                await self.data_cache.unregister_symbol(symbol, "gap")
                
                # 데이터 정리
                if symbol in self.price_data:
                    del self.price_data[symbol]
                if symbol in self.gap_data:
                    del self.gap_data[symbol]
                if symbol in self.volume_data:
                    del self.volume_data[symbol]
                if symbol in self.last_update:
                    del self.last_update[symbol]
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
                f"갭 전략: 종목 목록 업데이트 완료 - "
                f"추가: {len(to_add)}개, 제거: {len(to_remove)}개, 총: {len(self.watched_symbols)}개"
            )
        
        except Exception as e:
            logger.log_error(e, "갭 트레이딩 전략 종목 업데이트 오류")

# 싱글톤 인스턴스
gap_strategy = GapStrategy()
