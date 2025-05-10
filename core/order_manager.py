"""
주문 관리자
"""
import asyncio
import threading
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from config.settings import config
from core.api_client import api_client
from utils.logger import logger
from utils.database import database_manager
from monitoring.alert_system import alert_system

class OrderManager:
    """주문 관리자"""

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
            self.trading_config = config["trading"]
            self.positions = {}  # {symbol: position_data}
            self.pending_orders = {}  # {order_id: order_data}
            self.daily_pnl = 0
            self.daily_trades = 0
            self.trading_paused = False  # 거래 일시 중지 플래그
            self._async_lock = asyncio.Lock()  # 비동기 작업을 위한 락
            self._initialized = True
        
    async def initialize(self):
        """초기화 - 포지션/잔고 로드"""
        try:
            # 계좌 잔고 조회
            balance_data = api_client.get_account_balance()
            
            # DB에서 포지션 로드
            db_positions = database_manager.get_all_positions()
            
            # 포지션 동기화
            for position in db_positions:
                self.positions[position["symbol"]] = position
            
            # DB에서 시스템 상태 확인하여 거래 일시 중지 상태 초기화
            system_status = database_manager.get_system_status()
            if system_status and system_status.get("status") == "PAUSED":
                self.trading_paused = True
                logger.log_system("Trading initialized in paused state")
            else:
                self.trading_paused = False
            
            logger.log_system("Order manager initialized successfully")
            
        except Exception as e:
            logger.log_error(e, "Failed to initialize order manager")
            raise
    
    def is_trading_paused(self) -> bool:
        """거래 일시 중지 상태 반환"""
        return self.trading_paused
    
    def pause_trading(self) -> bool:
        """거래 일시 중지"""
        if not self.trading_paused:
            self.trading_paused = True
            logger.log_system("Trading has been paused")
            return True
        return False
    
    def resume_trading(self) -> bool:
        """거래 재개"""
        if self.trading_paused:
            self.trading_paused = False
            logger.log_system("Trading has been resumed")
            return True
        return False
    
    async def place_order(self, symbol: str, side: str, quantity: int, 
                         price: float = None, order_type: str = "MARKET",
                         strategy: str = None, reason: str = None, 
                         bypass_pause: bool = False) -> Dict[str, Any]:
        """주문 실행"""
        try:
            # 거래 일시 중지 상태 확인 (bypass_pause가 False이고 거래가 일시 중지된 경우)
            if not bypass_pause and self.trading_paused:
                # 전략에 의한 자동 거래 (reason에 'user_request'가 없는 경우)인 경우만 거부
                if not reason or 'user_request' not in reason:
                    logger.log_system(f"Order rejected for {symbol}: trading is paused")
                    return {"status": "rejected", "reason": "trading_paused"}
            
            # 리스크 체크
            if not await self._check_risk(symbol, side, quantity, price):
                return {"status": "rejected", "reason": "risk_check_failed"}
            
            # 주문 실행
            if order_type.upper() == "MARKET":
                order_result = api_client.place_order(
                    symbol=symbol,
                    order_type="MARKET",
                    side=side,
                    quantity=quantity
                )
            else:
                if price is None:
                    price_data = api_client.get_current_price(symbol)
                    price = float(price_data["output"]["stck_prpr"])
                
                order_result = api_client.place_order(
                    symbol=symbol,
                    order_type="LIMIT",
                    side=side,
                    quantity=quantity,
                    price=int(price)
                )
            
            # 주문 성공
            if order_result.get("rt_cd") == "0":
                order_id = order_result["output"]["ODNO"]
                
                # 주문 데이터 저장
                order_data = {
                    "order_id": order_id,
                    "symbol": symbol,
                    "side": side,
                    "order_type": order_type,
                    "price": price,
                    "quantity": quantity,
                    "status": "PENDING",
                    "strategy": strategy,
                    "reason": reason
                }
                
                database_manager.save_order(order_data)
                self.pending_orders[order_id] = order_data
                
                # 포지션 업데이트
                await self.update_position(symbol, side, quantity, price)
                
                # 거래 기록 저장 (trades 테이블)
                trade_data = {
                    "symbol": symbol,
                    "side": side,
                    "price": price,
                    "quantity": quantity,
                    "order_type": order_type,
                    "status": "FILLED",  # 시장가 주문은 즉시 체결로 간주
                    "order_id": order_id,
                    "commission": price * quantity * 0.0005,  # 예상 수수료 (0.05%)
                    "strategy": strategy,
                    "reason": reason
                }
                
                # 매도인 경우 실현 손익 계산
                if side == "SELL":
                    current_position = self.positions.get(symbol, {"avg_price": 0})
                    pnl = (price - current_position.get("avg_price", 0)) * quantity
                    trade_data["pnl"] = pnl
                
                # 트레이드 DB에 저장
                database_manager.save_trade(trade_data)
                
                # 알림 전송
                await alert_system.notify_trade(trade_data)
                
                self.daily_trades += 1
                
                return {"status": "success", "order_id": order_id, "trade_data": trade_data}
            
            else:
                error_msg = order_result.get("msg1", "Unknown error")
                logger.log_error(
                    Exception(error_msg),
                    f"Order failed for {symbol}"
                )
                return {"status": "failed", "reason": error_msg}
            
        except Exception as e:
            logger.log_error(e, "Order placement error")
            return {"status": "error", "reason": str(e)}
    
    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """주문 취소"""
        try:
            order_data = self.pending_orders.get(order_id)
            if not order_data:
                return {"status": "failed", "reason": "order_not_found"}
            
            result = api_client.cancel_order(
                order_id=order_id,
                symbol=order_data["symbol"],
                quantity=order_data["quantity"]
            )
            
            if result.get("rt_cd") == "0":
                # 주문 상태 업데이트
                database_manager.update_order(order_id, {"status": "CANCELLED"})
                del self.pending_orders[order_id]
                
                return {"status": "success"}
            else:
                return {"status": "failed", "reason": result.get("msg1")}
            
        except Exception as e:
            logger.log_error(e, f"Order cancellation error: {order_id}")
            return {"status": "error", "reason": str(e)}
    
    async def update_position(self, symbol: str, side: str, quantity: int, 
                            price: float):
        """포지션 업데이트"""
        try:
            current_position = self.positions.get(symbol, {
                "symbol": symbol,
                "quantity": 0,
                "avg_price": 0,
                "realized_pnl": 0
            })
            
            # 이전 포지션 정보 저장 (로깅용)
            old_quantity = current_position.get("quantity", 0)
            old_avg_price = current_position.get("avg_price", 0)
            
            if side == "BUY":
                # 매수 - 평균가 계산
                new_quantity = current_position["quantity"] + quantity
                if new_quantity > 0:
                    new_avg_price = (
                        (current_position["quantity"] * current_position["avg_price"]) +
                        (quantity * price)
                    ) / new_quantity
                    current_position["quantity"] = new_quantity
                    current_position["avg_price"] = new_avg_price
                else:
                    current_position["quantity"] = 0
                    current_position["avg_price"] = 0
                
            else:  # SELL
                # 매도 - 실현손익 계산
                sell_quantity = min(quantity, current_position["quantity"])
                if sell_quantity > 0:
                    realized_pnl = sell_quantity * (price - current_position["avg_price"])
                    current_position["realized_pnl"] += realized_pnl
                    self.daily_pnl += realized_pnl
                
                current_position["quantity"] -= quantity
                
                # 포지션 청산된 경우
                if current_position["quantity"] <= 0:
                    current_position["quantity"] = 0
                    current_position["avg_price"] = 0
            
            # DB 업데이트
            database_manager.save_position(current_position)
            
            # 메모리 업데이트
            if current_position["quantity"] > 0:
                self.positions[symbol] = current_position
            else:
                if symbol in self.positions:
                    del self.positions[symbol]
            
            # 포지션 변화 로깅
            change_description = ""
            if side == "BUY":
                change_description = f"증가: {old_quantity} → {current_position['quantity']} 주, 평단가: {old_avg_price:,.0f} → {current_position['avg_price']:,.0f} 원"
            else:
                change_description = f"감소: {old_quantity} → {current_position['quantity']} 주"
                if current_position["quantity"] == 0:
                    change_description += " (포지션 청산)"
            
            logger.log_system(f"Position updated for {symbol}: {change_description}")
            
        except Exception as e:
            logger.log_error(e, f"Position update error: {symbol}")
    
    async def _check_risk(self, symbol: str, side: str, quantity: int, 
                         price: float = None) -> bool:
        """리스크 체크"""
        try:
            # 루프 및 실행기 가져오기
            loop = asyncio.get_event_loop()
            
            # 현재가 조회 (price가 None인 경우에만)
            if price is None:
                try:
                    # run_in_executor를 사용하여 비동기적으로 동기 함수 호출
                    price_data = await loop.run_in_executor(None, 
                                                           lambda: api_client.get_current_price(symbol))
                    if price_data.get("rt_cd") == "0" and "output" in price_data:
                        price = float(price_data["output"]["stck_prpr"])
                    else:
                        logger.log_error(
                            Exception(f"Failed to get current price: {price_data.get('msg1', 'Unknown error')}"),
                            f"Risk check error for {symbol}"
                        )
                        return False
                except Exception as e:
                    logger.log_error(e, f"Error getting current price for {symbol}")
                    return False
            
            # 포지션 사이즈 체크
            position_value = quantity * price
            if position_value > self.trading_config.max_position_size:
                logger.log_system(
                    f"Risk check failed: Position size too large for {symbol} - "
                    f"Value: {position_value:,.0f}원, Limit: {self.trading_config.max_position_size:,.0f}원"
                )
                return False
            
            # 일일 손실 한도 체크
            daily_loss_limit = -self.trading_config.max_loss_rate * 100000  # 손실한도 10만원
            if self.daily_pnl < daily_loss_limit:
                logger.log_system(
                    f"Risk check failed: Daily loss limit reached - "
                    f"Current Loss: {self.daily_pnl:,.0f}원, Limit: {daily_loss_limit:,.0f}원"
                )
                return False
            
            # 종목별 최대 포지션 수량 체크
            current_position = self.positions.get(symbol, {"quantity": 0})
            max_position_per_symbol = getattr(self.trading_config, "max_position_per_symbol", 1000)
            if side == "BUY" and current_position["quantity"] + quantity > max_position_per_symbol:
                logger.log_system(
                    f"Risk check failed: Max position per symbol reached for {symbol} - "
                    f"Current: {current_position['quantity']}주, Adding: {quantity}주, Limit: {max_position_per_symbol}주"
                )
                return False
            
            # 변동성 체크
            try:
                # run_in_executor를 사용하여 비동기적으로 동기 함수 호출
                price_data = await loop.run_in_executor(None, 
                                                      lambda: api_client.get_daily_price(symbol))
                
                if price_data.get("rt_cd") == "0" and "output2" in price_data:
                    prices = []
                    # 최근 7일 또는 가능한 만큼의 가격 데이터 사용
                    for item in price_data["output2"][:7]:
                        try:
                            close_price = float(item.get("stck_clpr", 0))
                            if close_price > 0:
                                prices.append(close_price)
                        except (ValueError, TypeError):
                            continue
                    
                    # 최소 2일 이상의 가격 데이터가 있어야 변동성 계산 가능
                    if len(prices) >= 2:
                        volatility = self._calculate_volatility(prices)
                        max_volatility = getattr(self.trading_config, "max_volatility", 0.1)  # 기본값 10%
                        
                        if volatility > max_volatility:
                            logger.log_system(
                                f"Risk check failed: High volatility for {symbol} - "
                                f"Volatility: {volatility:.2%}, Limit: {max_volatility:.2%}"
                            )
                            return False
                        logger.log_system(f"Volatility check passed for {symbol}: {volatility:.2%}")
                    else:
                        logger.log_system(f"Insufficient price data for volatility check: {symbol}, using only {len(prices)} days")
                else:
                    logger.log_system(f"Could not retrieve price data for volatility check: {symbol}")
            except Exception as e:
                logger.log_error(e, f"Error in volatility check for {symbol}")
                # 변동성 체크 실패 시 경고만 하고 계속 진행
                logger.log_system(f"Warning: Skipping volatility check for {symbol} due to error")
            
            # 거래량 체크
            try:
                # run_in_executor를 사용하여 비동기적으로 동기 함수 호출
                volume_data = await loop.run_in_executor(None, api_client.get_market_trading_volume)
                
                if volume_data.get("rt_cd") == "0" and "output2" in volume_data:
                    # 해당 종목 검색 (대소문자 구분 없이)
                    symbol_upper = symbol.upper()
                    symbol_volume = next((item for item in volume_data["output2"] 
                                       if item.get("mksc_shrn_iscd", "").upper() == symbol_upper), None)
                    
                    if symbol_volume:
                        volume = int(symbol_volume.get("prdy_vrss_vol", 0))
                        min_volume = getattr(self.trading_config, "min_daily_volume", 10000)  # 기본값 1만주
                        
                        if volume < min_volume:
                            logger.log_system(
                                f"Risk check failed: Low trading volume for {symbol} - "
                                f"Volume: {volume:,}주, Minimum: {min_volume:,}주"
                            )
                            return False
                        logger.log_system(f"Volume check passed for {symbol}: {volume:,}주")
                    else:
                        logger.log_system(f"Could not find volume data for {symbol} in market data")
                else:
                    logger.log_system(f"Could not retrieve market volume data for check: {symbol}")
            except Exception as e:
                logger.log_error(e, f"Error in volume check for {symbol}")
                # 거래량 체크 실패 시 경고만 하고 계속 진행
                logger.log_system(f"Warning: Skipping volume check for {symbol} due to error")
            
            # 모든 체크 통과
            logger.log_system(f"All risk checks passed for {symbol}")
            return True
            
        except Exception as e:
            logger.log_error(e, f"Risk check error for {symbol}")
            return False
            
    def _calculate_volatility(self, prices: List[float]) -> float:
        """변동성 계산"""
        if len(prices) < 2:
            return 0
            
        returns = []
        for i in range(1, len(prices)):
            returns.append((prices[i] - prices[i-1]) / prices[i-1])
            
        return sum(abs(r) for r in returns) / len(returns)
    
    async def check_positions(self):
        """포지션 체크 - 손절/익절"""
        try:
            # 동시성 문제 방지를 위한 락 사용
            async with self._async_lock:
                for symbol, position in self.positions.items():
                    # 현재가 조회
                    price_data = api_client.get_current_price(symbol)
                    current_price = float(price_data["output"]["stck_prpr"])
                    
                    # 수익률 계산
                    pnl_rate = (current_price - position["avg_price"]) / position["avg_price"]
                    
                    # 손절/익절 체크
                    if pnl_rate <= -self.trading_config.max_loss_rate:
                        # 손절 (거래 중지 상태에서도 동작하도록 bypass_pause=True 설정)
                        result = await self.place_order(
                            symbol=symbol,
                            side="SELL",
                            quantity=position["quantity"],
                            order_type="MARKET",
                            reason="stop_loss",
                            bypass_pause=True  # 거래 중지 상태에서도 손절 실행
                        )
                        
                        # 주문 결과 확인
                        if result["status"] != "success":
                            logger.log_system(f"Stop loss failed for {symbol}: {result.get('reason', 'Unknown error')}")
                        
                    elif pnl_rate >= self.trading_config.max_profit_rate:
                        # 익절 (거래 중지 상태에서도 동작하도록 bypass_pause=True 설정)
                        result = await self.place_order(
                            symbol=symbol,
                            side="SELL",
                            quantity=position["quantity"],
                            order_type="MARKET",
                            reason="take_profit",
                            bypass_pause=True  # 거래 중지 상태에서도 익절 실행
                        )
                        
                        # 주문 결과 확인
                        if result["status"] != "success":
                            logger.log_system(f"Take profit failed for {symbol}: {result.get('reason', 'Unknown error')}")
                    
                    # 미실현 손익 업데이트
                    unrealized_pnl = position["quantity"] * (current_price - position["avg_price"])
                    database_manager.save_position({
                        **position,
                        "current_price": current_price,
                        "unrealized_pnl": unrealized_pnl
                    })
                
        except Exception as e:
            logger.log_error(e, "Position check error")
    
    async def get_daily_summary(self) -> Dict[str, Any]:
        """일일 거래 요약"""
        try:
            return {
                "daily_pnl": self.daily_pnl,
                "daily_trades": self.daily_trades
            }
        except Exception as e:
            logger.log_error(e, "Failed to get daily summary")
            return {"daily_pnl": 0, "daily_trades": 0}
    
    async def get_today_orders(self) -> List[Dict[str, Any]]:
        """오늘 생성된 주문 목록 조회"""
        try:
            # 오늘 날짜 기준 시작 시간과 종료 시간
            today = datetime.now().date()
            start_date = f"{today.strftime('%Y-%m-%d')} 00:00:00"
            end_date = f"{today.strftime('%Y-%m-%d')} 23:59:59"
            
            # DB에서 오늘 생성된 주문 조회
            with database_manager.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM orders 
                    WHERE created_at BETWEEN ? AND ?
                    ORDER BY created_at DESC
                """, (start_date, end_date))
                
                today_orders = [dict(row) for row in cursor.fetchall()]
            
            return today_orders
        except Exception as e:
            logger.log_error(e, "Failed to get today's orders")
            return []
    
    async def get_positions(self) -> List[Dict[str, Any]]:
        """보유 포지션 조회"""
        try:
            return list(self.positions.values())
        except Exception as e:
            logger.log_error(e, "Failed to get positions")
            return []
            
    async def get_account_balance(self) -> Dict[str, Any]:
        """계좌 잔고 조회"""
        try:
            # 비동기 환경에서 동기 함수 호출을 위해 run_in_executor 사용
            loop = asyncio.get_event_loop()
            raw_result = await loop.run_in_executor(None, api_client.get_account_balance)
            
            # 데이터 표준화: raw_result가 리스트인 경우
            if isinstance(raw_result, list):
                logger.log_system("계좌 잔고 데이터가 리스트 형식으로 반환되었습니다.")
                if not raw_result:
                    logger.log_system("계좌 잔고 리스트가 비어 있습니다.", level="WARNING")
                    return {"output1": {
                        "tot_evlu_amt": "0",
                        "dnca_tot_amt": "0",
                        "scts_evlu_amt": "0",
                        "nass_amt": "0"
                    }}
                
                # 첫 번째 항목을 사용하여 딕셔너리 생성
                first_item = raw_result[0]
                if isinstance(first_item, dict):
                    # 첫 번째 항목을 output1 형식으로 표준화
                    return {"output1": {
                        "tot_evlu_amt": first_item.get("tot_evlu_amt", "0"),
                        "dnca_tot_amt": first_item.get("dnca_tot_amt", "0"),
                        "scts_evlu_amt": first_item.get("scts_evlu_amt", "0"),
                        "nass_amt": first_item.get("nass_amt", "0")
                    }}
                else:
                    logger.log_system(f"계좌 잔고 항목이 딕셔너리가 아닙니다: {type(first_item)}", level="WARNING")
                    return {"output1": {
                        "tot_evlu_amt": "0",
                        "dnca_tot_amt": "0",
                        "scts_evlu_amt": "0",
                        "nass_amt": "0"
                    }}
            
            # 데이터 표준화: raw_result가 딕셔너리인 경우
            elif isinstance(raw_result, dict):
                logger.log_system("계좌 잔고 데이터가 딕셔너리 형식으로 반환되었습니다.")
                
                # 이미 output1 키가 있는 경우
                if "output1" in raw_result:
                    return raw_result
                
                # 필요한 키들이 최상위에 있는 경우 (output1 구조로 래핑)
                if any(key in raw_result for key in ["tot_evlu_amt", "dnca_tot_amt", "scts_evlu_amt", "nass_amt"]):
                    return {"output1": {
                        "tot_evlu_amt": raw_result.get("tot_evlu_amt", "0"),
                        "dnca_tot_amt": raw_result.get("dnca_tot_amt", "0"),
                        "scts_evlu_amt": raw_result.get("scts_evlu_amt", "0"),
                        "nass_amt": raw_result.get("nass_amt", "0")
                    }}
                
                # rt_cd가 있고 0이 아닌 경우 (API 오류)
                if raw_result.get("rt_cd") and raw_result.get("rt_cd") != "0":
                    logger.log_system(f"계좌 잔고 조회 실패: {raw_result.get('msg1', '알 수 없는 오류')}", level="WARNING")
                    
                # 기타 경우는 빈 구조 반환
                return {"output1": {
                    "tot_evlu_amt": "0",
                    "dnca_tot_amt": "0",
                    "scts_evlu_amt": "0",
                    "nass_amt": "0"
                }}
            
            # 데이터 표준화: 다른 타입인 경우
            else:
                logger.log_system(f"계좌 잔고 데이터가 예상하지 못한 형식입니다: {type(raw_result)}", level="WARNING")
                return {"output1": {
                    "tot_evlu_amt": "0",
                    "dnca_tot_amt": "0",
                    "scts_evlu_amt": "0",
                    "nass_amt": "0"
                }}
                
        except Exception as e:
            logger.log_error(e, "Failed to get account balance")
            return {"output1": {
                "tot_evlu_amt": "0",
                "dnca_tot_amt": "0",
                "scts_evlu_amt": "0",
                "nass_amt": "0"
            }}

# 싱글톤 인스턴스
order_manager = OrderManager()
