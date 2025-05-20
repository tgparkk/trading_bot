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
import random
import threading
import concurrent.futures

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
                # 각 전략별 가중치 (0~1 사이 값, 합계 1.0)
                "breakout_weight": 0.1,     # 브레이크아웃 전략 가중치 (0.2→0.1)
                "momentum_weight": 0.2,     # 모멘텀 전략 가중치 (0.25→0.2)
                "gap_weight": 0.3,          # 갭 트레이딩 전략 가중치 (0.2→0.3)
                "vwap_weight": 0.15,        # VWAP 전략 가중치 (0.2→0.15)
                "volume_weight": 0.25,      # 볼륨 스파이크 전략 가중치 (0.15→0.25)
                
                # 매매 신호 기준
                "buy_threshold": 4.0,       # 매수 신호 임계값 (0~10) - 4.0으로 낮춤 (원래 5.0)
                "sell_threshold": 6.0,      # 매도 신호 임계값 (0~10)
                "min_agreement": 3,         # 최소 몇 개 전략이 일치해야 하는지
                
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
            
            # 전략 객체 초기화 - 명시적 모듈 로드 및 에러 처리 개선
            self.strategies = {}
            
            # 전략 모듈 로드 및 초기화
            try:
                # breakout 전략 로드
                from strategies.breakout_strategy import breakout_strategy
                self.strategies['breakout'] = breakout_strategy
                logger.log_system("브레이크아웃 전략 로드 성공")
            except ImportError as e:
                logger.log_error(e, "브레이크아웃 전략 로드 실패")
                self.strategies['breakout'] = None
            
            try:
                # momentum 전략 로드
                from strategies.momentum_strategy import momentum_strategy
                self.strategies['momentum'] = momentum_strategy
                logger.log_system("모멘텀 전략 로드 성공")
            except ImportError as e:
                logger.log_error(e, "모멘텀 전략 로드 실패")
                self.strategies['momentum'] = None
            
            try:
                # gap 전략 로드
                from strategies.gap_strategy import gap_strategy
                self.strategies['gap'] = gap_strategy
                logger.log_system("갭 전략 로드 성공")
            except ImportError as e:
                logger.log_error(e, "갭 전략 로드 실패")
                self.strategies['gap'] = None
            
            try:
                # vwap 전략 로드
                from strategies.vwap_strategy import vwap_strategy
                self.strategies['vwap'] = vwap_strategy
                logger.log_system("VWAP 전략 로드 성공")
            except ImportError as e:
                logger.log_error(e, "VWAP 전략 로드 실패")
                self.strategies['vwap'] = None
            
            try:
                # volume 전략 로드
                from strategies.volume_spike_strategy import volume_strategy
                self.strategies['volume'] = volume_strategy
                logger.log_system("볼륨 전략 로드 성공")
            except ImportError as e:
                logger.log_error(e, "볼륨 전략 로드 실패")
                self.strategies['volume'] = None
            
            # 전략 객체 유효성 확인
            valid_strategies = 0
            for strategy_name, strategy in self.strategies.items():
                if strategy is None:
                    logger.log_error(f"{strategy_name} 전략 객체가 None입니다. 초기화에 실패했을 수 있습니다.")
                elif not hasattr(strategy, "get_signal"):
                    logger.log_error(f"{strategy_name} 전략에 get_signal 메서드가 없습니다.")
                else:
                    valid_strategies += 1
            
            logger.log_system(f"전략 초기화 상태: 총 {len(self.strategies)}개 중 {valid_strategies}개 유효")
            
            # 가중치 정규화
            self._normalize_weights()
            self._initialized = True
        
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
        """개별 전략 신호 업데이트"""
        try:
            logger.log_system(f"[전략] {symbol} - 신호 업데이트 시작")
            
            # 1. 현재가 정보 업데이트
            await self._update_price_data(symbol)
            
            # 2. 개별 전략 신호 계산 (병렬 처리)
            await self._calculate_strategy_signals(symbol)
            
            # 3. 통합 신호 계산
            self._update_combined_signal(symbol)
            
            logger.log_system(f"[전략] {symbol} - 신호 업데이트 완료: 점수={self.signals[symbol]['score']:.2f}, 방향={self.signals[symbol]['direction']}")
            
        except Exception as e:
            logger.log_warning(f"[전략] {symbol} - 신호 업데이트 중 오류: {str(e)}")
            # 기존 신호 정보는 유지
        
    async def _update_price_data(self, symbol: str):
        """심볼의 현재가 정보 업데이트"""
        try:
            from core.api_client import api_client
            
            # API 호출 타임아웃 처리
            get_symbol_task = asyncio.create_task(api_client.get_symbol_info(symbol))
            try:
                symbol_info = await asyncio.wait_for(get_symbol_task, timeout=3.0)
                
                if symbol_info and "current_price" in symbol_info:
                    current_price = float(symbol_info["current_price"])
                    
                    logger.log_debug(f"[전략] {symbol} - API에서 가격 가져옴: {current_price:,}원")
                    
                    # 가격 데이터 업데이트
                    self._store_price_data(symbol, current_price, symbol_info.get("volume", 0))
                else:
                    logger.log_debug(f"[전략] {symbol} - API 응답에 가격 정보 없음")
                    
            except asyncio.TimeoutError:
                logger.log_debug(f"[전략] {symbol} - API 가격 조회 타임아웃 (3초)")
                await self._handle_price_fallback(symbol)
                    
        except Exception as e:
            logger.log_warning(f"[전략] {symbol} - API 가격 조회 실패: {str(e)}")
            await self._handle_price_fallback(symbol)

    def _store_price_data(self, symbol: str, price: float, volume: int = 0):
        """가격 데이터 저장 및 신호 초기화"""
        # 가격 데이터 저장
        if symbol not in self.price_data:
            self.price_data[symbol] = deque(maxlen=100)
        
        self.price_data[symbol].append({
            "price": price,
            "volume": volume,
            "timestamp": datetime.now()
        })
        
        # signals 초기화 또는 업데이트
        if symbol not in self.signals:
            self.signals[symbol] = {
                'score': 0,
                'direction': "NEUTRAL",
                'strategies': {},
                'last_update': None,
                'last_price': price
            }
        else:
            self.signals[symbol]["last_price"] = price

    async def _calculate_strategy_signals(self, symbol: str):
        """모든 전략에 대해 병렬로 신호 계산"""
        # 병렬 처리를 위한 Task 리스트
        signal_tasks = []
        
        # 각 전략별 신호 업데이트 태스크 생성
        for strategy_name, strategy in self.strategies.items():
            if strategy is None:
                logger.log_debug(f"[전략] {symbol} - {strategy_name} 전략이 None이므로 건너뜀")
                continue
            
            task = asyncio.create_task(
                self._get_single_strategy_signal(symbol, strategy_name, strategy)
            )
            signal_tasks.append(task)
        
        # 모든 전략의 신호를 병렬로 기다림 (최대 4초 타임아웃)
        try:
            strategy_results = await asyncio.wait_for(
                asyncio.gather(*signal_tasks, return_exceptions=True), 
                timeout=4.0
            )
            
            # signals 딕셔너리 초기화
            self._ensure_signal_structure(symbol)
            
            # 전략 결과 저장
            self._process_strategy_results(symbol, strategy_results)
            
        except asyncio.TimeoutError:
            logger.log_warning(f"[전략] {symbol} - 전체 전략 신호 업데이트 타임아웃 (4초)")
        except Exception as gather_error:
            logger.log_warning(f"[전략] {symbol} - 전략 신호 수집 중 오류: {str(gather_error)}")

    async def _get_single_strategy_signal(self, symbol: str, strategy_name: str, strategy):
        """단일 전략의 신호 계산 (타임아웃 처리 포함)"""
        try:
            logger.log_debug(f"[전략] {symbol} - {strategy_name} 전략 신호 요청 시작")
            
            try:
                # 타임아웃 설정
                signal = await asyncio.wait_for(strategy.get_signal(symbol), timeout=3.0)
                
                if signal is None:
                    logger.log_debug(f"[전략] {symbol} - {strategy_name} 전략 신호가 None으로 반환됨")
                    return strategy_name, {'signal': 0, 'direction': 'NEUTRAL'}
                
                return strategy_name, signal
                
            except asyncio.TimeoutError:
                logger.log_debug(f"[전략] {symbol} - {strategy_name} 전략 호출 타임아웃 (3초)")
                return strategy_name, {'signal': 0, 'direction': 'NEUTRAL'}
            except Exception as signal_error:
                logger.log_warning(f"[전략] {symbol} - {strategy_name} 신호 계산 중 오류: {str(signal_error)}")
                return strategy_name, {'signal': 0, 'direction': 'NEUTRAL'}
                
        except Exception as task_error:
            logger.log_warning(f"[전략] {symbol} - {strategy_name} Task 실행 중 오류: {str(task_error)}")
            return strategy_name, {'signal': 0, 'direction': 'NEUTRAL'}

    def _ensure_signal_structure(self, symbol: str):
        """신호 데이터 구조 초기화 확인"""
        if symbol not in self.signals:
            current_price = 0
            if symbol in self.price_data and self.price_data[symbol]:
                current_price = self.price_data[symbol][-1]["price"]
            
            self.signals[symbol] = {
                "strategies": {},
                "combined_signal": 0,
                "direction": "NEUTRAL",
                "last_update": datetime.now(),
                "last_price": current_price
            }
        
        # strategies 필드 확인
        if "strategies" not in self.signals[symbol]:
            self.signals[symbol]["strategies"] = {}

    def _process_strategy_results(self, symbol: str, strategy_results):
        """전략 결과 처리 및 저장"""
        for result in strategy_results:
            if isinstance(result, Exception):
                logger.log_warning(f"[전략] {symbol} - 일부 전략 신호 계산 중 예외 발생: {str(result)}")
                continue
            
            strategy_name, signal = result
            
            # 신호 데이터 검증 및 저장
            if isinstance(signal, dict) and 'signal' in signal and 'direction' in signal:
                # 신호값 검증
                signal_value = self._validate_signal_value(signal['signal'])
                
                # 방향 검증
                direction_value = signal['direction']
                if direction_value not in ["BUY", "SELL", "NEUTRAL"]:
                    direction_value = "NEUTRAL"
                
                # 검증된 값 저장
                self.signals[symbol]["strategies"][strategy_name] = {
                    'signal': signal_value, 
                    'direction': direction_value
                }
                
                logger.log_debug(f"[전략] {symbol} - {strategy_name} 전략 신호: {signal_value:.2f}, 방향: {direction_value}")
            else:
                # 유효하지 않은 신호 형식
                logger.log_warning(f"[전략] {symbol} - {strategy_name} 전략 신호 형식 오류: {signal}")
                
                # 기본값 저장
                self.signals[symbol]["strategies"][strategy_name] = {
                    'signal': 0, 
                    'direction': "NEUTRAL"
                }

    def _validate_signal_value(self, signal_value):
        """신호값 검증 및 변환"""
        try:
            if isinstance(signal_value, str):
                return float(signal_value)
            elif isinstance(signal_value, (int, float)):
                return signal_value
            else:
                return 0
        except (ValueError, TypeError):
            return 0

    def _update_combined_signal(self, symbol: str):
        """통합 신호 계산 및 업데이트"""
        # 신호 구조 확인
        self._ensure_signal_structure(symbol)
        
        # 통합 신호 계산
        score, direction, agreements = self._calculate_combined_signal(symbol)
        
        # 결과 저장
        self.signals[symbol]["combined_signal"] = score
        self.signals[symbol]["score"] = score
        self.signals[symbol]["direction"] = direction
        self.signals[symbol]["agreements"] = agreements
        self.signals[symbol]["last_update"] = datetime.now()
    
    async def _handle_price_fallback(self, symbol: str):
        """API 가격 조회 실패 시 대체 가격 정보 생성"""
        # 1. 이전 가격 데이터가 있으면 사용
        if symbol in self.price_data and self.price_data[symbol]:
            last_price_data = self.price_data[symbol][-1]
            current_price = last_price_data["price"]
            logger.log_system(f"[전략] {symbol} - 이전 가격 데이터 사용: {current_price:,}원")
            
            # signals에도 가격 정보 유지
            if symbol in self.signals:
                self.signals[symbol]["last_price"] = current_price
            else:
                self.signals[symbol] = {
                    'score': 0,
                    'direction': "NEUTRAL",
                    'strategies': {},
                    'last_update': None,
                    'last_price': current_price
                }
            return
        
        # 2. 이전 가격이 없으면 테스트 데이터 생성 요청
        try:
            from core.api_client import api_client
            test_data = api_client._generate_test_price_data(symbol)
            if test_data and "current_price" in test_data:
                current_price = float(test_data["current_price"])
                logger.log_system(f"[전략] {symbol} - 테스트 가격 데이터 생성: {current_price:,}원")
                
                # 가격 데이터 업데이트
                if symbol not in self.price_data:
                    self.price_data[symbol] = deque(maxlen=100)
                
                self.price_data[symbol].append({
                    "price": current_price,
                    "volume": test_data.get("volume", 0),
                    "timestamp": datetime.now()
                })
                
                # signals에도 가격 정보 저장
                if symbol in self.signals:
                    self.signals[symbol]["last_price"] = current_price
                else:
                    self.signals[symbol] = {
                        'score': 0,
                        'direction': "NEUTRAL",
                        'strategies': {},
                        'last_update': None,
                        'last_price': current_price
                    }
            else:
                logger.log_system(f"[전략] {symbol} - 테스트 데이터 생성 실패")
        except Exception as e:
            logger.log_error(e, f"{symbol} - 대체 가격 데이터 생성 실패")
    
    def _calculate_combined_signal(self, symbol: str) -> Tuple[float, str, Dict[str, int]]:
        """개별 신호를 종합하여 최종 신호 강도와 방향 계산"""
        try:
            # strategies가 없으면 초기화
            if symbol not in self.signals or 'strategies' not in self.signals[symbol]:
                return 0.0, "NEUTRAL", {"BUY": 0, "SELL": 0}
                
            strategies = self.signals[symbol]['strategies']
            
            # 모든 전략이 기본값인지 확인 (실제 신호가 있는지)
            has_real_signals = False
            for strat_name, strat_data in strategies.items():
                if strat_data.get('signal', 0) > 0 or strat_data.get('direction', 'NEUTRAL') != 'NEUTRAL':
                    has_real_signals = True
                    break
                    
            # 실제 신호가 없으면 기본값 반환
            if not has_real_signals:
                logger.log_system(f"[DEBUG] {symbol} - 모든 전략이 기본값이므로 계산 건너뜀")
                return 0.0, "NEUTRAL", {"BUY": 0, "SELL": 0}
            
            # 가중 점수 계산 (안전한 변환 추가)
            breakout_score = self._safe_float(strategies['breakout']['signal']) * self.params["breakout_weight"]
            momentum_score = self._safe_float(strategies['momentum']['signal']) * self.params["momentum_weight"]
            gap_score = self._safe_float(strategies['gap']['signal']) * self.params["gap_weight"]
            vwap_score = self._safe_float(strategies['vwap']['signal']) * self.params["vwap_weight"]
            volume_score = self._safe_float(strategies['volume']['signal']) * self.params["volume_weight"]
            
            # 개별 점수 로그
            logger.log_system(f"[DEBUG] {symbol} - 가중치 계산: "
                             f"breakout={breakout_score:.2f} (원래값={strategies['breakout']['signal']}), "
                             f"momentum={momentum_score:.2f} (원래값={strategies['momentum']['signal']}), "
                             f"gap={gap_score:.2f} (원래값={strategies['gap']['signal']}), "
                             f"vwap={vwap_score:.2f} (원래값={strategies['vwap']['signal']}), "
                             f"volume={volume_score:.2f} (원래값={strategies['volume']['signal']})")
            
            # 종합 점수 (0-10)
            total_score = breakout_score + momentum_score + gap_score + vwap_score + volume_score
            
            # 방향성 투표
            buy_votes = 0
            sell_votes = 0
            
            # 각 전략별로 매수/매도 의견 카운트
            for strat_name, strat_data in strategies.items():
                direction = strat_data.get('direction', 'NEUTRAL')
                if direction == "BUY":
                    buy_votes += 1
                elif direction == "SELL":
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
                
                for strat_name, strat_data in strategies.items():
                    strat_direction = strat_data.get('direction', 'NEUTRAL')
                    strat_signal = self._safe_float(strat_data.get('signal', 0))
                    
                    # 해당 전략의 가중치 가져오기
                    weight_param = f"{strat_name}_weight"
                    weight = self.params.get(weight_param, 0.2)  # 기본값 0.2
                    
                    if strat_direction == "BUY":
                        buy_strength += strat_signal * weight
                    elif strat_direction == "SELL":
                        sell_strength += strat_signal * weight
                
                # 강도가 더 높은 쪽으로 결정
                if buy_strength > sell_strength:
                    direction = "BUY"
                elif sell_strength > buy_strength:
                    direction = "SELL"
            
            agreements = {"BUY": buy_votes, "SELL": sell_votes}
            return total_score, direction, agreements
            
        except Exception as e:
            logger.log_error(e, f"Error in _calculate_combined_signal for {symbol}")
            return 0.0, "NEUTRAL", {"BUY": 0, "SELL": 0}
    
    def _safe_float(self, value) -> float:
        """안전하게 float로 변환"""
        try:
            if isinstance(value, (int, float)):
                return float(value)
            elif isinstance(value, str):
                return float(value)
            else:
                return 0.0
        except (ValueError, TypeError):
            return 0.0
    
    async def _strategy_loop(self):
        """전략 실행 루프"""
        while self.running:
            try:
                # 장 시간 체크
                current_time = datetime.now().time()
                is_market_hours = time(9, 0) <= current_time <= time(15, 30)
                
                # 장 시간일 때만 진행
                if not is_market_hours:
                    # 매시간 정각에만 로그 출력 (불필요한 로그 감소)
                    if current_time.minute == 0 and current_time.second < 10:
                        logger.log_system(f"장 시간이 아님 - 현재: {current_time}, 거래 평가 건너뜀 (09:00-15:30)")
                    await asyncio.sleep(60)  # 장 시간 아닌 경우 1분 대기
                    continue
                
                # 전략이 일시 중지된 경우 스킵
                if self.paused:
                    await asyncio.sleep(1)
                    continue
                
                # 트레이딩 일시 중지 확인
                try:
                    # is_trading_paused는 코루틴이 아닌 일반 함수이므로 직접 호출
                    is_paused = order_manager.is_trading_paused()
                    if is_paused:
                        await asyncio.sleep(1)
                        continue
                except Exception as e:
                    logger.log_error(e, "is_trading_paused 호출 실패")
                    # 오류 발생 시 기본값으로 계속 진행
                
                # 거래 신호 확인 및 실행 (평가 로그 생성)
                for symbol in self.watched_symbols:
                    try:
                        check_task = asyncio.create_task(self._check_and_trade(symbol))
                        await asyncio.wait_for(check_task, timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.log_system(f"{symbol} - _check_and_trade 처리 타임아웃 (5초)")
                    except Exception as e:
                        logger.log_error(e, f"{symbol} - _check_and_trade 호출 실패")
                
                    # 각 종목 처리 사이에 약간의 딜레이 (0.1초)
                    await asyncio.sleep(0.1)
                
                # 10초마다 한 번씩 포지션 모니터링
                try:
                    monitor_task = asyncio.create_task(self._monitor_positions())
                    await asyncio.wait_for(monitor_task, timeout=3.0)
                except asyncio.TimeoutError:
                    logger.log_system("포지션 모니터링 타임아웃 (3초)")
                except Exception as e:
                    logger.log_error(e, "포지션 모니터링 실패")
                
                # 다음 주기까지 대기
                await asyncio.sleep(1)
                
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
            
            # 현재가 확인 - 가격 데이터가 없어도 로깅은 진행
            current_price = 0
            price_source = "없음"
            
            # 1. 먼저 price_data에서 가격 확인
            if symbol in self.price_data and self.price_data[symbol]:
                current_price = self.price_data[symbol][-1]["price"]
                price_source = "price_data"
                logger.log_system(f"[거래] {symbol} - price_data에서 가격 확인: {current_price:,}원")
            # 2. signals에서 last_price 확인
            elif symbol in self.signals and "last_price" in self.signals[symbol]:
                current_price = self.signals[symbol]["last_price"]
                price_source = "signals"
                logger.log_system(f"[거래] {symbol} - signals의 last_price에서 가격 확인: {current_price:,}원")
            # 3. API에서 직접 조회
            else:
                try:
                    from core.api_client import api_client
                    
                    # 타임아웃 설정
                    symbol_info_task = asyncio.create_task(api_client.get_symbol_info(symbol))
                    try:
                        symbol_info = await asyncio.wait_for(symbol_info_task, timeout=2.0)
                        if symbol_info and "current_price" in symbol_info:
                            current_price = float(symbol_info["current_price"])
                            price_source = "api_direct"
                            
                            # 테스트 데이터 여부 확인
                            if symbol_info.get("is_test_data", False):
                                logger.log_system(f"[거래] {symbol} - API에서 테스트 데이터 가격 확인: {current_price:,}원")
                            else:
                                logger.log_system(f"[거래] {symbol} - API에서 직접 가격 확인: {current_price:,}원")
                            
                            # 가격 데이터 캐싱
                            if symbol not in self.price_data:
                                self.price_data[symbol] = deque(maxlen=100)
                            
                            self.price_data[symbol].append({
                                "price": current_price,
                                "volume": symbol_info.get("volume", 0),
                                "timestamp": datetime.now()
                            })
                            
                            # signals에도 캐싱
                            if symbol in self.signals:
                                self.signals[symbol]["last_price"] = current_price
                            else:
                                self.signals[symbol] = {
                                    'score': 0, 
                                    'direction': "NEUTRAL",
                                    'strategies': {},
                                    'last_update': None,
                                    'last_price': current_price
                                }
                        else:
                            logger.log_system(f"[거래] {symbol} - API 응답에 가격 정보가 없음")
                    except asyncio.TimeoutError:
                        logger.log_system(f"[거래] {symbol} - API 가격 조회 타임아웃 (2초)")
                        
                except Exception as e:
                    logger.log_error(e, f"{symbol} - API에서 직접 가격 확인 실패")
            
            # 가격 정보 로깅
            if current_price > 0:
                logger.log_system(f"[거래] {symbol} - 현재가: {current_price:,}원 (출처: {price_source})")
            else:
                logger.log_system(f"[거래] {symbol} - 현재가를 확인할 수 없음 (출처: {price_source})")
                # 가격이 없으면 거래 판단 중단
                return
            
            # 전략 신호 강제 업데이트
            await self._update_signals(symbol)
            logger.log_system(f"[거래] {symbol} - 신호 강제 업데이트 완료")
            
            # 신호 계산
            try:
                if symbol not in self.signals or 'strategies' not in self.signals[symbol]:
                    logger.log_system(f"[거래] {symbol} - 신호 데이터 없음, 계산 건너뜀")
                    return
                    
                score, direction, agreements = self._calculate_combined_signal(symbol)
                logger.log_system(f"[거래] {symbol} - 신호 계산 결과: 점수={score:.1f}, 방향={direction}, 매수동의={agreements.get('BUY', 0)}, 매도동의={agreements.get('SELL', 0)}")
            except Exception as e:
                logger.log_error(e, f"계산 오류: {symbol} - _calculate_combined_signal 실패")
                score, direction, agreements = 0, "NEUTRAL", {"BUY": 0, "SELL": 0}
            
            # 매수/매도 기준값
            buy_threshold = self.params["buy_threshold"]
            sell_threshold = self.params["sell_threshold"]
            min_agreement = self.params["min_agreement"]
            
            # 매수/매도 판단 정보 trade.log에 자세히 기록
            strategies = self.signals[symbol]['strategies']
            
            # 디버그: 각 전략별 점수와 방향 로그 기록
            strategy_log = f"[거래판단] {symbol} - 전략별 점수: "
            for strat_name, strat_data in strategies.items():
                strategy_log += f"{strat_name}={strat_data.get('signal', 0):.1f}({strat_data.get('direction', 'NEUTRAL')}), "
            logger.log_system(strategy_log)
            
            # 평가 상태 결정
            evaluation_status = "NEUTRAL"
            failure_reason = []
            
            # 가격 0 체크
            if current_price <= 0:
                evaluation_status = "PRICE_ERROR"
                failure_reason.append(f"가격 오류 (price={current_price})")
                logger.log_system(f"[거래판단] {symbol} - 가격이 0 이하여서 거래 불가")
                return
                
            # 매수 조건 평가
            if direction == "BUY":
                # 스코어 검증
                if score < buy_threshold:
                    failure_reason.append(f"점수 부족 (score={score:.1f} < threshold={buy_threshold})")
                    evaluation_status = "WEAK_BUY"
                
                # 동의 개수 검증 
                if agreements["BUY"] < min_agreement:
                    failure_reason.append(f"동의 부족 (agree={agreements['BUY']} < min={min_agreement})")
                    evaluation_status = "FEW_BUY"
                
                # 두 조건 모두 통과하면 매수
                if score >= buy_threshold and agreements["BUY"] >= min_agreement:
                    evaluation_status = "STRONG_BUY"
                    
                    # 매수 조건 충족
                    logger.log_system(f"[거래판단] {symbol} - 매수 시그널 발생: 점수={score:.1f}, 동의수={agreements['BUY']}, 현재가={current_price:,}원")
                    await self._enter_position(symbol, "BUY", current_price, score, agreements)
                else:
                    # 매수 조건 미충족
                    reason_txt = ", ".join(failure_reason)
                    logger.log_system(f"[거래판단] {symbol} - 매수 검토 중 취소: {reason_txt}")
                    
            # 매도 조건 평가
            elif direction == "SELL":
                # 포지션 현황 체크
                current_position = 0
                for pos in symbol_positions:
                    current_position += pos.get("quantity", 0)
                
                # 포지션이 없으면 매도 무시
                if current_position <= 0:
                    evaluation_status = "NO_POSITION"
                    logger.log_system(f"[거래판단] {symbol} - 매도 신호이나 포지션 없음")
                else:
                    # 스코어 검증
                    if score < sell_threshold:
                        failure_reason.append(f"점수 부족 (score={score:.1f} < threshold={sell_threshold})")
                        evaluation_status = "WEAK_SELL"
                    
                    # 동의 개수 검증
                    if agreements["SELL"] < min_agreement:
                        failure_reason.append(f"동의 부족 (agree={agreements['SELL']} < min={min_agreement})")
                        evaluation_status = "FEW_SELL"
                    
                    # 두 조건 모두 통과하면 매도
                    if score >= sell_threshold and agreements["SELL"] >= min_agreement:
                        evaluation_status = "STRONG_SELL"
                        
                        # 매도 조건 충족
                        logger.log_system(f"[거래판단] {symbol} - 매도 시그널 발생: 점수={score:.1f}, 동의수={agreements['SELL']}, 현재가={current_price:,}원")
                        await self._exit_position(symbol, "SELL", current_position, current_price, score)
                    else:
                        # 매도 조건 미충족
                        reason_txt = ", ".join(failure_reason)
                        logger.log_system(f"[거래판단] {symbol} - 매도 검토 중 취소: {reason_txt}")
            
            # 중립 신호인 경우
            else:
                logger.log_system(f"[거래판단] {symbol} - 중립 신호: 점수={score:.1f}, 방향={direction}")
                evaluation_status = "NEUTRAL"
            
            # 거래 판단 결과 저장
            self.signals[symbol]["evaluation"] = {
                "status": evaluation_status,
                "reason": failure_reason,
                "timestamp": datetime.now()
            }
            
        except Exception as e:
            logger.log_error(e, f"Error checking and trading for {symbol}")
    
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
            # 포지션 수 확인
            current_positions = self._get_symbol_positions(symbol)
            if len(current_positions) >= self.params["max_positions"]:
                logger.log_system(f"[TRADE_SKIP] {symbol} - 이미 최대 포지션({self.params['max_positions']}개) 도달, 추가 매수 건너뜀")
                logger.log_trade(
                    action=f"{side}_SKIPPED",
                    symbol=symbol,
                    price=price,
                    quantity=0,
                    reason=f"최대 포지션 수 도달 ({len(current_positions)}/{self.params['max_positions']}개)",
                    score=f"{score:.1f}",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="SKIP"
                )
                return
            
            # 가격 유효성 재확인
            if price <= 0:
                logger.log_system(f"[TRADE_ERROR] {symbol} - 가격이 0 이하 ({price}), 매매 불가")
                logger.log_trade(
                    action=f"{side}_ERROR",
                    symbol=symbol,
                    price=price,
                    quantity=0,
                    reason=f"유효하지 않은 가격: {price}",
                    score=f"{score:.1f}",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="ERROR"
                )
                return
            
            # 주문 수량 계산
            position_size = self.params["position_size"]  # 100만원
            quantity = int(position_size / price)
            
            if quantity <= 0:
                logger.log_system(f"[TRADE_SKIP] {symbol} - 수량이 0 이하 ({quantity}), 매매 건너뜀")
                logger.log_trade(
                    action=f"{side}_SKIPPED",
                    symbol=symbol,
                    price=price,
                    quantity=quantity,
                    reason=f"주문 수량이 0 이하 ({quantity})",
                    score=f"{score:.1f}",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="SKIP"
                )
                return
            
            # 주문 실행 시도 로그
            logger.log_system(f"[TRADE_TRY] {symbol} - {side} 주문 시도: 가격={price}, 수량={quantity}, 점수={score:.1f}")
            
            # 실제 주문 실행
            try:
                logger.log_system(f"[DEBUG] {symbol} - order_manager.place_order 호출 시작")
                
                # 주문 실행 시 타임아웃 설정
                order_task = asyncio.create_task(
                    order_manager.place_order(
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                        order_type="MARKET",
                        strategy="combined",
                        reason=f"combined_signal_{score:.1f}"
                    )
                )
                
                # 2초 타임아웃 설정
                result = await asyncio.wait_for(order_task, timeout=5.0)
                logger.log_system(f"[DEBUG] {symbol} - order_manager.place_order 결과: {result}")
                
            except asyncio.TimeoutError:
                logger.log_system(f"[TRADE_ERROR] {symbol} - order_manager.place_order 호출 타임아웃 (5초)")
                logger.log_trade(
                    action=f"{side}_TIMEOUT",
                    symbol=symbol,
                    price=price,
                    quantity=quantity,
                    reason="주문 API 타임아웃 (5초)",
                    score=f"{score:.1f}",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="ERROR"
                )
                return
            except Exception as e:
                logger.log_error(e, f"{symbol} - order_manager.place_order 호출 실패")
                logger.log_trade(
                    action=f"{side}_FAILED",
                    symbol=symbol,
                    price=price,
                    quantity=quantity,
                    reason=f"API 호출 오류: {str(e)}",
                    score=f"{score:.1f}",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="ERROR"
                )
                return
            
            # 주문 결과 확인
            if not result:
                logger.log_system(f"[TRADE_FAIL] {symbol} - {side} 주문 결과가 None입니다. order_manager가 제대로 초기화되지 않았을 수 있습니다.")
                logger.log_trade(
                    action=f"{side}_FAILED",
                    symbol=symbol,
                    price=price,
                    quantity=quantity,
                    reason="order_manager 응답 없음",
                    score=f"{score:.1f}",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="ERROR"
                )
                return
            
            if result.get("status") == "success":
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
                    f"※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※"
                )
                logger.log_system(
                    f"[{side}_SUCCESS] {symbol} - {quantity}주 {price}원에 성공, "
                    f"점수: {score:.1f}, 합의수: {sum(agreements.values())}, "
                    f"손절가: {stop_price:.0f}, 익절가: {target_price:.0f}"
                )
                logger.log_system(
                    f"※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※※"
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
            else:
                # 주문 실패 로그
                error_msg = result.get("message", "알 수 없는 오류")
                logger.log_system(f"[{side}_FAIL] {symbol} - 주문 실패: {error_msg}")
                logger.log_trade(
                    action=f"{side}_FAILED",
                    symbol=symbol,
                    price=price,
                    quantity=quantity,
                    reason=f"주문 실패: {error_msg}",
                    score=f"{score:.1f}",
                    time=datetime.now().strftime("%H:%M:%S"),
                    status="FAIL"
                )
                
        except Exception as e:
            logger.log_error(e, f"Combined strategy entry error for {symbol}")
            logger.log_trade(
                action=f"{side}_ERROR",
                symbol=symbol,
                price=price,
                quantity=0,
                reason=f"처리 중 오류 발생: {str(e)}",
                score=f"{score:.1f}",
                time=datetime.now().strftime("%H:%M:%S"),
                status="ERROR"
            )
    
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
        """전략 상태 정보 반환 - 항상 최신 신호 계산"""
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
                pos_symbol = pos["symbol"]
                result["position_details"][pos_id] = {
                    "symbol": pos_symbol,
                    "side": pos["side"],
                    "entry_price": pos["entry_price"],
                    "stop_price": pos["stop_price"],
                    "target_price": pos["target_price"],
                    "entry_time": pos["entry_time"].strftime("%H:%M:%S"),
                    "hold_time": (datetime.now() - pos["entry_time"]).total_seconds() / 60
                }
            
            # 특정 심볼에 대한 상세 정보 요청인 경우 - 강제 업데이트
            if symbol:
                logger.log_system(f"[전략] get_strategy_status - {symbol} 신호 강제 업데이트 시작")
                
                # 비동기 함수를 동기적으로 실행하기 위한 이벤트 루프 얻기
                try:
                    # 현재 이벤트 루프 가져오기 시도
                    loop = asyncio.get_event_loop()
                    
                    # 루프가 실행중인지 확인
                    if loop.is_running():
                        # 이미 실행 중인 루프에서는 Future를 생성하여 실행
                        try:
                            future = asyncio.run_coroutine_threadsafe(self._update_signals(symbol), loop)
                            # 타임아웃 증가: 2초 → 5초
                            future.result(timeout=5.0)  # 타임아웃 시간 증가
                            logger.log_system(f"[전략] {symbol} - run_coroutine_threadsafe로 신호 업데이트 완료")
                        except concurrent.futures._base.TimeoutError:
                            # 타임아웃 발생 시 기존 신호 정보 사용
                            logger.log_system(f"[전략] {symbol} - 신호 업데이트 타임아웃 발생, 기존 신호 정보 사용")
                            # 오류 대신 경고로 처리
                            logger.log_warning(f"[전략] {symbol} - 신호 업데이트 타임아웃")
                    else:
                        try:
                            # 타임아웃 설정 추가
                            async def run_with_timeout():
                                # 비동기 타임아웃을 사용하여 신호 업데이트 실행
                                try:
                                    await asyncio.wait_for(self._update_signals(symbol), timeout=5.0)
                                    return True
                                except asyncio.TimeoutError:
                                    logger.log_system(f"[전략] {symbol} - wait_for 타임아웃 발생, 기존 신호 정보 사용")
                                    return False
                            
                            # 타임아웃 설정으로 실행
                            update_success = loop.run_until_complete(run_with_timeout())
                            if update_success:
                                logger.log_system(f"[전략] {symbol} - run_until_complete로 신호 업데이트 완료")
                            else:
                                logger.log_warning(f"[전략] {symbol} - 신호 업데이트 타임아웃")
                        except Exception as timeout_error:
                            logger.log_warning(f"[전략] {symbol} - 신호 업데이트 중 예외 발생: {str(timeout_error)}")
                except Exception as loop_error:
                    # 오류 레벨 변경 (error → warning)
                    logger.log_warning(f"[전략] {symbol} - 이벤트 루프 처리 중 오류: {str(loop_error)}")
                    # 이벤트 루프 오류가 발생해도 저장된 신호 데이터는 반환
                
                # 개별 전략 신호 (없으면 초기화)
                if symbol not in self.signals:
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
                        'last_update': datetime.now()
                    }
                
                # 통합 신호 다시 계산 (자체 타이머에 기능 제한 가능성이 있으므로 명시적 계산)
                score, direction, agreements = self._calculate_combined_signal(symbol)
                
                # 통합 신호 결과 업데이트
                self.signals[symbol]["combined_signal"] = score
                self.signals[symbol]["score"] = score  # score 키 추가
                self.signals[symbol]["direction"] = direction
                self.signals[symbol]["agreements"] = agreements
                self.signals[symbol]["last_update"] = datetime.now()
                
                logger.log_system(f"[전략] get_strategy_status - {symbol} 통합 신호 재계산 완료: 점수={score:.1f}, 방향={direction}")
                
                # 새로 계산된 결과 반환을 위해 설정
                result["signals"][symbol] = {
                    "score": score,
                    "direction": direction,
                    "agreements": agreements,
                    "strategies": self.signals[symbol]["strategies"]
                }
                
            # 아니면 모든 심볼의 요약 정보 (저장된 데이터 기반)
            elif not symbol:
                for sym in self.watched_symbols:
                    if sym in self.signals:
                        # "score" 키가 없는 경우 "combined_signal" 값 사용
                        if "score" in self.signals[sym]:
                            score_value = self.signals[sym]["score"]
                        else:
                            score_value = self.signals[sym].get("combined_signal", 0)
                            
                        result["signals"][sym] = {
                            "score": score_value,
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