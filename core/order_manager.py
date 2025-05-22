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
from core.account_state import account_state
from core.risk_manager import risk_manager
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
            self.daily_sell_amount = 0  # 일일 매도 금액 합계 추가
            self.trading_paused = False  # 거래 일시 중지 플래그
            self._async_lock = asyncio.Lock()  # 비동기 작업을 위한 락
            self.order_blacklist = {}  # 블랙리스트 추가: {종목코드: 만료시간}
            self.order_failures = {}   # 연속 실패 횟수 트래킹: {종목코드: {"최근_실패_시간": time.time(), "횟수": 0, "실패_원인": []}}
            self.max_consecutive_failures = 5  # 최대 연속 실패 허용 횟수 증가
            self.blacklist_duration = 15 * 60  # 블랙리스트 유지 시간 감소 (15분)
            self.failure_reset_time = 3 * 60  # 실패 횟수 초기화 시간 감소 (3분)
            self.retry_delay = 1  # 재시도 대기 시간 (초)
            self.max_retries = 2  # 최대 재시도 횟수
            self.max_order_wait_time = 30  # 주문 체결 최대 대기 시간 (초)
            self.order_check_interval = 1  # 주문 상태 체크 간격 (초)
            self._initialized = True
    
    async def initialize(self):
        """초기화 - 포지션/잔고 로드"""
        try:
            # 계좌 상태 관리자 초기화 확인 - 초기화되지 않았다면 초기화 실행
            if not hasattr(account_state, 'initialized') or not account_state.initialized:
                logger.log_system("계좌 상태 관리자 초기화가 필요합니다.")
                await account_state.initialize()
                logger.log_system("계좌 상태 관리자 초기화 완료")
            
            # 리스크 관리자 초기화 확인
            if not hasattr(risk_manager, '_initialized') or not risk_manager._initialized:
                logger.log_system("리스크 관리자 초기화가 필요합니다.")
                await risk_manager.initialize()
                logger.log_system("리스크 관리자 초기화 완료")
            
            # 계좌 잔고 강제 동기화
            await account_state.sync_with_api(force=True)
            
            # 계좌 잔고 정보 로깅
            account_info = await account_state.get_account_info()
            logger.log_system(f"계좌 잔고 정보: 실제잔고={account_info['available_cash']:,.0f}원, "
                            f"내부가용잔고={account_info['internal_available_cash']:,.0f}원")
            
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
            
            # 일일 거래 데이터 초기화
            self.daily_pnl = 0
            self.daily_trades = 0
            self.daily_sell_amount = 0
            
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
    
    async def place_order(self, symbol: str, side: str, quantity: int = None, 
                          price: float = None, order_type: str = "MARKET",
                          strategy: str = None, reason: str = None, 
                          bypass_pause: bool = False,
                          signal_strength: float = 5.0) -> Dict[str, Any]:
        """주문 실행
        
        Args:
            symbol: 종목코드
            side: 매수/매도 구분 (BUY/SELL)
            quantity: 주문 수량 (매수의 경우 None으로 설정 시 리스크 관리자가 계산)
            price: 주문 가격 (지정가 주문 시 필수)
            order_type: 주문 유형 (MARKET/LIMIT)
            strategy: 전략 이름
            reason: 주문 사유
            bypass_pause: 거래 중지 상태 무시 (긴급 매도 등에 사용)
            signal_strength: 신호 강도 (0~10, 기본값 5)
            
        Returns:
            Dict[str, Any]: 주문 결과
        """
        # 거래 중지 상태 확인
        if self.is_trading_paused() and not bypass_pause:
            return {"status": "failed", "reason": "거래가 일시 중지되었습니다"}
        
        try:
            logger.log_system(f"[주문시도] {symbol} {side} 주문 시작 - 초기 요청 수량: {quantity}주, 가격: {price}, 타입: {order_type}")
            
            # 가격 정보 확인 - 주문 처리를 위해 price 값 필요
            if price is None or price <= 0:
                # 현재가 조회 필요
                try:
                    price_data = api_client.get_current_price(symbol)
                    if price_data and price_data.get("rt_cd") == "0" and "output" in price_data:
                        price = float(price_data["output"]["stck_prpr"])
                        logger.log_system(f"[주문정보] {symbol} - 현재가 조회 결과: {price:,.0f}원")
                    else:
                        logger.log_system(f"[주문실패] {symbol} - 현재가 조회 실패")
                        return {"status": "failed", "reason": "현재가 조회 실패"}
                except Exception as price_e:
                    logger.log_error(price_e, f"{symbol} 현재가 조회 중 오류")
                    return {"status": "failed", "reason": "현재가 조회 오류"}
            
            # BUY 주문이고 수량이 지정되지 않은 경우 리스크 관리자를 통해 수량 계산
            if side.upper() == "BUY" and (quantity is None or quantity <= 0):
                quantity, order_amount = await risk_manager.calculate_position_size(
                    symbol=symbol,
                    side=side,
                    current_price=price,
                    strategy=strategy,
                    signal_strength=signal_strength
                )
                
                # 계산된 수량이 0인 경우 (리스크 제한 등의 이유로)
                if quantity <= 0:
                    logger.log_system(f"[주문취소] {symbol} - 리스크 관리자가 0주 수량 계산, 주문 취소")
                    return {"status": "rejected", "reason": "리스크 관리 제한으로 주문 수량이 0입니다"}
                
                logger.log_system(f"[주문정보] {symbol} - 리스크 관리자 계산 수량: {quantity}주")
            
            # 매도 주문인 경우 사전 검증 - 보유 수량 확인
            if side.upper() == "SELL":
                # 보유 수량 확인 (데이터베이스와 API 둘 다 확인)
                current_position = self.positions.get(symbol, {"quantity": 0})
                
                # API를 통해 실제 보유 수량 다시 확인 (실시간 데이터 우선)
                try:
                    positions_data = await self.get_positions()
                    if positions_data and "output1" in positions_data:
                        for position in positions_data["output1"]:
                            if position.get("pdno") == symbol:
                                # API에서 조회한 실제 보유 수량으로 업데이트
                                current_position["quantity"] = int(position.get("hldg_qty", 0))
                                break
                except Exception as e:
                    logger.log_error(e, f"{symbol} 실제 보유 수량 조회 중 오류")
                
                if current_position["quantity"] < quantity:
                    error_msg = f"보유 수량 부족 (보유: {current_position['quantity']}주, 요청: {quantity}주)"
                    logger.log_system(f"[매도실패] {symbol} - {error_msg}")
                    
                    # 실패 횟수 증가 처리
                    self._increment_failure_count(symbol, "INSUFFICIENT_QUANTITY", error_msg)
                    return {"status": "failed", "reason": error_msg}
            
            # 블랙리스트 체크 - 특정 종목이 블랙리스트에 있는지 확인
            current_time = time.time()
            if symbol in self.order_blacklist:
                expire_time = self.order_blacklist[symbol]
                if current_time < expire_time:
                    remaining_time = int((expire_time - current_time) / 60)
                    error_msg = f"이 종목은 주문 실패 횟수 초과로 {remaining_time}분간 거래가 중지되었습니다."
                    logger.log_system(f"[주문거부] {symbol}: {error_msg}")
                    return {"status": "rejected", "reason": "symbol_blacklisted", "remaining_time": remaining_time}
                else:
                    # 블랙리스트 만료되면 제거
                    del self.order_blacklist[symbol]
                    if symbol in self.order_failures:
                        self.order_failures[symbol] = {"최근_실패_시간": 0, "횟수": 0, "실패_원인": []}
                    logger.log_system(f"[블랙리스트 해제] {symbol}: 거래 재개 가능")
            
            # 거래 일시 중지 상태 확인
            if not bypass_pause and self.trading_paused:
                if not reason or 'user_request' not in reason:
                    logger.log_system(f"[주문거부] {symbol}: 거래가 일시 중지 상태입니다.")
                    return {"status": "rejected", "reason": "trading_paused"}

            # 리스크 한도 검증 (매도는 예외 - 포지션 정리 목적)
            if side.upper() == "BUY":
                risk_check = await risk_manager.check_risk_limits(symbol, side, quantity, price)
                if not risk_check["allowed"]:
                    logger.log_system(f"[주문거부] {symbol}: {risk_check['reason']}")
                    return {"status": "rejected", "reason": risk_check["reason"]}
            
            # 주문 금액 계산 및 내부 잔고 검증
            if side.upper() == "BUY":
                # 시장가 주문은 계산 금액에 여유 필요 (시장가 10%, 지정가 3%로 여유 적절히 조정)
                calculated_price = price * 1.03 if order_type.upper() == "LIMIT" else price * 1.10
                order_amount = calculated_price * quantity
                
                # 로그 추가 - 보다 자세한 정보
                logger.log_system(f"[매수금액계산] {symbol} - 현재가: {price:,.0f}원, 수량: {quantity}주, "
                                f"계산가격(마진적용): {calculated_price:,.0f}원, 주문총액: {order_amount:,.0f}원")
                
                # 계좌 잔고 최신화 (주문 전 실시간 동기화)
                try:
                    # 계좌 정보 강제 동기화 - 오류 처리 강화
                    try:
                        sync_result = await account_state.sync_with_api(force=True)
                        if not sync_result:
                            logger.log_warning(f"[매수주의] {symbol} - 계좌 동기화 실패, 기존 정보로 진행")
                    except RuntimeError as loop_error:
                        # 이벤트 루프 관련 오류 처리
                        error_msg = str(loop_error)
                        if "different loop" in error_msg:
                            logger.log_warning(f"[이벤트루프오류] {symbol} - 이벤트 루프 불일치 오류 발생, 강제 진행")
                        else:
                            logger.log_error(loop_error, f"{symbol} 계좌 동기화 중 이벤트 루프 오류")
                    except Exception as sync_error:
                        logger.log_error(sync_error, f"{symbol} 계좌 동기화 중 예외 발생")
                    
                    account_info = await account_state.get_account_info()
                    
                    # 주문 가능 금액 확인
                    available_cash = account_info["available_cash"]  # 예수금
                    internal_available_cash = account_info["internal_available_cash"]  # 내부 가용 잔고
                    
                    # 실제 주문가능금액 조회 (API)
                    api_balance = api_client.get_account_balance()
                    api_ord_psbl_cash = 0  # API 주문가능금액
                    
                    if api_balance.get("rt_cd") == "0":
                        # 주문가능금액 추출
                        if "output2" in api_balance and api_balance["output2"]:
                            output2 = api_balance["output2"]
                            if isinstance(output2, list) and output2:
                                item = output2[0]
                                if "ord_psbl_cash" in item:
                                    api_ord_psbl_cash = float(item.get("ord_psbl_cash", "0"))
                            elif isinstance(output2, dict) and "ord_psbl_cash" in output2:
                                api_ord_psbl_cash = float(output2.get("ord_psbl_cash", "0"))
                    
                    # API 주문가능금액 검증 (가장 우선)
                    if api_ord_psbl_cash > 0:
                        if order_amount > api_ord_psbl_cash:
                            logger.log_system(f"[매수실패] {symbol} - 주문금액({order_amount:,.0f}원)이 API 주문가능금액({api_ord_psbl_cash:,.0f}원)을 초과합니다.")
                            self._increment_failure_count(symbol, "INSUFFICIENT_BALANCE", "주문가능금액 초과 (API 주문가능금액 부족)")
                            return {"status": "failed", "reason": "주문가능금액 초과 (API 주문가능금액 부족)"}
                        else:
                            logger.log_system(f"[매수검증성공] {symbol} - API 주문가능금액 검증 통과: 주문금액={order_amount:,.0f}원, API주문가능금액={api_ord_psbl_cash:,.0f}원")
                    
                    # 내부 가용 잔고 검증 (백업)
                    elif order_amount > internal_available_cash:
                        logger.log_system(f"[매수실패] {symbol} - 주문금액({order_amount:,.0f}원)이 내부 가용 잔고({internal_available_cash:,.0f}원)를 초과합니다.")
                        self._increment_failure_count(symbol, "INSUFFICIENT_BALANCE", "주문가능금액 초과 (내부 가용 잔고 부족)")
                        return {"status": "failed", "reason": "주문가능금액 초과 (내부 가용 잔고 부족)"}
                    
                    logger.log_system(f"[매수검증] {symbol} - 주문금액: {order_amount:,.0f}원, 실제잔고: {available_cash:,.0f}원, 내부가용잔고: {internal_available_cash:,.0f}원")
                except Exception as balance_e:
                    logger.log_error(balance_e, f"{symbol} 계좌 잔고 확인 중 오류")
                    self._increment_failure_count(symbol, "BALANCE_CHECK_ERROR", "계좌 잔고 확인 중 오류")
                    return {"status": "failed", "reason": "계좌 잔고 확인 중 오류"}
                
                # 주문 ID 생성 (예약용)
                temp_order_id = account_state.generate_temp_order_id()
                
                # 내부 잔고 검증 및 예약 - 반드시 API 호출 전에 수행
                # 오류 처리 강화
                try:
                    reserve_result = await account_state.reserve_amount(symbol, temp_order_id, order_amount)
                    if not reserve_result:
                        self._increment_failure_count(symbol, "RESERVE_FAILED", "주문가능금액 예약 실패 (내부 검증 실패)")
                        return {"status": "failed", "reason": "주문가능금액 초과 (내부 검증 실패)"}
                except RuntimeError as loop_error:
                    # 이벤트 루프 관련 오류 처리
                    error_msg = str(loop_error)
                    if "different loop" in error_msg:
                        logger.log_warning(f"[이벤트루프오류] {symbol} - 금액 예약 중 이벤트 루프 불일치, 예약 없이 진행")
                        # 예약 실패 시에도 진행 (API에서 재검증됨)
                    else:
                        logger.log_error(loop_error, f"{symbol} 금액 예약 중 이벤트 루프 오류")
                        self._increment_failure_count(symbol, "RESERVE_ERROR", f"주문가능금액 예약 오류: {error_msg}")
                        return {"status": "failed", "reason": f"주문가능금액 예약 오류: {error_msg}"}
                except Exception as reserve_error:
                    logger.log_error(reserve_error, f"{symbol} 금액 예약 중 예외 발생")
                    self._increment_failure_count(symbol, "RESERVE_ERROR", f"주문가능금액 예약 오류: {str(reserve_error)}")
                    return {"status": "failed", "reason": f"주문가능금액 예약 오류: {str(reserve_error)}"}
            else:
                # 매도 주문의 경우 금액 계산만 (예약 과정 없음)
                order_amount = price * quantity
                temp_order_id = account_state.generate_temp_order_id()

            # 주문 실행 (재시도 로직 포함)
            retry_count = 0
            last_error = None
            
            while retry_count <= self.max_retries:
                try:
                    if retry_count > 0:
                        logger.log_system(f"[주문재시도] {symbol} {side} 주문 재시도 ({retry_count}/{self.max_retries})")
                        await asyncio.sleep(self.retry_delay)
                    
                    # API를 통한 주문 실행
                    if order_type.upper() == "MARKET":
                        order_result = api_client.place_order(
                            symbol=symbol,
                            order_type="MARKET",
                            side=side,
                            quantity=quantity
                        )
                    else:
                        order_result = api_client.place_order(
                            symbol=symbol,
                            order_type="LIMIT",
                            side=side,
                            quantity=quantity,
                            price=int(price)
                        )
                    
                    # 주문 성공
                    if order_result.get("rt_cd") == "0":
                        # 실패 카운터 초기화
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
                            "reason": reason,
                            "created_at": datetime.now()
                        }
                        
                        # DB에 주문 정보 저장
                        database_manager.save_order(order_data)
                        self.pending_orders[order_id] = order_data
                        
                        # 내부 계좌 상태 업데이트 (임시 ID를 실제 주문 ID로 갱신)
                        if side.upper() == "BUY":
                            await account_state.update_after_order(temp_order_id, success=True)
                        
                        # 주문 체결 대기 및 모니터링
                        if order_type.upper() == "MARKET":
                            # 시장가 주문은 즉시 체결로 간주
                            await self._handle_order_execution(order_id, order_data)
                        else:
                            # 지정가 주문은 체결 대기
                            await self._wait_for_order_execution(order_id, order_data)
                        
                        return {"status": "success", "order_id": order_id}
                    
                    # 주문 실패 - 내부 잔고 업데이트 (예약 취소)
                    if side.upper() == "BUY":
                        await account_state.update_after_order(temp_order_id, success=False)
                    
                    # 오류 메시지 처리
                    error_msg = order_result.get("msg1", "Unknown error")
                    error_code = order_result.get("rt_cd", "9999")
                    
                    # 특정 오류 코드에 대한 재시도 여부 결정
                    if error_code in ["9999", "9998"]:  # 타임아웃 또는 네트워크 오류
                        if retry_count < self.max_retries:
                            retry_count += 1
                            last_error = f"Timeout/Network error: {error_msg}"
                            continue
                    
                    # 주문가능금액 초과 오류 (rt_cd: 7)인 경우 계좌 잔고 강제 동기화
                    if error_code == "7" and "주문가능금액을 초과" in error_msg:
                        logger.log_warning(f"{symbol} - 주문가능금액 초과 오류, 계좌 잔고 강제 동기화")
                        await account_state.sync_with_api(force=True)
                    
                    # 실패 정보 기록 및 블랙리스트 처리
                    failures_count = self._increment_failure_count(symbol, error_code, error_msg)
                    
                    logger.log_system(f"[주문실패] {symbol} 주문 실패 ({failures_count}/{self.max_consecutive_failures}회) - 오류: {error_msg}")
                    return {"status": "failed", "reason": error_msg, "failure_count": failures_count}
                    
                except asyncio.TimeoutError:
                    if retry_count < self.max_retries:
                        retry_count += 1
                        last_error = "API timeout"
                        continue
                    else:
                        # 매수 주문이었다면 내부 예약 취소
                        if side.upper() == "BUY":
                            await account_state.cancel_reservation(temp_order_id)
                        raise
                except Exception as api_e:
                    # 예상치 못한 오류 발생 시
                    logger.log_error(api_e, f"{symbol} 주문 처리 중 오류")
                    
                    # 매수 주문이었다면 내부 예약 취소
                    if side.upper() == "BUY":
                        await account_state.cancel_reservation(temp_order_id)
                        
                    if retry_count < self.max_retries:
                        retry_count += 1
                        last_error = str(api_e)
                        continue
                    else:
                        return {"status": "failed", "reason": f"API 오류: {str(api_e)}"}
            
            # 모든 재시도 실패
            logger.log_system(f"[주문실패] {symbol} 최대 재시도 횟수 초과: {last_error}")
            
            # 실패 정보 기록
            failures_count = self._increment_failure_count(symbol, "MAX_RETRY_EXCEEDED", last_error or "최대 재시도 횟수 초과")
            
            return {"status": "failed", "reason": last_error or "최대 재시도 횟수 초과", "failure_count": failures_count}
            
        except Exception as e:
            logger.log_error(e, f"{symbol} 주문 요청 처리 중 오류")
            
            # 예상치 못한 오류에 대해서도 실패 정보 기록
            failures_count = self._increment_failure_count(symbol, "UNEXPECTED_ERROR", str(e))
            
            return {"status": "failed", "reason": str(e), "failure_count": failures_count}
    
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
                "realized_pnl": 0,
                "total_buy_amount": 0,  # 총 매수 금액
                "total_sell_amount": 0  # 총 매도 금액
            })
            
            # 이전 포지션 정보 저장 (로깅용)
            old_quantity = current_position.get("quantity", 0)
            old_avg_price = current_position.get("avg_price", 0)
            
            if side == "BUY":
                # 매수 - 평균가 계산
                new_quantity = current_position["quantity"] + quantity
                if new_quantity > 0:
                    # 총 매수 금액 업데이트
                    current_position["total_buy_amount"] += price * quantity
                    new_avg_price = current_position["total_buy_amount"] / new_quantity
                    current_position["quantity"] = new_quantity
                    current_position["avg_price"] = new_avg_price
                else:
                    current_position["quantity"] = 0
                    current_position["avg_price"] = 0
                    current_position["total_buy_amount"] = 0
                
            else:  # SELL
                # 매도 - 실현손익 계산
                sell_quantity = min(quantity, current_position["quantity"])
                if sell_quantity > 0:
                    # 총 매도 금액 업데이트 (total_sell_amount 키가 없으면 초기화)
                    if "total_sell_amount" not in current_position:
                        current_position["total_sell_amount"] = 0
                        
                    current_position["total_sell_amount"] += price * sell_quantity
                    
                    # 실현 손익 계산 (FIFO 방식)
                    realized_pnl = sell_quantity * (price - current_position["avg_price"])
                    if "realized_pnl" not in current_position:
                        current_position["realized_pnl"] = 0
                        
                    current_position["realized_pnl"] += realized_pnl
                    self.daily_pnl += realized_pnl
                    
                    # 수익률 계산
                    profit_rate = (price - current_position["avg_price"]) / current_position["avg_price"] * 100
                    
                    # 로깅
                    logger.log_system(
                        f"[매도처리] {symbol} - "
                        f"수량: {sell_quantity}주, "
                        f"평단가: {current_position['avg_price']:,.0f}원, "
                        f"매도가: {price:,.0f}원, "
                        f"수익률: {profit_rate:.2f}%, "
                        f"실현손익: {realized_pnl:,.0f}원"
                    )
                
                current_position["quantity"] -= quantity
                
                # 포지션 청산된 경우
                if current_position["quantity"] <= 0:
                    current_position["quantity"] = 0
                    current_position["avg_price"] = 0
                    current_position["total_buy_amount"] = 0
                    current_position["total_sell_amount"] = 0
            
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
            # 먼저 API로 최신 보유 종목 정보 조회
            latest_positions = await self.get_positions()
            if not latest_positions or "output1" not in latest_positions:
                logger.log_system("[포지션체크] 현재 보유 종목 정보가 없습니다.")
                return
                
            position_items = latest_positions.get("output1", [])
            
            # 보유 수량 > 0인 종목만 필터링
            valid_positions = {}
            for position in position_items:
                symbol = position.get("pdno", "")
                qty = int(position.get("hldg_qty", "0"))
                if symbol and qty > 0:
                    valid_positions[symbol] = {
                        "quantity": qty,
                        "avg_price": float(position.get("pchs_avg_pric", "0")),
                        "current_price": float(position.get("prpr", "0")),
                        "profit_rate": float(position.get("evlu_pfls_rt", "0")) / 100 if "evlu_pfls_rt" in position else 0
                    }
            
            # 유효한 포지션이 없으면 종료
            if not valid_positions:
                logger.log_system("[포지션체크] 유효한 보유 종목(수량 > 0)이 없습니다.")
                return
                
            logger.log_system(f"[포지션체크] 유효한 보유 종목 수: {len(valid_positions)}개, 종목 목록: {', '.join(valid_positions.keys())}")
            
            # 동시성 문제 방지를 위한 락 사용
            async with self._async_lock:
                # 메모리의 포지션 정보를 API 조회한 최신 정보로 업데이트
                self.positions = valid_positions
                
                for symbol, position in valid_positions.items():
                    try:
                        # 블랙리스트에 있는 종목은 건너뛰기
                        current_time = time.time()
                        if symbol in self.order_blacklist:
                            expire_time = self.order_blacklist[symbol]
                            if current_time < expire_time:
                                remaining_time = int((expire_time - current_time) / 60)
                                logger.log_system(f"[포지션체크] {symbol}: 블랙리스트에 등록된 종목(앞으로 {remaining_time}분), 처리 건너뜀")
                                continue
                            else:
                                # 블랙리스트 만료되면 제거
                                del self.order_blacklist[symbol]
                                if symbol in self.order_failures:
                                    self.order_failures[symbol] = {"최근_실패_시간": 0, "횟수": 0, "실패_원인": []}
                                logger.log_system(f"[블랙리스트 해제] {symbol}: 거래 재개 가능")
                        
                        # 현재가 조회 - API 응답에 현재가가 없거나 정확하지 않은 경우 별도 조회
                        if position["current_price"] <= 0:
                            price_data = api_client.get_current_price(symbol)
                            
                            # 현재가 조회 실패 시 건너뛰기
                            if price_data.get("rt_cd") != "0" or "output" not in price_data:
                                logger.log_system(f"[포지션체크] {symbol} 현재가 조회 실패, 건너뜀")
                                continue
                                
                            current_price = float(price_data["output"]["stck_prpr"])
                            position["current_price"] = current_price
                        else:
                            current_price = position["current_price"]
                        
                        # 수익률 계산 - API 응답의 수익률이 있으면 사용, 없으면 계산
                        if "profit_rate" in position and position["profit_rate"] != 0:
                            pnl_rate = position["profit_rate"]
                        else:
                            pnl_rate = (current_price - position["avg_price"]) / position["avg_price"] if position["avg_price"] > 0 else 0
                        
                        # 손절/익절 체크
                        max_loss_rate = self.trading_config.risk_params.get("max_loss_rate", 0.02)
                        max_profit_rate = self.trading_config.scalping_params.get("take_profit", 0.015)
                        
                        # 보유 수량이 0인 경우 처리 건너뜀 (이중 체크)
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
                            "symbol": symbol,
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
    
    async def get_positions(self) -> Dict[str, Any]:
        """보유 포지션 조회
        
        Returns:
            Dict[str, Any]: API로 조회한 보유 종목 정보. 포함하는 필드는 API 응답에 따르고, 일반적으로 output1과 output2를 포함한다.
        """
        try:
            # API로 포지션 조회
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, api_client.get_account_balance)
            
            if result and result.get("rt_cd") == "0":
                # 로그 확인 추가
                if "output1" in result:
                    num_positions = len(result["output1"]) if isinstance(result["output1"], list) else 1
                    logger.log_system(f"포지션 조회 성공: {num_positions}개 종목 정보 받음")
                else:
                    logger.log_system("포지션 조회 성공: 조회할 내용이 없습니다.")
            else:
                error_msg = result.get("msg1", "Unknown error") if result else "Unknown error"
                logger.log_system(f"포지션 조회 실패: {error_msg}")
                
            return result
        except Exception as e:
            logger.log_error(e, "Failed to get positions")
            return {"rt_cd": "E", "msg1": str(e)}
            
    async def get_account_balance(self) -> Dict[str, Any]:
        """계좌 잔고 조회"""
        try:
            # 계좌 상태 동기화 (5분 이상 경과 시 자동 동기화됨)
            account_info = await account_state.get_account_info()
            
            # API 응답 형식에 맞게 반환
            return {
                "rt_cd": "0",
                "msg1": "계좌 정보 조회 성공",
                "cash_balance": account_info["available_cash"],
                "total_balance": account_info["total_balance"],
                "internal_available_cash": account_info["internal_available_cash"],
                "output1": [{
                    "dnca_tot_amt": str(account_info["available_cash"]),
                    "tot_evlu_amt": str(account_info["total_balance"])
                }]
            }
            
        except Exception as e:
            logger.log_error(e, "계좌 잔고 조회 실패")
            return {
                "rt_cd": "9999", 
                "msg1": f"계좌 정보 조회 실패: {str(e)}", 
                "output1": []
            }

    async def _check_sell_availability(self, symbol: str, quantity: int) -> bool:
        """매도 가능 여부 확인"""
        try:
            # 1. 거래 정지 여부 확인
            trading_status = api_client.get_trading_status(symbol)
            if trading_status.get("rt_cd") != "0" or trading_status.get("output", {}).get("status") == "SUSPENDED":
                logger.log_system(f"[매도제한] {symbol} - 거래 정지 상태")
                return False
            
            # 2. 매도 제한 여부 확인
            restrictions = api_client.get_trading_restrictions(symbol)
            if restrictions.get("rt_cd") == "0":
                if restrictions.get("output", {}).get("sell_restricted"):
                    logger.log_system(f"[매도제한] {symbol} - 매도 제한 상태")
                    return False
            
            # 3. 보유 수량 재확인
            current_position = self.positions.get(symbol, {"quantity": 0})
            if current_position["quantity"] < quantity:
                logger.log_system(f"[매도제한] {symbol} - 보유 수량 부족")
                return False
            
            # 4. 일일 매도 한도 확인
            daily_sell_limit = self.trading_config.risk_params.get("daily_sell_limit", 100000000)  # 기본값 1억원
            if self.daily_sell_amount + (quantity * current_position.get("avg_price", 0)) > daily_sell_limit:
                logger.log_system(f"[매도제한] {symbol} - 일일 매도 한도 초과")
                return False
            
            return True
            
        except Exception as e:
            logger.log_error(e, f"Error checking sell availability for {symbol}")
            return False
    
    async def _wait_for_order_execution(self, order_id: str, order_data: Dict[str, Any]):
        """주문 체결 대기 및 모니터링"""
        try:
            start_time = time.time()
            check_count = 0
            
            while time.time() - start_time < self.max_order_wait_time:
                # 주문 상태 확인
                order_status = await self._check_order_status(order_id)
                check_count += 1
                
                if order_status["status"] == "FILLED":
                    # 체결 완료
                    await self._handle_order_execution(order_id, order_data)
                    return
                elif order_status["status"] == "CANCELLED":
                    # 주문 취소됨
                    logger.log_system(f"[주문취소] {order_data['symbol']} 주문이 취소되었습니다.")
                    return
                elif order_status["status"] == "REJECTED":
                    # 주문 거부됨
                    logger.log_system(f"[주문거부] {order_data['symbol']} 주문이 거부되었습니다.")
                    return
                elif order_status["status"] == "PARTIALLY_FILLED":
                    # 부분 체결 처리
                    if order_data["side"] == "SELL":
                        filled_quantity = order_status.get("filled_quantity", 0)
                        remaining_quantity = order_data["quantity"] - filled_quantity
                        logger.log_system(f"[부분체결] {order_data['symbol']} - 체결: {filled_quantity}주, 잔여: {remaining_quantity}주")
                        
                        # 부분 체결된 수량만큼 처리
                        partial_order_data = order_data.copy()
                        partial_order_data["quantity"] = filled_quantity
                        await self._handle_order_execution(order_id, partial_order_data)
                        
                        # 잔여 수량 재주문
                        if remaining_quantity > 0:
                            new_order_data = order_data.copy()
                            new_order_data["quantity"] = remaining_quantity
                            await self.place_order(
                                symbol=new_order_data["symbol"],
                                side=new_order_data["side"],
                                quantity=remaining_quantity,
                                price=new_order_data["price"],
                                order_type=new_order_data["order_type"],
                                strategy=new_order_data["strategy"],
                                reason=f"partial_fill_{order_id}"
                            )
                        return
                
                # 대기 시간 로깅
                if check_count % 5 == 0:  # 5회마다 로그 출력
                    elapsed_time = time.time() - start_time
                    logger.log_system(f"[주문대기] {order_data['symbol']} 주문 체결 대기 중... ({elapsed_time:.1f}초 경과)")
                
                await asyncio.sleep(self.order_check_interval)
            
            # 최대 대기 시간 초과
            logger.log_system(f"[주문타임아웃] {order_data['symbol']} 주문 체결 대기 시간 초과")
            
            # 주문 취소 시도
            try:
                await self.cancel_order(order_id)
                logger.log_system(f"[주문취소] {order_data['symbol']} 타임아웃으로 주문 취소")
            except Exception as cancel_error:
                logger.log_error(cancel_error, f"Failed to cancel order {order_id}")
            
        except Exception as e:
            logger.log_error(e, f"Error waiting for order execution: {order_id}")
    
    async def _check_order_status(self, order_id: str) -> Dict[str, Any]:
        """주문 상태 확인"""
        try:
            # API로 주문 상태 조회
            result = api_client.get_order_status(order_id)
            
            if result.get("rt_cd") == "0":
                status = result.get("output", {}).get("order_status", "UNKNOWN")
                return {"status": status}
            else:
                return {"status": "UNKNOWN"}
                
        except Exception as e:
            logger.log_error(e, f"Error checking order status: {order_id}")
            return {"status": "UNKNOWN"}
    
    async def _handle_order_execution(self, order_id: str, order_data: Dict[str, Any]):
        """주문 체결 처리"""
        try:
            # 주문 상태 업데이트
            order_data["status"] = "FILLED"
            database_manager.update_order(order_id, {"status": "FILLED"})
            
            # 매도 주문인 경우 보유 수량 확인
            if order_data["side"] == "SELL":
                current_position = self.positions.get(order_data["symbol"], {"quantity": 0})
                if current_position["quantity"] < order_data["quantity"]:
                    logger.log_system(f"[매도수량오류] {order_data['symbol']} - 요청 수량({order_data['quantity']})이 보유 수량({current_position['quantity']})보다 많습니다.")
                    return
                
                # 일일 매도 금액 업데이트
                sell_amount = order_data["price"] * order_data["quantity"]
                self.daily_sell_amount += sell_amount
                logger.log_system(f"[매도체결] {order_data['symbol']} - 일일 매도 금액 업데이트: {sell_amount:,.0f}원 (총 {self.daily_sell_amount:,.0f}원)")
            
            # 포지션 업데이트
            await self.update_position(
                order_data["symbol"],
                order_data["side"],
                order_data["quantity"],
                order_data["price"]
            )
            
            # 거래 기록 저장
            trade_data = {
                "symbol": order_data["symbol"],
                "side": order_data["side"],
                "price": order_data["price"],
                "quantity": order_data["quantity"],
                "order_type": order_data["order_type"],
                "status": "FILLED",
                "order_id": order_id,
                "commission": order_data["price"] * order_data["quantity"] * 0.0005,
                "strategy": order_data["strategy"],
                "reason": order_data["reason"]
            }
            
            # 매도인 경우 실현 손익 계산
            if order_data["side"] == "SELL":
                current_position = self.positions.get(order_data["symbol"], {"avg_price": 0})
                pnl = (order_data["price"] - current_position.get("avg_price", 0)) * order_data["quantity"]
                trade_data["pnl"] = pnl
                
                # 수익률 계산 및 로깅
                profit_rate = (order_data["price"] - current_position.get("avg_price", 0)) / current_position.get("avg_price", 1) * 100
                logger.log_system(f"[매도체결] {order_data['symbol']} - 수익률: {profit_rate:.2f}%, 실현손익: {pnl:,.0f}원")
                
                # 수익률에 따른 알림
                if profit_rate >= 2.0:  # 2% 이상 수익
                    await alert_system.notify_trade(trade_data, "HIGH_PROFIT")
                elif profit_rate <= -2.0:  # 2% 이상 손실
                    await alert_system.notify_trade(trade_data, "HIGH_LOSS")
            
            # 트레이드 DB에 저장
            database_manager.save_trade(trade_data)
            
            # 리스크 관리자에 거래 정보 업데이트
            await risk_manager.update_after_trade(trade_data)
            
            # 알림 전송
            await alert_system.notify_trade(trade_data)
            
            # 대기 중인 주문 목록에서 제거
            if order_id in self.pending_orders:
                del self.pending_orders[order_id]
            
            self.daily_trades += 1
            
            logger.log_system(f"[주문체결] {order_data['symbol']} 주문 체결 완료")
            
        except Exception as e:
            logger.log_error(e, f"Error handling order execution: {order_id}")

    # 주기적인 계좌 정보 동기화 기능 추가
    async def sync_account_state(self, force: bool = False) -> None:
        """계좌 상태 동기화"""
        await account_state.sync_with_api(force=force)

    def _increment_failure_count(self, symbol: str, error_code: str, error_msg: str) -> int:
        """
        실패 횟수 증가 및 블랙리스트 처리
        
        Args:
            symbol: 종목 코드
            error_code: 오류 코드
            error_msg: 오류 메시지
            
        Returns:
            int: 증가된 후의 실패 횟수
        """
        current_time = time.time()
        
        # 보유 수량 부족 오류는 블랙리스트 카운트에서 제외
        if "보유 수량 부족" in error_msg or "INSUFFICIENT_QUANTITY" in error_code:
            logger.log_system(f"[오류무시] {symbol} - 보유 수량 부족 오류는 블랙리스트 카운트에서 제외합니다.")
            return 0  # 블랙리스트 카운트 증가하지 않음
        
        if symbol not in self.order_failures:
            self.order_failures[symbol] = {
                "최근_실패_시간": current_time,
                "횟수": 1,
                "실패_원인": [{"시간": current_time, "원인": error_msg, "코드": error_code}]
            }
        else:
            last_failure_time = self.order_failures[symbol]["최근_실패_시간"]
            time_diff = current_time - last_failure_time
            
            if time_diff > self.failure_reset_time:
                self.order_failures[symbol] = {
                    "최근_실패_시간": current_time,
                    "횟수": 1,
                    "실패_원인": [{"시간": current_time, "원인": error_msg, "코드": error_code}]
                }
            else:
                self.order_failures[symbol]["횟수"] += 1
                self.order_failures[symbol]["최근_실패_시간"] = current_time
                self.order_failures[symbol]["실패_원인"].append({
                    "시간": current_time,
                    "원인": error_msg,
                    "코드": error_code
                })
        
        # 블랙리스트 조건 체크
        failures_count = self.order_failures[symbol]["횟수"]
        if failures_count >= self.max_consecutive_failures:
            # 블랙리스트 등록
            self.order_blacklist[symbol] = time.time() + self.blacklist_duration
            logger.log_system(f"[블랙리스트 등록] {symbol}: 연속 {failures_count}회 주문 실패로 {self.blacklist_duration//60}분간 주문 중지")
            
            # 알림 전송
            try:
                asyncio.create_task(alert_system.notify_system_status(
                    "WARNING",
                    f"{symbol} 연속 {failures_count}회 주문 실패로 블랙리스트 등록 ({self.blacklist_duration//60}분간 주문 중지)"
                ))
            except Exception as alert_error:
                logger.log_error(alert_error, f"{symbol} 블랙리스트 알림 전송 실패")
                
        return failures_count

# 싱글톤 인스턴스
order_manager = OrderManager()
