"""
리스크 관리 모듈
트레이딩 시스템의 자금 관리와 리스크 관리를 담당합니다.
"""
import asyncio
import threading
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta
import time

from config.settings import config
from core.api_client import api_client
from core.account_state import account_state
from utils.logger import logger
from utils.database import database_manager


class RiskManager:
    """리스크 관리 클래스"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        """싱글톤 패턴 구현"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """초기화"""
        if not hasattr(self, '_initialized') or not self._initialized:
            # 설정 로드
            self.risk_params = config["trading"].risk_params
            
            # 트레이딩 성과 추적
            self.daily_profit_loss = 0
            self.daily_trades = 0
            self.max_drawdown = 0
            self.win_count = 0
            self.loss_count = 0
            
            # 포지션 관련 데이터
            self.open_positions = {}  # {symbol: position_data}
            self.position_history = []  # 최근 포지션 이력
            
            # 리스크 제한
            self.daily_risk_used = 0  # 하루 사용된 리스크 금액
            self.max_daily_risk = self.risk_params.get("max_daily_risk", 100000)  # 일일 최대 리스크 금액
            
            # 변동성 추적
            self.symbol_volatility = {}  # {symbol: volatility}
            
            self._initialized = True
            logger.log_system("리스크 관리자 초기화 완료")
    
    async def initialize(self):
        """리스크 관리자 초기화"""
        try:
            # 계좌 상태 관리자 초기화 확인
            if not hasattr(account_state, 'initialized') or not account_state.initialized:
                await account_state.initialize()
            
            # DB에서 포지션 로드
            db_positions = database_manager.get_all_positions()
            for position in db_positions:
                self.open_positions[position["symbol"]] = position
            
            # 일일 성과 리셋
            self._reset_daily_stats()
            
            logger.log_system("리스크 관리자 초기화 완료")
        except Exception as e:
            logger.log_error(e, "리스크 관리자 초기화 실패")
            raise
    
    def _reset_daily_stats(self):
        """일일 통계 초기화"""
        self.daily_profit_loss = 0
        self.daily_trades = 0
        self.daily_risk_used = 0
    
    async def calculate_position_size(self, symbol: str, side: str, 
                                     current_price: float, 
                                     strategy: str = None,
                                     signal_strength: float = 5.0) -> Tuple[int, float]:
        """리스크 기반 포지션 크기 계산
        
        Args:
            symbol: 종목코드
            side: 매수/매도 구분 (BUY/SELL)
            current_price: 현재가
            strategy: 전략 이름
            signal_strength: 신호 강도 (0~10)
            
        Returns:
            Tuple[int, float]: (주문 수량, 주문 금액)
        """
        try:
            # 1. 계좌 정보 조회 (실시간 동기화)
            account_info = await account_state.get_account_info()
            available_cash = account_info["available_cash"]
            
            # 계좌가 비어있거나 자금이 부족한 경우
            if available_cash <= 0:
                logger.log_warning(f"{symbol} - 계좌 잔고 부족: {available_cash:,.0f}원")
                return 0, 0
            
            # 2. 기본 포지션 크기 설정 (계좌 자금의 일정 비율)
            account_ratio = min(self.risk_params.get("position_size_ratio", 0.1), 0.2)  # 최대 20%
            base_position_size = available_cash * account_ratio
            
            # 3. 리스크 한도 체크
            max_position_size = self.risk_params.get("max_position_size", 10000000)  # 기본 1천만원
            max_position_per_symbol = self.risk_params.get("max_position_per_symbol", 5000000)  # 기본 5백만원
            
            # 4. 변동성에 따른 포지션 크기 조정
            volatility = await self._get_symbol_volatility(symbol)
            volatility_factor = self._calculate_volatility_factor(volatility)
            
            # 5. 신호 강도에 따른 포지션 크기 조정
            signal_factor = self._calculate_signal_factor(signal_strength)
            
            # 6. 전략 성과에 따른 포지션 크기 조정
            strategy_factor = await self._get_strategy_performance_factor(strategy)
            
            # 7. 종목 성과에 따른 포지션 크기 조정
            symbol_factor = await self._get_symbol_performance_factor(symbol)
            
            # 8. 일중 리스크 사용량 체크
            daily_risk_left = self.max_daily_risk - self.daily_risk_used
            if daily_risk_left <= 0:
                logger.log_warning(f"일일 리스크 한도 초과: {self.daily_risk_used:,.0f}원/{self.max_daily_risk:,.0f}원")
                return 0, 0
            
            # 9. 최종 포지션 크기 계산
            adjusted_size = base_position_size * volatility_factor * signal_factor * strategy_factor * symbol_factor
            
            # 10. 한도 적용
            final_position_size = min(
                adjusted_size,
                max_position_size,
                max_position_per_symbol,
                daily_risk_left
            )
            
            # 11. 주문 수량 계산
            quantity = int(final_position_size / current_price)
            
            # 12. 실제 주문 금액 계산 (수량 * 현재가)
            order_amount = quantity * current_price
            
            # 13. 로그 기록
            logger.log_system(
                f"[포지션계산] {symbol} - "
                f"계좌잔고: {available_cash:,.0f}원, "
                f"기본금액: {base_position_size:,.0f}원, "
                f"변동성계수: {volatility_factor:.2f}, "
                f"신호계수: {signal_factor:.2f}, "
                f"전략계수: {strategy_factor:.2f}, "
                f"종목계수: {symbol_factor:.2f}, "
                f"최종금액: {final_position_size:,.0f}원, "
                f"주문수량: {quantity}주"
            )
            
            return quantity, order_amount
            
        except Exception as e:
            logger.log_error(e, f"{symbol} 포지션 크기 계산 중 오류")
            return 0, 0
    
    async def _get_symbol_volatility(self, symbol: str) -> float:
        """종목의 변동성 계산"""
        try:
            # 캐시된 변동성이 있으면 사용
            if symbol in self.symbol_volatility:
                return self.symbol_volatility[symbol]
            
            # 일봉 데이터 조회
            daily_data = api_client.get_daily_price(symbol, 20)
            
            if not daily_data or daily_data.get("rt_cd") != "0":
                # 실패 시 기본값 반환
                return 0.02  # 기본 변동성 2%
            
            # 일별 종가 추출
            prices = []
            chart_data = daily_data.get("output2", [])
            
            for item in chart_data:
                if "stck_clpr" in item:
                    prices.append(float(item["stck_clpr"]))
            
            if len(prices) < 2:
                return 0.02  # 데이터 부족 시 기본값
            
            # 일별 수익률 계산
            returns = []
            for i in range(1, len(prices)):
                daily_return = (prices[i] - prices[i-1]) / prices[i-1]
                returns.append(daily_return)
            
            # 변동성 계산 (일별 수익률의 표준편차)
            import numpy as np
            volatility = np.std(returns)
            
            # 연간 변동성으로 변환 (일별 변동성 * sqrt(252))
            annualized_volatility = volatility * (252 ** 0.5)
            
            # 캐시에 저장
            self.symbol_volatility[symbol] = annualized_volatility
            
            return annualized_volatility
            
        except Exception as e:
            logger.log_error(e, f"{symbol} 변동성 계산 중 오류")
            return 0.02  # 오류 시 기본값
    
    def _calculate_volatility_factor(self, volatility: float) -> float:
        """변동성에 따른 포지션 크기 조정 계수 계산"""
        # 변동성이 높을수록 포지션 크기 감소
        max_allowed_volatility = self.risk_params.get("max_volatility", 0.5)  # 최대 허용 변동성 50%
        
        if volatility >= max_allowed_volatility:
            return 0.1  # 변동성이 너무 높으면 최소 계수 적용
        
        # 기본 변동성(10%)을 기준으로 계수 계산
        base_volatility = 0.10
        if volatility <= base_volatility:
            return 1.0  # 기본 변동성 이하면 100% 적용
        
        # 변동성에 반비례하여 계수 감소
        return max(0.1, base_volatility / volatility)
    
    def _calculate_signal_factor(self, signal_strength: float) -> float:
        """신호 강도에 따른 포지션 크기 조정 계수 계산"""
        # 신호 강도가 높을수록 포지션 크기 증가
        if signal_strength <= 0:
            return 0  # 신호 없음
        
        # 0~10 범위의 신호를 0.2~1.0 범위의 계수로 변환
        return 0.2 + (signal_strength / 10) * 0.8
    
    async def _get_strategy_performance_factor(self, strategy: str) -> float:
        """전략 성과에 따른 포지션 크기 조정 계수 계산"""
        if not strategy:
            return 0.8  # 전략 정보 없으면 기본값
        
        try:
            # 최근 전략 성과 조회 (DB에서)
            recent_trades = database_manager.get_recent_trades_by_strategy(strategy, limit=20)
            
            if not recent_trades:
                return 0.8  # 거래 이력 없으면 기본값
            
            # 승률 계산
            wins = sum(1 for trade in recent_trades if trade.get("pnl", 0) > 0)
            total = len(recent_trades)
            win_rate = wins / total if total > 0 else 0.5
            
            # 평균 수익률 계산
            if total > 0:
                avg_profit = sum(trade.get("pnl", 0) for trade in recent_trades) / total
                avg_profit_rate = avg_profit / sum(trade.get("price", 0) * trade.get("quantity", 0) for trade in recent_trades) if sum(trade.get("price", 0) * trade.get("quantity", 0) for trade in recent_trades) > 0 else 0
            else:
                avg_profit_rate = 0
            
            # 성과 계수 계산 (승률과 평균 수익률 고려)
            # 승률 50% 이상이면 보너스, 미만이면 페널티
            win_factor = 0.5 + win_rate
            
            # 평균 수익률이 양수면 보너스, 음수면 페널티
            profit_factor = 1.0 + (avg_profit_rate * 5.0)  # 수익률 영향 증폭
            
            # 최종 계수 (0.3~2.0 범위로 제한)
            return max(0.3, min(2.0, win_factor * profit_factor))
            
        except Exception as e:
            logger.log_error(e, f"{strategy} 전략 성과 계산 중 오류")
            return 0.8  # 오류 시 기본값
    
    async def _get_symbol_performance_factor(self, symbol: str) -> float:
        """종목 성과에 따른 포지션 크기 조정 계수 계산"""
        try:
            # 최근 종목 거래 이력 조회
            recent_trades = database_manager.get_recent_trades_by_symbol(symbol, limit=10)
            
            if not recent_trades:
                return 1.0  # 거래 이력 없으면 기본값
            
            # 승률 계산
            wins = sum(1 for trade in recent_trades if trade.get("pnl", 0) > 0)
            total = len(recent_trades)
            win_rate = wins / total if total > 0 else 0.5
            
            # 승률에 따른 계수 계산
            if win_rate >= 0.7:  # 70% 이상 승률
                return 1.2
            elif win_rate >= 0.5:  # 50% 이상 승률
                return 1.0
            elif win_rate >= 0.3:  # 30% 이상 승률
                return 0.8
            else:  # 저조한 승률
                return 0.5
            
        except Exception as e:
            logger.log_error(e, f"{symbol} 종목 성과 계산 중 오류")
            return 1.0  # 오류 시 기본값
    
    async def check_risk_limits(self, symbol: str, side: str, 
                               quantity: int, price: float) -> Dict[str, Any]:
        """리스크 한도 검증
        
        Args:
            symbol: 종목코드
            side: 매수/매도 구분 (BUY/SELL)
            quantity: 주문 수량
            price: 주문 가격
            
        Returns:
            Dict[str, Any]: 검증 결과 ({"allowed": bool, "reason": str})
        """
        try:
            # 주문 금액 계산
            order_amount = quantity * price
            
            # 1. 최대 포지션 크기 검증
            max_position_size = self.risk_params.get("max_position_size", 10000000)
            if order_amount > max_position_size:
                return {
                    "allowed": False,
                    "reason": f"주문금액({order_amount:,.0f}원)이 최대 포지션 크기({max_position_size:,.0f}원)를 초과합니다."
                }
            
            # 2. 종목별 최대 포지션 크기 검증
            max_position_per_symbol = self.risk_params.get("max_position_per_symbol", 5000000)
            existing_position = self.open_positions.get(symbol, {"quantity": 0, "avg_price": 0})
            
            if side == "BUY":
                total_position_size = (existing_position.get("quantity", 0) + quantity) * price
                if total_position_size > max_position_per_symbol:
                    return {
                        "allowed": False,
                        "reason": f"총 포지션 크기({total_position_size:,.0f}원)가 종목별 최대 포지션({max_position_per_symbol:,.0f}원)을 초과합니다."
                    }
            
            # 3. 최대 포지션 개수 검증
            max_open_positions = self.risk_params.get("max_open_positions", 10)
            current_position_count = len(self.open_positions)
            
            if side == "BUY" and symbol not in self.open_positions and current_position_count >= max_open_positions:
                return {
                    "allowed": False,
                    "reason": f"최대 포지션 개수({max_open_positions}개)에 도달했습니다."
                }
            
            # 4. 일일 리스크 한도 검증
            if side == "BUY":
                potential_risk = order_amount * self.risk_params.get("max_loss_rate", 0.02)  # 최대 손실률 (기본 2%)
                if self.daily_risk_used + potential_risk > self.max_daily_risk:
                    return {
                        "allowed": False,
                        "reason": f"일일 리스크 한도({self.max_daily_risk:,.0f}원)에 도달했습니다."
                    }
            
            # 5. 종목 변동성 검증
            volatility = await self._get_symbol_volatility(symbol)
            max_allowed_volatility = self.risk_params.get("max_volatility", 0.5)
            
            if volatility > max_allowed_volatility:
                return {
                    "allowed": False,
                    "reason": f"종목 변동성({volatility:.2%})이 최대 허용 변동성({max_allowed_volatility:.2%})을 초과합니다."
                }
            
            # 모든 검증 통과
            return {
                "allowed": True,
                "reason": "모든 리스크 검증 통과"
            }
            
        except Exception as e:
            logger.log_error(e, f"{symbol} 리스크 한도 검증 중 오류")
            return {
                "allowed": False,
                "reason": f"리스크 검증 중 오류: {str(e)}"
            }
    
    async def update_after_trade(self, trade_data: Dict[str, Any]) -> None:
        """거래 후 리스크 관리자 업데이트"""
        try:
            symbol = trade_data.get("symbol")
            side = trade_data.get("side")
            quantity = trade_data.get("quantity", 0)
            price = trade_data.get("price", 0)
            pnl = trade_data.get("pnl", 0)
            
            # 일일 성과 업데이트
            self.daily_trades += 1
            if pnl:
                self.daily_profit_loss += pnl
                if pnl > 0:
                    self.win_count += 1
                else:
                    self.loss_count += 1
            
            # 포지션 업데이트
            if side == "BUY":
                # 매수 시 리스크 사용량 증가
                risk_amount = price * quantity * self.risk_params.get("max_loss_rate", 0.02)
                self.daily_risk_used += risk_amount
                
                # 포지션 추가/업데이트
                if symbol in self.open_positions:
                    current_position = self.open_positions[symbol]
                    new_quantity = current_position.get("quantity", 0) + quantity
                    new_avg_price = ((current_position.get("quantity", 0) * current_position.get("avg_price", 0)) + 
                                    (quantity * price)) / new_quantity
                    
                    self.open_positions[symbol] = {
                        "symbol": symbol,
                        "quantity": new_quantity,
                        "avg_price": new_avg_price,
                        "last_update": datetime.now()
                    }
                else:
                    self.open_positions[symbol] = {
                        "symbol": symbol,
                        "quantity": quantity,
                        "avg_price": price,
                        "last_update": datetime.now()
                    }
            
            elif side == "SELL":
                # 포지션 감소/제거
                if symbol in self.open_positions:
                    current_position = self.open_positions[symbol]
                    new_quantity = current_position.get("quantity", 0) - quantity
                    
                    if new_quantity <= 0:
                        # 포지션 청산
                        del self.open_positions[symbol]
                    else:
                        # 포지션 감소
                        self.open_positions[symbol]["quantity"] = new_quantity
                        self.open_positions[symbol]["last_update"] = datetime.now()
            
            # 포지션 이력 추가
            self.position_history.append({
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "price": price,
                "pnl": pnl,
                "timestamp": datetime.now()
            })
            
            # 이력 크기 제한 (최근 100개만 유지)
            if len(self.position_history) > 100:
                self.position_history = self.position_history[-100:]
            
        except Exception as e:
            logger.log_error(e, f"거래 후 리스크 관리자 업데이트 중 오류")
    
    def get_risk_metrics(self) -> Dict[str, Any]:
        """현재 리스크 메트릭 조회"""
        return {
            "daily_profit_loss": self.daily_profit_loss,
            "daily_trades": self.daily_trades,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": self.win_count / (self.win_count + self.loss_count) if (self.win_count + self.loss_count) > 0 else 0,
            "open_positions": len(self.open_positions),
            "daily_risk_used": self.daily_risk_used,
            "daily_risk_limit": self.max_daily_risk,
            "risk_usage_pct": (self.daily_risk_used / self.max_daily_risk * 100) if self.max_daily_risk > 0 else 0
        }

# 싱글톤 인스턴스
risk_manager = RiskManager() 