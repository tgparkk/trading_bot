"""
주문 관리자
"""
import asyncio
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from config.settings import config
from core.api_client import api_client
from utils.logger import logger
from utils.database import db
from monitoring.alert_system import alert_system

class OrderManager:
    """주문 관리자"""
    
    def __init__(self):
        self.trading_config = config["trading"]
        self.positions = {}  # {symbol: position_data}
        self.pending_orders = {}  # {order_id: order_data}
        self.daily_pnl = 0
        self.daily_trades = 0
        
    async def initialize(self):
        """초기화 - 포지션/잔고 로드"""
        try:
            # 계좌 잔고 조회
            balance_data = api_client.get_account_balance()
            
            # DB에서 포지션 로드
            db_positions = db.get_all_positions()
            
            # 포지션 동기화
            for position in db_positions:
                self.positions[position["symbol"]] = position
            
            logger.log_system("Order manager initialized successfully")
            
        except Exception as e:
            logger.log_error(e, "Failed to initialize order manager")
            raise
    
    async def place_order(self, symbol: str, side: str, quantity: int, 
                         price: float = None, order_type: str = "MARKET",
                         strategy: str = None, reason: str = None) -> Dict[str, Any]:
        """주문 실행"""
        try:
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
                
                db.save_order(order_data)
                self.pending_orders[order_id] = order_data
                
                # 알림 전송
                await alert_system.notify_trade(order_data)
                
                self.daily_trades += 1
                
                return {"status": "success", "order_id": order_id}
            
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
                db.update_order(order_id, {"status": "CANCELLED"})
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
            db.save_position(current_position)
            
            # 메모리 업데이트
            if current_position["quantity"] > 0:
                self.positions[symbol] = current_position
            else:
                if symbol in self.positions:
                    del self.positions[symbol]
            
            logger.log_system(f"Position updated for {symbol}")
            
        except Exception as e:
            logger.log_error(e, f"Position update error: {symbol}")
    
    async def _check_risk(self, symbol: str, side: str, quantity: int, 
                         price: float = None) -> bool:
        """리스크 체크"""
        try:
            # 현재가 조회
            if price is None:
                price_data = api_client.get_current_price(symbol)
                price = float(price_data["output"]["stck_prpr"])
            
            # 포지션 사이즈 체크
            position_value = quantity * price
            if position_value > self.trading_config.max_position_size:
                logger.log_system(
                    f"Risk check failed: Position size too large for {symbol}"
                )
                return False
            
            # 일일 손실 한도 체크
            if self.daily_pnl < -self.trading_config.max_loss_rate * 100000000:  # 1억 기준
                logger.log_system("Risk check failed: Daily loss limit reached")
                return False
            
            # 종목별 최대 포지션 수량 체크
            current_position = self.positions.get(symbol, {"quantity": 0})
            if side == "BUY" and current_position["quantity"] + quantity > self.trading_config.max_position_per_symbol:
                logger.log_system(f"Risk check failed: Max position per symbol reached for {symbol}")
                return False
            
            # 변동성 체크
            price_data = api_client.get_daily_price(symbol)
            if price_data.get("rt_cd") == "0":
                prices = [float(d["stck_clpr"]) for d in price_data["output2"]]
                volatility = self._calculate_volatility(prices)
                if volatility > self.trading_config.max_volatility:
                    logger.log_system(f"Risk check failed: High volatility for {symbol}")
                    return False
            
            # 거래량 체크
            volume_data = api_client.get_market_trading_volume()
            if volume_data.get("rt_cd") == "0":
                symbol_volume = next((item for item in volume_data["output2"] if item["mksc_shrn_iscd"] == symbol), None)
                if symbol_volume and int(symbol_volume.get("prdy_vrss_vol", 0)) < self.trading_config.min_daily_volume:
                    logger.log_system(f"Risk check failed: Low trading volume for {symbol}")
                    return False
            
            return True
            
        except Exception as e:
            logger.log_error(e, "Risk check error")
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
            for symbol, position in self.positions.items():
                # 현재가 조회
                price_data = api_client.get_current_price(symbol)
                current_price = float(price_data["output"]["stck_prpr"])
                
                # 수익률 계산
                pnl_rate = (current_price - position["avg_price"]) / position["avg_price"]
                
                # 손절/익절 체크
                if pnl_rate <= -self.trading_config.max_loss_rate:
                    # 손절
                    await self.place_order(
                        symbol=symbol,
                        side="SELL",
                        quantity=position["quantity"],
                        order_type="MARKET",
                        reason="stop_loss"
                    )
                    
                elif pnl_rate >= self.trading_config.max_profit_rate:
                    # 익절
                    await self.place_order(
                        symbol=symbol,
                        side="SELL",
                        quantity=position["quantity"],
                        order_type="MARKET",
                        reason="take_profit"
                    )
                
                # 미실현 손익 업데이트
                unrealized_pnl = position["quantity"] * (current_price - position["avg_price"])
                db.save_position({
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
            with db.get_connection() as conn:
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
