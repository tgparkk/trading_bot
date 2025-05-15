"""
주문 관리자
"""
import asyncio
import threading
import time
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
            self.order_blacklist = {}  # 블랙리스트 추가: {종목코드: 만료시간}
            self.order_failures = {}   # 연속 실패 횟수 트래킹: {종목코드: 실패횟수}
            self.max_consecutive_failures = 3  # 최대 연속 실패 허용 횟수
            self.blacklist_duration = 30 * 60  # 블랙리스트 유지 시간 (초단위, 기본 30분)
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
            logger.log_system(f"[주문시도] {symbol} {side} 주문 시작 - 수량: {quantity}주, 가격: {price}, 타입: {order_type}")
            
            # 블랙리스트 체크 - 특정 종목이 블랙리스트에 있는지 확인
            current_time = time.time()
            if symbol in self.order_blacklist:
                expire_time = self.order_blacklist[symbol]
                if current_time < expire_time:
                    remaining_time = int((expire_time - current_time) / 60)
                    logger.log_system(f"[주문거부] {symbol}: 이 종목은 주문 실패 횟수 초과로 {remaining_time}분간 거래가 중지되었습니다.")
                    return {"status": "rejected", "reason": "symbol_blacklisted", "remaining_time": remaining_time}
                else:
                    # 블랙리스트 만료되면 제거
                    del self.order_blacklist[symbol]
                    if symbol in self.order_failures:
                        del self.order_failures[symbol]
                    logger.log_system(f"[블랙리스트 해제] {symbol}: 거래 재개 가능")
            
            # 거래 일시 중지 상태 확인 (bypass_pause가 False이고 거래가 일시 중지된 경우)
            if not bypass_pause and self.trading_paused:
                # 전략에 의한 자동 거래 (reason에 'user_request'가 없는 경우)인 경우만 거부
                if not reason or 'user_request' not in reason:
                    logger.log_system(f"[주문거부] {symbol}: 거래가 일시 중지 상태입니다.")
                    return {"status": "rejected", "reason": "trading_paused"}
            
            # 주문 단위 초기화 - 추후 KIS API 반환값에 따라 조정
            min_order_unit = 1
            order_unit = 1
            
            # API를 통해 주문 단위/최소 주문 수량 정보 가져오기 시도
            try:
                stock_info = api_client.get_stock_info(symbol)
                # 종목별 주문 단위 정보가 있으면 적용
                if stock_info.get("rt_cd") == "0" and "output" in stock_info:
                    # 필드명 예시: 'mktd_ord_unpr_unit', 'ord_unit_qty', 'unit_trade_qty' 등
                    for field in ["mktd_ord_unpr_unit", "ord_unit_qty", "unit_trade_qty"]:
                        if field in stock_info["output"] and stock_info["output"][field]:
                            try:
                                order_unit = int(stock_info["output"][field])
                                if order_unit > 0:
                                    break
                            except (ValueError, TypeError):
                                pass
            except Exception as e:
                logger.log_warning(f"{symbol} 주문 단위 정보 조회 중 오류: {str(e)}")
            
            # 주문 단위로 수량 조정
            if order_unit > 1 and quantity % order_unit != 0:
                adjusted_quantity = (quantity // order_unit) * order_unit
                logger.log_system(f"[주문수량조정] {symbol}: 주문 단위({order_unit}주) 조정 - {quantity}주 → {adjusted_quantity}주")
                quantity = adjusted_quantity
            
            # 수량이 0 이하인 경우
            if quantity <= 0:
                logger.log_system(f"[주문거부] {symbol}: 주문 수량이 0 이하입니다.")
                return {"status": "rejected", "reason": "invalid_quantity"}
            
            # 계좌 확인 및 매수 가능 확인 로직
            if side == "BUY":
                try:
                    # 계좌 잔고 조회
                    balance_data = await self.get_account_balance()
                    available_cash = 0
                    
                    # balance_data가 리스트인 경우 처리
                    if isinstance(balance_data, list):
                        if balance_data and isinstance(balance_data[0], dict):
                            available_cash = float(balance_data[0].get("dnca_tot_amt", "0"))
                            logger.log_system("[계좌조회] 계좌 잔고 데이터 형식 확인 필요 (리스트)")
                        else:
                            logger.log_system("[계좌조회] 계좌 잔고 데이터 형식 확인 필요 (리스트)")
                    # 딕셔너리인 경우 처리
                    elif isinstance(balance_data, dict):
                        if "output1" in balance_data:
                            available_cash = float(balance_data["output1"].get("dnca_tot_amt", "0"))
                        else:
                            # 최상위 레벨에 필드가 있는 경우
                            available_cash = float(balance_data.get("dnca_tot_amt", "0"))
                    
                    # 안전 마진 적용 (예수금의 90%만 사용)
                    available_cash = available_cash * 0.9
                    
                    # 주문 금액 계산
                    order_amount = price * quantity
                    
                    # 매수 주문 가능 최대 수량 계산
                    max_quantity = int(available_cash / price) if price > 0 else 0
                    
                    # 주문 단위 적용 (최대 수량도 주문 단위로 조정)
                    if order_unit > 1 and max_quantity > 0:
                        max_quantity = (max_quantity // order_unit) * order_unit
                        if max_quantity < min_order_unit:
                            max_quantity = 0
                    
                    logger.log_system(f"[주문수량검증] {symbol}: 주문수량={quantity}주, 최대가능수량={max_quantity}주, 주문단위={order_unit}주")
                    
                    if max_quantity <= 0:
                        logger.log_system(f"[주문거부] {symbol}: 주문 가능 수량이 없습니다 (가용 잔고: {available_cash:,.0f}원)")
                        return {"status": "rejected", "reason": "insufficient_balance"}
                    
                    if quantity > max_quantity:
                        # 가능한 최대 수량으로 조정
                        logger.log_system(f"[주문수량조정] {symbol}: 계좌 잔고 부족으로 수량 조정 - {quantity}주 → {max_quantity}주")
                        quantity = max_quantity
                        
                    logger.log_system(f"[주문금액] {symbol}: {quantity}주 x {price:,}원 = {quantity * price:,.0f}원 (가용 잔고: {available_cash:,.0f}원)")
                except Exception as e:
                    logger.log_error(e, "계좌 잔고 조회 실패")
                    logger.log_system(f"[계좌조회오류] 계좌 잔고 조회 중 오류 발생: {str(e)}")
                    return {"status": "rejected", "reason": "balance_check_failed"}
            elif side == "SELL":
                # 매도 주문일 경우 보유 수량 체크
                try:
                    # 현재 보유 포지션 확인
                    current_position = self.positions.get(symbol, {"quantity": 0})
                    available_quantity = current_position.get("quantity", 0)
                    
                    if available_quantity <= 0:
                        logger.log_system(f"[주문거부] {symbol}: 보유 수량이 없습니다 (요청: {quantity}주, 보유: {available_quantity}주)")
                        return {"status": "rejected", "reason": "insufficient_position"}
                    
                    if quantity > available_quantity:
                        # 보유 수량으로 조정
                        logger.log_system(f"[주문수량조정] {symbol}: 보유 수량 초과로 수량 조정 - {quantity}주 → {available_quantity}주")
                        quantity = available_quantity
                    
                    # 주문 단위 적용
                    if order_unit > 1 and quantity % order_unit != 0:
                        adjusted_quantity = (quantity // order_unit) * order_unit
                        if adjusted_quantity <= 0:
                            adjusted_quantity = 0
                        
                        if adjusted_quantity <= 0:
                            logger.log_system(f"[주문거부] {symbol}: 주문 단위 조정 후 수량이 0 이하입니다.")
                            return {"status": "rejected", "reason": "insufficient_quantity_after_adjustment"}
                        
                        logger.log_system(f"[주문수량조정] {symbol}: 주문 단위({order_unit}주) 조정 - {quantity}주 → {adjusted_quantity}주")
                        quantity = adjusted_quantity
                    
                    logger.log_system(f"[매도수량] {symbol}: 매도 수량 {quantity}주 (보유: {available_quantity}주)")
                except Exception as e:
                    logger.log_error(e, f"보유 수량 확인 중 오류: {symbol}")
                    logger.log_system(f"[보유량확인오류] {symbol} 보유량 확인 중 오류 발생: {str(e)}")
                    return {"status": "rejected", "reason": "position_check_failed"}
            
            # 수량이 0 이하인 경우 거부
            if quantity <= 0:
                logger.log_system(f"[주문거부] {symbol}: 주문 수량이 0 이하입니다.")
                return {"status": "rejected", "reason": "invalid_quantity"}
            
            # 리스크 체크
            risk_check_result = await self._check_risk(symbol, side, quantity, price)
            if not risk_check_result:
                logger.log_system(f"[주문거부] {symbol}: 리스크 체크 실패")
                return {"status": "rejected", "reason": "risk_check_failed"}
            
            # 주문 실행
            try:
                if order_type.upper() == "MARKET":
                    logger.log_system(f"[주문실행] {symbol} 시장가 주문 실행 - 수량: {quantity}주")
                    order_result = api_client.place_order(
                        symbol=symbol,
                        order_type="MARKET",
                        side=side,
                        quantity=quantity
                    )
                else:
                    # 지정가 주문에서도 가격이 없으면 현재가 조회
                    if price is None:
                        price_data = api_client.get_current_price(symbol)
                        price = float(price_data["output"]["stck_prpr"])
                    
                    logger.log_system(f"[주문실행] {symbol} 지정가 주문 실행 - 가격: {price:,}원, 수량: {quantity}주")
                    order_result = api_client.place_order(
                        symbol=symbol,
                        order_type="LIMIT",
                        side=side,
                        quantity=quantity,
                        price=int(price)
                    )
                
                # 주문 결과 확인
                if order_result.get("rt_cd") == "0":
                    # 주문 성공 - 실패 카운터 초기화
                    if symbol in self.order_failures:
                        del self.order_failures[symbol]
                    
                    order_id = order_result["output"]["ODNO"]
                    logger.log_system(f"[주문성공] {symbol} 주문 성공 - 주문ID: {order_id}")
                    
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
                    # 주문 실패 - 실패 카운터 증가
                    self.order_failures[symbol] = self.order_failures.get(symbol, 0) + 1
                    
                    # 블랙리스트 조건 체크
                    if self.order_failures[symbol] >= self.max_consecutive_failures:
                        # 최대 실패 횟수 초과 - 블랙리스트에 추가
                        self.order_blacklist[symbol] = time.time() + self.blacklist_duration
                        logger.log_system(f"[블랙리스트 등록] {symbol}: 연속 {self.order_failures[symbol]}회 주문 실패로 {self.blacklist_duration//60}분간 주문 중지")
                        
                        # 알림 전송 (블랙리스트 추가 알림)
                        try:
                            await alert_system.notify_system_status(
                                "WARNING",
                                f"{symbol} 연속 {self.order_failures[symbol]}회 주문 실패로 블랙리스트 등록 ({self.blacklist_duration//60}분간 주문 중지)"
                            )
                        except Exception as alert_error:
                            logger.log_error(alert_error, f"{symbol} 블랙리스트 알림 전송 실패")
                    
                    error_msg = order_result.get("msg1", "Unknown error")
                    failure_count = self.order_failures.get(symbol, 0)
                    logger.log_system(f"[주문실패] {symbol} 주문 실패 ({failure_count}/{self.max_consecutive_failures}회) - 오류: {error_msg}")
                    logger.log_error(
                        Exception(error_msg),
                        f"Order failed for {symbol}"
                    )
                    return {"status": "failed", "reason": error_msg, "failure_count": failure_count}
            except Exception as api_e:
                # API 호출 예외도 실패 카운터 증가
                self.order_failures[symbol] = self.order_failures.get(symbol, 0) + 1
                failure_count = self.order_failures.get(symbol, 0)
                
                logger.log_system(f"[API호출오류] {symbol} API 호출 중 예외 발생 ({failure_count}/{self.max_consecutive_failures}회): {str(api_e)}")
                logger.log_error(api_e, f"API call error for {symbol}")
                
                # 블랙리스트 조건 체크
                if self.order_failures[symbol] >= self.max_consecutive_failures:
                    self.order_blacklist[symbol] = time.time() + self.blacklist_duration
                    logger.log_system(f"[블랙리스트 등록] {symbol}: 연속 {self.order_failures[symbol]}회 API 오류로 {self.blacklist_duration//60}분간 주문 중지")
                
                return {"status": "error", "reason": f"API call error: {str(api_e)}", "failure_count": failure_count}
            
        except Exception as e:
            logger.log_system(f"[주문오류] {symbol} 주문 처리 중 예외 발생: {str(e)}")
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
            logger.log_system(f"[리스크체크] {symbol} 리스크 체크 시작")
            
            # 루프 및 실행기 가져오기
            loop = asyncio.get_event_loop()
            
            # 현재가 조회 (price가 None인 경우에만)
            if price is None:
                try:
                    logger.log_system(f"[가격조회] {symbol} 현재가 조회")
                    # run_in_executor를 사용하여 비동기적으로 동기 함수 호출
                    price_data = await loop.run_in_executor(None, 
                                                           lambda: api_client.get_current_price(symbol))
                    if price_data.get("rt_cd") == "0" and "output" in price_data:
                        price = float(price_data["output"]["stck_prpr"])
                        logger.log_system(f"[가격조회] {symbol} 현재가: {price:,}원")
                    else:
                        error_msg = price_data.get("msg1", "Unknown error")
                        logger.log_system(f"[가격조회실패] {symbol} 현재가 조회 실패: {error_msg}")
                        logger.log_error(
                            Exception(f"Failed to get current price: {error_msg}"),
                            f"Risk check error for {symbol}"
                        )
                        return False
                except Exception as e:
                    logger.log_system(f"[가격조회오류] {symbol} 현재가 조회 중 오류: {str(e)}")
                    logger.log_error(e, f"Error getting current price for {symbol}")
                    return False
            
            # 포지션 사이즈 체크
            position_value = quantity * price
            max_position_size = self.trading_config.risk_params.get("max_position_size", 10_000_000)
            if position_value > max_position_size:
                logger.log_system(
                    f"[포지션크기초과] {symbol} - 포지션 크기 제한 초과: "
                    f"계산값={position_value:,.0f}원, 제한={max_position_size:,.0f}원"
                )
                return False
            
            # 일일 손실 한도 체크
            max_loss_rate = self.trading_config.risk_params.get("max_loss_rate", 0.02)
            daily_loss_limit = -max_loss_rate * 100000
            if self.daily_pnl < daily_loss_limit:
                logger.log_system(
                    f"[일일손실한도] 일일 손실 한도 도달: "
                    f"현재손실={self.daily_pnl:,.0f}원, 한도={daily_loss_limit:,.0f}원"
                )
                return False
            
            # 종목별 최대 포지션 수량 체크
            current_position = self.positions.get(symbol, {"quantity": 0})
            max_position_per_symbol = self.trading_config.risk_params.get("max_position_per_symbol", 1000)
            if side == "BUY" and current_position["quantity"] + quantity > max_position_per_symbol:
                logger.log_system(
                    f"[포지션수량초과] {symbol} - 종목별 최대 포지션 수량 초과: "
                    f"현재={current_position['quantity']}주, 추가={quantity}주, 제한={max_position_per_symbol}주"
                )
                return False
            
            # 변동성 체크
            try:
                logger.log_system(f"[변동성체크] {symbol} 변동성 체크")
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
                        max_volatility = self.trading_config.risk_params.get("max_volatility", 0.1)  # 기본값 10%
                        
                        if volatility > max_volatility:
                            logger.log_system(
                                f"[변동성초과] {symbol} - 변동성 제한 초과: "
                                f"현재={volatility:.2%}, 제한={max_volatility:.2%}"
                            )
                            return False
                        logger.log_system(f"[변동성체크] {symbol} 변동성: {volatility:.2%}")
                    else:
                        logger.log_system(f"[변동성체크] {symbol} 변동성 계산을 위한 충분한 데이터가 없습니다. (데이터 수: {len(prices)})")
                else:
                    logger.log_system(f"[변동성체크실패] {symbol} 가격 이력 조회 실패")
            except Exception as e:
                logger.log_system(f"[변동성체크오류] {symbol} 변동성 체크 중 오류: {str(e)}")
                logger.log_error(e, f"Error in volatility check for {symbol}")
                # 변동성 체크 실패 시 경고만 하고 계속 진행
                logger.log_system(f"Warning: Skipping volatility check for {symbol} due to error")
            
            # 거래량 체크
            try:
                logger.log_system(f"[거래량체크] {symbol} 거래량 체크")
                
                # get_market_trading_volume API 대신 get_symbol_info API 사용
                symbol_info_task = asyncio.create_task(api_client.get_symbol_info(symbol))
                
                try:
                    symbol_info = await asyncio.wait_for(symbol_info_task, timeout=3.0)
                    
                    if symbol_info and "volume" in symbol_info:
                        volume = int(symbol_info.get("volume", 0))
                        min_volume = self.trading_config.risk_params.get("min_daily_volume", 10000)  # 기본값 1만주
                        
                        if volume < min_volume:
                            logger.log_system(
                                f"[거래량부족] {symbol} - 거래량 부족: "
                                f"현재={volume:,}주, 최소={min_volume:,}주"
                            )
                            return False
                        logger.log_system(f"[거래량체크] {symbol} 거래량: {volume:,}주")
                    else:
                        logger.log_system(f"[거래량체크] {symbol} 거래량 정보 없음, 체크 건너뜀")
                except asyncio.TimeoutError:
                    logger.log_system(f"[거래량체크] {symbol} 종목 정보 API 타임아웃, 체크 건너뜀")
                except Exception as e:
                    logger.log_system(f"[거래량체크] {symbol} 종목 정보 API 오류, 체크 건너뜀: {str(e)}")
            except Exception as e:
                logger.log_system(f"[거래량체크오류] {symbol} 거래량 체크 중 오류: {str(e)}")
                logger.log_error(e, f"Error in volume check for {symbol}")
                # 거래량 체크 실패 시 경고만 하고 계속 진행
                logger.log_system(f"Warning: Skipping volume check for {symbol} due to error")
            
            # 모든 체크 통과
            logger.log_system(f"[리스크체크] {symbol} 모든 리스크 체크 통과")
            return True
        except Exception as e:
            logger.log_system(f"[리스크체크오류] {symbol} 리스크 체크 중 예외 발생: {str(e)}")
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
                    try:
                        # 현재가 조회
                        price_data = api_client.get_current_price(symbol)
                        
                        # 현재가 조회 실패 시 건너뛰기
                        if price_data.get("rt_cd") != "0" or "output" not in price_data:
                            logger.log_system(f"[포지션체크] {symbol} 현재가 조회 실패, 건너뜀")
                            continue
                            
                        current_price = float(price_data["output"]["stck_prpr"])
                        
                        # 수익률 계산
                        pnl_rate = (current_price - position["avg_price"]) / position["avg_price"] if position["avg_price"] > 0 else 0
                        
                        # 손절/익절 체크
                        max_loss_rate = self.trading_config.risk_params.get("max_loss_rate", 0.02)
                        max_profit_rate = self.trading_config.scalping_params.get("take_profit", 0.015)
                        
                        # 보유 수량이 0인 경우 처리 건너뜀
                        if position["quantity"] <= 0:
                            logger.log_system(f"[포지션체크] {symbol} 보유 수량이 0 이하, 건너뜀")
                            continue
                        
                        if pnl_rate <= -max_loss_rate:
                            # 손절 (거래 중지 상태에서도 동작하도록 bypass_pause=True 설정)
                            logger.log_system(f"[손절시도] {symbol} 손절 주문 시도 - 수익률: {pnl_rate:.2%}, 한도: -{max_loss_rate:.2%}")
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
                                logger.log_system(f"[손절실패] {symbol}: {result.get('reason', 'Unknown error')}")
                            
                        elif pnl_rate >= max_profit_rate:
                            # 익절 (거래 중지 상태에서도 동작하도록 bypass_pause=True 설정)
                            logger.log_system(f"[익절시도] {symbol} 익절 주문 시도 - 수익률: {pnl_rate:.2%}, 한도: {max_profit_rate:.2%}")
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
                                logger.log_system(f"[익절실패] {symbol}: {result.get('reason', 'Unknown error')}")
                        
                        # 미실현 손익 업데이트
                        unrealized_pnl = position["quantity"] * (current_price - position["avg_price"])
                        database_manager.save_position({
                            **position,
                            "current_price": current_price,
                            "unrealized_pnl": unrealized_pnl
                        })
                    except Exception as position_e:
                        logger.log_error(position_e, f"포지션 체크 중 개별 종목 오류: {symbol}")
                        # 한 종목 오류로 전체 프로세스가 중단되지 않도록 계속 진행
                        continue
                
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
        """계좌 잔고 조회
        
        Returns:
            Dict[str, Any]: 계좌 잔고 정보를 담은 딕셔너리
                - cash_balance: 주문 가능한 현금 잔고
                - total_balance: 총 평가금액
                - positions: 보유 종목 목록
        """
        try:
            # 비동기 환경에서 동기 함수 호출을 위해 run_in_executor 사용
            loop = asyncio.get_event_loop()
            raw_result = await loop.run_in_executor(None, api_client.get_account_balance)
            
            # 기본 반환 구조 초기화
            result = {
                "cash_balance": 0.0,  # 현금 잔고
                "total_balance": 0.0,  # 총 평가금액
                "positions": []  # 보유 종목 목록
            }
            
            # API 응답이 유효한지 확인
            if raw_result and raw_result.get("rt_cd") == "0":
                logger.log_system("계좌 잔고 조회 성공")
                
                # output1(보유 종목), output2(계좌 요약) 구조 확인
                
                # 1. 보유 종목 처리 (output1)
                if "output1" in raw_result:
                    # 종목 목록 형식 확인
                    if isinstance(raw_result["output1"], list):
                        result["positions"] = raw_result["output1"]
                    elif isinstance(raw_result["output1"], dict):
                        # 딕셔너리인 경우 리스트로 변환
                        result["positions"] = [raw_result["output1"]]
                
                # 2. 계좌 요약 정보 처리 (output2 - 예수금 총액 등)
                if "output2" in raw_result and raw_result["output2"]:
                    # output2가 리스트인 경우
                    if isinstance(raw_result["output2"], list) and raw_result["output2"]:
                        account_summary = raw_result["output2"][0]
                        if isinstance(account_summary, dict):
                            # 예수금 총액
                            result["cash_balance"] = float(account_summary.get("dnca_tot_amt", "0"))
                            # 총 평가금액
                            result["total_balance"] = float(account_summary.get("tot_evlu_amt", "0"))
                    # output2가 딕셔너리인 경우
                    elif isinstance(raw_result["output2"], dict):
                        # 예수금 총액
                        result["cash_balance"] = float(raw_result["output2"].get("dnca_tot_amt", "0"))
                        # 총 평가금액
                        result["total_balance"] = float(raw_result["output2"].get("tot_evlu_amt", "0"))
                
                # 3. output1에서 계좌 정보 추출 (레거시 대응)
                if result["cash_balance"] == 0 and "output1" in raw_result and raw_result["output1"]:
                    logger.log_system("output2가 없거나 예수금이 0입니다. output1에서 정보 추출 시도")
                    
                    if isinstance(raw_result["output1"], list) and raw_result["output1"]:
                        account_data = raw_result["output1"][0]
                        if isinstance(account_data, dict):
                            # 예수금 총액 검색
                            if "dnca_tot_amt" in account_data:
                                result["cash_balance"] = float(account_data.get("dnca_tot_amt", "0"))
                                logger.log_system(f"output1에서 예수금 추출: {result['cash_balance']:,.0f}원")
            else:
                # API 오류 처리
                error_msg = raw_result.get("msg1", "알 수 없는 오류")
                logger.log_system(f"계좌 잔고 조회 실패: {error_msg}", level="WARNING")
            
            # 결과 정보 로깅
            logger.log_system(f"계좌 잔고 정보: 예수금={result['cash_balance']:,.0f}원, 총평가={result['total_balance']:,.0f}원, 보유종목={len(result['positions'])}개")
            
            return result
                
        except Exception as e:
            logger.log_error(e, "Failed to get account balance")
            return {
                "cash_balance": 0.0,
                "total_balance": 0.0,
                "positions": []
            }

# 싱글톤 인스턴스
order_manager = OrderManager()
