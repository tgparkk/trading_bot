"""
계좌 상태 관리 모듈
계좌 잔고를 프로그램 내부에서 관리하여 주문 가능 금액 초과 문제를 방지합니다.
"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import threading

from utils.logger import logger
from core.api_client import api_client

class AccountState:
    """계좌 상태 관리 클래스"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        """싱글톤 패턴 구현"""
        if cls._instance is None:
            cls._instance = super(AccountState, cls).__new__(cls)
        return cls._instance
    
    def __init__(self):
        """초기화"""
        if not hasattr(self, 'initialized'):
            self.available_cash = 0  # 가용 현금
            self.ord_psbl_cash = 0   # 주문가능금액 
            self.total_balance = 0   # 총 평가금액
            self.last_sync_time = None  # 마지막 동기화 시간
            self.last_api_call_time = 0  # API 호출 제한을 위한 마지막 호출 시간
            self.pending_orders = {}  # 보류 중인 주문 (아직 체결되지 않은)
            self.ordered_amount = 0  # 주문 중인 금액 (아직 체결되지 않은)
            self.initialized = False
            self.api_call_interval = 3  # API 호출 최소 간격 (초)
            self._async_locks = {}  # 스레드/태스크별 락 객체
            self._thread_local = threading.local()  # 스레드 로컬 스토리지
    
    async def initialize(self) -> None:
        """계좌 상태 초기화"""
        if not self.initialized:
            await self.sync_with_api(force=True)
            self.initialized = True
            logger.log_system("계좌 상태 관리자 초기화 완료")
    
    def _get_async_lock(self):
        """현재 이벤트 루프에 적합한 락 객체 반환"""
        try:
            # 현재 이벤트 루프 가져오기
            current_loop = asyncio.get_running_loop()
            loop_id = id(current_loop)
            
            # 현재 루프용 락이 없으면 생성
            if loop_id not in self._async_locks:
                self._async_locks[loop_id] = asyncio.Lock()
                logger.log_system(f"새 이벤트 루프({loop_id})용 락 생성")
            
            return self._async_locks[loop_id]
        except RuntimeError:
            # 이벤트 루프가 없는 경우 (비동기 컨텍스트 외부에서 호출)
            logger.log_warning("이벤트 루프 외부에서 호출됨, 새 루프 생성")
            new_loop = asyncio.new_event_loop()
            loop_id = id(new_loop)
            self._async_locks[loop_id] = asyncio.Lock()
            asyncio.set_event_loop(new_loop)
            return self._async_locks[loop_id]
    
    async def sync_with_api(self, force: bool = False) -> bool:
        """API로부터 실제 계좌 잔고 정보 동기화"""
        current_time = time.time()
        
        # API 호출 제한 (최소 3초 간격)
        if not force and current_time - self.last_api_call_time < self.api_call_interval:
            logger.log_system(f"API 호출 제한으로 동기화 건너뜀 (마지막 호출로부터 {current_time - self.last_api_call_time:.1f}초)")
            return False
        
        try:
            # 현재 이벤트 루프에 맞는 락 획득
            current_lock = self._get_async_lock()
            
            # 락 획득 시도 (타임아웃 3초 설정)
            try:
                # 락 획득 시도
                lock_acquire_task = asyncio.create_task(current_lock.acquire())
                done, pending = await asyncio.wait([lock_acquire_task], timeout=3.0)
                
                # 타임아웃 확인
                if lock_acquire_task not in done:
                    # 타임아웃 발생, 남은 태스크 취소
                    lock_acquire_task.cancel()
                    logger.log_warning("계좌 동기화 락 획득 타임아웃, 강제 진행")
                    # 타임아웃 시 락 없이 진행 (경쟁 상태 위험 있으나 무한 대기보다 낫음)
                else:
                    # 락 획득 성공
                    logger.log_debug("계좌 동기화 락 획득 성공")
            except Exception as lock_e:
                logger.log_warning(f"락 획득 중 예외 발생: {str(lock_e)}, 락 없이 진행")
                # 예외 발생 시 락 없이 진행

            try:
                # API로부터 계좌 잔고 조회
                balance_data = api_client.get_account_balance()
                self.last_api_call_time = time.time()
                
                # API 오류 확인
                if balance_data.get("rt_cd") != "0":
                    error_msg = balance_data.get("msg1", "알 수 없는 오류")
                    logger.log_warning(f"계좌 잔고 조회 실패: {error_msg}")
                    return False
                
                # 데이터 형식에 따라 처리
                dnca_tot_amt = 0   # 예수금
                ord_psbl_cash = 0  # 주문가능금액
                tot_evlu_amt = 0   # 총 평가금액
                
                # 새로운 구조 확인 (output2에 있는 예수금 정보)
                if "output2" in balance_data and balance_data["output2"]:
                    output2 = balance_data["output2"]
                    if isinstance(output2, list) and output2:
                        item = output2[0]
                        # 주문가능금액 정보 추출
                        if "ord_psbl_cash" in item:
                            try:
                                ord_psbl_cash = float(item["ord_psbl_cash"])
                                logger.log_system(f"주문가능금액 정보(output2): {ord_psbl_cash:,.0f}원")
                            except (ValueError, TypeError) as e:
                                logger.log_warning(f"주문가능금액 정보 변환 오류: {e}")
                        
                        # 예수금 정보 추출
                        if "dnca_tot_amt" in item:
                            try:
                                dnca_tot_amt = float(item["dnca_tot_amt"])
                                logger.log_system(f"계좌 예수금 정보(output2): {dnca_tot_amt:,.0f}원")
                            except (ValueError, TypeError) as e:
                                logger.log_warning(f"예수금 정보 변환 오류: {e}")
                        
                        # 총 평가금액 추출
                        if "tot_evlu_amt" in item:
                            try:
                                tot_evlu_amt = float(item["tot_evlu_amt"])
                                logger.log_system(f"계좌 총평가금액 정보(output2): {tot_evlu_amt:,.0f}원")
                            except (ValueError, TypeError) as e:
                                logger.log_warning(f"총평가금액 정보 변환 오류: {e}")
                    elif isinstance(output2, dict):
                        # 주문가능금액 정보 추출
                        if "ord_psbl_cash" in output2:
                            try:
                                ord_psbl_cash = float(output2["ord_psbl_cash"])
                                logger.log_system(f"주문가능금액 정보(output2 dict): {ord_psbl_cash:,.0f}원")
                            except (ValueError, TypeError) as e:
                                logger.log_warning(f"주문가능금액 정보 변환 오류: {e}")
                        
                        # 예수금 정보 추출
                        if "dnca_tot_amt" in output2:
                            try:
                                dnca_tot_amt = float(output2["dnca_tot_amt"])
                                logger.log_system(f"계좌 예수금 정보(output2 dict): {dnca_tot_amt:,.0f}원")
                            except (ValueError, TypeError) as e:
                                logger.log_warning(f"예수금 정보 변환 오류: {e}")
                        
                        # 총 평가금액 추출
                        if "tot_evlu_amt" in output2:
                            try:
                                tot_evlu_amt = float(output2["tot_evlu_amt"])
                                logger.log_system(f"계좌 총평가금액 정보(output2 dict): {tot_evlu_amt:,.0f}원")
                            except (ValueError, TypeError) as e:
                                logger.log_warning(f"총평가금액 정보 변환 오류: {e}")
                
                # 주문가능금액/예수금 정보가 없는 경우 output1에서 검색
                if (ord_psbl_cash <= 0 or dnca_tot_amt <= 0) and "output1" in balance_data:
                    output1 = balance_data["output1"]
                    
                    # output1이 리스트인 경우
                    if isinstance(output1, list) and output1:
                        for item in output1:
                            # 주문가능금액 확인
                            if "ord_psbl_cash" in item and ord_psbl_cash <= 0:
                                try:
                                    ord_psbl_cash = float(item["ord_psbl_cash"])
                                    logger.log_system(f"주문가능금액 정보(output1): {ord_psbl_cash:,.0f}원")
                                except (ValueError, TypeError) as e:
                                    logger.log_warning(f"주문가능금액 정보 변환 오류: {e}")
                            
                            # 예수금 확인
                            if "dnca_tot_amt" in item and dnca_tot_amt <= 0:
                                try:
                                    dnca_tot_amt = float(item["dnca_tot_amt"])
                                    logger.log_system(f"계좌 예수금 정보(output1): {dnca_tot_amt:,.0f}원")
                                except (ValueError, TypeError) as e:
                                    logger.log_warning(f"예수금 정보 변환 오류: {e}")
                    
                    # output1이 딕셔너리인 경우
                    elif isinstance(output1, dict):
                        # 주문가능금액 확인
                        if "ord_psbl_cash" in output1 and ord_psbl_cash <= 0:
                            try:
                                ord_psbl_cash = float(output1["ord_psbl_cash"])
                                logger.log_system(f"주문가능금액 정보(output1 dict): {ord_psbl_cash:,.0f}원")
                            except (ValueError, TypeError) as e:
                                logger.log_warning(f"주문가능금액 정보 변환 오류: {e}")
                        
                        # 예수금 확인
                        if "dnca_tot_amt" in output1 and dnca_tot_amt <= 0:
                            try:
                                dnca_tot_amt = float(output1["dnca_tot_amt"])
                                logger.log_system(f"계좌 예수금 정보(output1 dict): {dnca_tot_amt:,.0f}원")
                            except (ValueError, TypeError) as e:
                                logger.log_warning(f"예수금 정보 변환 오류: {e}")
                
                # API 응답에서 추출한 값으로 내부 상태 업데이트
                self.available_cash = dnca_tot_amt
                self.ord_psbl_cash = ord_psbl_cash  # 주문가능금액 저장
                self.total_balance = tot_evlu_amt
                
                # 동기화 완료 시간 기록
                self.last_sync_time = datetime.now()
                
                # 하드코딩된 테스트 금액 설정 (테스트용)
                # 실제 환경에서는 제거 필요
                if self.available_cash <= 0:
                    logger.log_warning("API에서 계좌 잔고가 0원으로 반환되었습니다. 테스트를 위해 160,000원으로 설정합니다.")
                    self.available_cash = 160000  # 테스트용 금액 설정
                    
                if self.ord_psbl_cash <= 0:
                    logger.log_warning("API에서 주문가능금액이 0원으로 반환되었습니다. 예수금의 95%로 설정합니다.")
                    self.ord_psbl_cash = self.available_cash * 0.95
                
                # 보류 중인 주문 금액 반영 (내부 가용 금액 계산 시)
                logger.log_system(f"계좌 잔고 동기화 완료: 실제잔고={self.available_cash:,.0f}원, "
                                f"API주문가능금액={self.ord_psbl_cash:,.0f}원, "
                                f"보류주문={self.ordered_amount:,.0f}원, "
                                f"내부가용잔고={self.get_internal_available_cash():,.0f}원")
                return True
            finally:
                # 락 해제 시도 (락을 획득한 경우만)
                if current_lock.locked():
                    current_lock.release()
                    logger.log_debug("계좌 동기화 락 해제 완료")
                
        except Exception as e:
            logger.log_error(e, "계좌 잔고 동기화 중 오류 발생")
            return False
    
    def get_internal_available_cash(self) -> float:
        """내부적으로 계산된 가용 현금 (보류 중인 주문 금액 제외)"""
        # 주문가능금액을 우선 사용하고, 없으면 예수금에서 보류주문액 차감
        if hasattr(self, 'ord_psbl_cash') and self.ord_psbl_cash > 0:
            return max(0, self.ord_psbl_cash - self.ordered_amount)
        else:
            return max(0, self.available_cash - self.ordered_amount)
    
    async def reserve_amount(self, symbol: str, order_id: str, amount: float) -> bool:
        """주문을 위한 금액 예약 (주문 전)"""
        try:
            # 현재 이벤트 루프에 맞는 락 획득
            current_lock = self._get_async_lock()
            
            # 락 획득 시도 (타임아웃 3초 설정)
            try:
                # 락 획득 시도
                lock_acquire_task = asyncio.create_task(current_lock.acquire())
                done, pending = await asyncio.wait([lock_acquire_task], timeout=3.0)
                
                # 타임아웃 확인
                if lock_acquire_task not in done:
                    # 타임아웃 발생, 남은 태스크 취소
                    lock_acquire_task.cancel()
                    logger.log_warning("금액 예약 락 획득 타임아웃, 강제 진행")
                    # 타임아웃 시 락 없이 진행
                else:
                    # 락 획득 성공
                    logger.log_debug("금액 예약 락 획득 성공")
            except Exception as lock_e:
                logger.log_warning(f"예약 락 획득 중 예외 발생: {str(lock_e)}, 락 없이 진행")
                # 예외 발생 시 락 없이 진행
                
            try:
                # 주문 전 최신 계좌 정보로 강제 동기화
                sync_success = await self.sync_with_api(force=True)
                if not sync_success:
                    logger.log_warning(f"[주문예약] {symbol} - 계좌 동기화 실패, 기존 정보로 진행")
                
                # 내부 가용 잔고 계산 (주문가능금액 기준)
                internal_available = self.get_internal_available_cash()
                # 안전 마진 적용 (가용 잔고의 98%만 사용 - 수수료 등 고려)
                safe_available = internal_available * 0.98
                
                if amount > safe_available:
                    logger.log_system(f"[주문거부] {symbol} - 내부 잔고 검증 실패: "
                                    f"주문액={amount:,.0f}원, 내부가용잔고={internal_available:,.0f}원, "
                                    f"안전마진적용={safe_available:,.0f}원")
                    return False
                    
                # 10만원 이상 주문은 추가 검증
                if amount > 100000:
                    # 실제 API에서 잔고 다시 확인
                    try:
                        balance_data = api_client.get_account_balance()
                        if balance_data.get("rt_cd") == "0":
                            # 데이터 형식에 따라 처리
                            api_ord_psbl_cash = 0  # API 주문가능금액
                            
                            # output2 확인 (주문가능금액)
                            if "output2" in balance_data and balance_data["output2"]:
                                output2 = balance_data["output2"]
                                if isinstance(output2, list) and output2:
                                    item = output2[0]
                                    if "ord_psbl_cash" in item:
                                        api_ord_psbl_cash = float(item["ord_psbl_cash"])
                                elif isinstance(output2, dict) and "ord_psbl_cash" in output2:
                                    api_ord_psbl_cash = float(output2["ord_psbl_cash"])
                            
                            # output1 확인 (주문가능금액이 없는 경우)
                            if api_ord_psbl_cash <= 0 and "output1" in balance_data:
                                output1 = balance_data["output1"]
                                if isinstance(output1, list) and output1:
                                    for item in output1:
                                        if "ord_psbl_cash" in item:
                                            api_ord_psbl_cash = float(item["ord_psbl_cash"])
                                            break
                                elif isinstance(output1, dict) and "ord_psbl_cash" in output1:
                                    api_ord_psbl_cash = float(output1["ord_psbl_cash"])
                            
                            # 실제 API 주문가능금액 기반 추가 검증
                            if api_ord_psbl_cash > 0:
                                # 안전 마진 적용 (API 주문가능금액의 98%만 사용 - 수수료 고려)
                                api_safe_available = api_ord_psbl_cash * 0.98
                                
                                if amount > api_safe_available:
                                    logger.log_system(f"[주문거부] {symbol} - API 주문가능금액 검증 실패: "
                                                    f"주문액={amount:,.0f}원, API주문가능금액={api_ord_psbl_cash:,.0f}원, "
                                                    f"안전마진적용={api_safe_available:,.0f}원")
                                    return False
                                else:
                                    logger.log_system(f"[주문검증] {symbol} - API 주문가능금액 검증 성공: "
                                                    f"주문액={amount:,.0f}원, API주문가능금액={api_ord_psbl_cash:,.0f}원")
                    except Exception as e:
                        # API 오류는 무시하고 내부 검증만 사용
                        logger.log_warning(f"API 주문가능금액 확인 중 오류: {str(e)}")
                        pass
                
                # 예약 처리
                self.pending_orders[order_id] = {
                    "symbol": symbol,
                    "amount": amount,
                    "reserved_time": datetime.now()
                }
                self.ordered_amount += amount
                
                logger.log_system(f"[주문예약] {symbol} - 금액 예약 성공: "
                                f"주문액={amount:,.0f}원, 남은내부가용잔고={self.get_internal_available_cash():,.0f}원")
                return True
            finally:
                # 락 해제 시도 (락을 획득한 경우만)
                if current_lock.locked():
                    current_lock.release()
                    logger.log_debug("금액 예약 락 해제 완료")
                    
        except Exception as e:
            logger.log_error(e, f"{symbol} 주문 금액 예약 중 오류 발생")
            return False
    
    async def update_after_order(self, order_id: str, success: bool = True) -> None:
        """주문 후 내부 잔고 업데이트"""
        try:
            # 현재 이벤트 루프에 맞는 락 획득
            current_lock = self._get_async_lock()
            
            # 락 획득 시도 (타임아웃 2초 설정)
            try:
                # 락 획득 시도
                lock_acquire_task = asyncio.create_task(current_lock.acquire())
                done, pending = await asyncio.wait([lock_acquire_task], timeout=2.0)
                
                # 타임아웃 확인
                if lock_acquire_task not in done:
                    # 타임아웃 발생, 남은 태스크 취소
                    lock_acquire_task.cancel()
                    logger.log_warning("주문 후 업데이트 락 획득 타임아웃, 강제 진행")
                else:
                    # 락 획득 성공
                    logger.log_debug("주문 후 업데이트 락 획득 성공")
            except Exception as lock_e:
                logger.log_warning(f"주문 후 업데이트 락 획득 중 예외: {str(lock_e)}, 락 없이 진행")
            
            try:
                if order_id in self.pending_orders:
                    order_info = self.pending_orders[order_id]
                    amount = order_info.get("amount", 0)
                    symbol = order_info.get("symbol", "unknown")
                    
                    if success:
                        # 실제 주문 성공 시 예약 금액은 이미 차감되었으므로 내부 상태만 업데이트
                        logger.log_system(f"[주문확정] {symbol} - 주문ID: {order_id}, 금액: {amount:,.0f}원 차감 완료")
                    else:
                        # 주문 실패 시 예약된 금액 복원
                        self.ordered_amount -= amount
                        logger.log_system(f"[주문실패] {symbol} - 주문ID: {order_id}, 예약금액 {amount:,.0f}원 복원")
                    
                    # 보류 주문에서 제거
                    del self.pending_orders[order_id]
            finally:
                # 락 해제 시도 (락을 획득한 경우만)
                if current_lock.locked():
                    current_lock.release()
                    logger.log_debug("주문 후 업데이트 락 해제 완료")
        except Exception as e:
            logger.log_error(e, f"주문 후 잔고 업데이트 중 오류: {order_id}")
    
    async def cancel_reservation(self, order_id: str) -> None:
        """주문 예약 취소 (타임아웃 등의 이유로)"""
        try:
            # 현재 이벤트 루프에 맞는 락 획득
            current_lock = self._get_async_lock()
            
            # 락 획득 시도 (타임아웃 2초 설정)
            try:
                # 락 획득 시도
                lock_acquire_task = asyncio.create_task(current_lock.acquire())
                done, pending = await asyncio.wait([lock_acquire_task], timeout=2.0)
                
                # 타임아웃 확인
                if lock_acquire_task not in done:
                    # 타임아웃 발생, 남은 태스크 취소
                    lock_acquire_task.cancel()
                    logger.log_warning("예약 취소 락 획득 타임아웃, 강제 진행")
                else:
                    # 락 획득 성공
                    logger.log_debug("예약 취소 락 획득 성공")
            except Exception as lock_e:
                logger.log_warning(f"예약 취소 락 획득 중 예외: {str(lock_e)}, 락 없이 진행")
            
            try:
                if order_id in self.pending_orders:
                    order_info = self.pending_orders[order_id]
                    amount = order_info.get("amount", 0)
                    symbol = order_info.get("symbol", "unknown")
                    
                    # 예약된 금액 복원
                    self.ordered_amount -= amount
                    logger.log_system(f"[예약취소] {symbol} - 주문ID: {order_id}, 예약금액 {amount:,.0f}원 취소")
                    
                    # 보류 주문에서 제거
                    del self.pending_orders[order_id]
            finally:
                # 락 해제 시도 (락을 획득한 경우만)
                if current_lock.locked():
                    current_lock.release()
                    logger.log_debug("예약 취소 락 해제 완료")
        except Exception as e:
            logger.log_error(e, f"주문 예약 취소 중 오류: {order_id}")
    
    async def cleanup_pending_orders(self, max_age_minutes: int = 10) -> None:
        """오래된 보류 주문 정리 (주문 처리 실패로 남은 예약 정리)"""
        try:
            # 현재 이벤트 루프에 맞는 락 획득
            current_lock = self._get_async_lock()
            
            # 락 획득 시도 (타임아웃 2초 설정)
            try:
                # 락 획득 시도
                lock_acquire_task = asyncio.create_task(current_lock.acquire())
                done, pending = await asyncio.wait([lock_acquire_task], timeout=2.0)
                
                # 타임아웃 확인
                if lock_acquire_task not in done:
                    # 타임아웃 발생, 남은 태스크 취소
                    lock_acquire_task.cancel()
                    logger.log_warning("보류 주문 정리 락 획득 타임아웃, 건너뜀")
                    return  # 타임아웃 시 작업 수행하지 않음
                else:
                    # 락 획득 성공
                    logger.log_debug("보류 주문 정리 락 획득 성공")
            except Exception as lock_e:
                logger.log_warning(f"보류 주문 정리 락 획득 중 예외: {str(lock_e)}, 건너뜀")
                return  # 예외 발생 시 작업 수행하지 않음
            
            try:
                current_time = datetime.now()
                expired_orders = []
                
                # 오래된 보류 주문 찾기
                for order_id, order_info in self.pending_orders.items():
                    reserved_time = order_info.get("reserved_time")
                    if reserved_time and (current_time - reserved_time).total_seconds() > (max_age_minutes * 60):
                        expired_orders.append(order_id)
                
                # 오래된 주문 예약 취소
                for order_id in expired_orders:
                    order_info = self.pending_orders[order_id]
                    amount = order_info.get("amount", 0)
                    symbol = order_info.get("symbol", "unknown")
                    
                    # 예약된 금액 복원
                    self.ordered_amount -= amount
                    logger.log_warning(f"[예약만료] {symbol} - 주문ID: {order_id}, 예약금액 {amount:,.0f}원 (만료시간: {max_age_minutes}분)")
                    
                    # 보류 주문에서 제거
                    del self.pending_orders[order_id]
            finally:
                # 락 해제 시도 (락을 획득한 경우만)
                if current_lock.locked():
                    current_lock.release()
                    logger.log_debug("보류 주문 정리 락 해제 완료")
        except Exception as e:
            logger.log_error(e, "보류 주문 정리 중 오류 발생")
    
    async def get_account_info(self) -> Dict[str, Any]:
        """계좌 정보 조회"""
        try:
            # 마지막 동기화로부터 5분 이상 지났으면 동기화
            if self.last_sync_time is None or (datetime.now() - self.last_sync_time) > timedelta(minutes=5):
                try:
                    # 동기화 시도
                    await self.sync_with_api()
                except RuntimeError as loop_error:
                    # 이벤트 루프 관련 오류 처리
                    error_msg = str(loop_error)
                    if "different loop" in error_msg:
                        logger.log_warning(f"계좌 정보 조회 중 이벤트 루프 불일치 오류, 동기화 건너뜀")
                    else:
                        logger.log_error(loop_error, "계좌 정보 조회 중 이벤트 루프 오류")
                except Exception as sync_error:
                    logger.log_error(sync_error, "계좌 정보 조회 중 동기화 오류")
            
            # 주문가능금액 필드 추가
            ord_psbl_cash = getattr(self, 'ord_psbl_cash', 0)
            if ord_psbl_cash <= 0:
                # 주문가능금액이 없으면 예수금의 95%로 추정
                ord_psbl_cash = self.available_cash * 0.95
            
            return {
                "available_cash": self.available_cash,
                "ord_psbl_cash": ord_psbl_cash,  # 주문가능금액 추가
                "total_balance": self.total_balance,
                "internal_available_cash": self.get_internal_available_cash(),
                "ordered_amount": self.ordered_amount,
                "pending_orders_count": len(self.pending_orders),
                "last_sync_time": self.last_sync_time.strftime("%Y-%m-%d %H:%M:%S") if self.last_sync_time else None
            }
        except Exception as e:
            logger.log_error(e, "계좌 정보 조회 중 예외 발생")
            # 기본 정보 반환
            return {
                "available_cash": self.available_cash,
                "ord_psbl_cash": getattr(self, 'ord_psbl_cash', self.available_cash * 0.95),
                "total_balance": self.total_balance,
                "internal_available_cash": self.get_internal_available_cash(),
                "ordered_amount": self.ordered_amount,
                "pending_orders_count": len(self.pending_orders),
                "last_sync_time": self.last_sync_time.strftime("%Y-%m-%d %H:%M:%S") if self.last_sync_time else None,
                "error": str(e)
            }
    
    def generate_temp_order_id(self) -> str:
        """임시 주문 ID 생성 (예약 시 사용)"""
        timestamp = int(time.time() * 1000)
        random_suffix = hash(timestamp) % 10000
        return f"temp_{timestamp}_{random_suffix}"

# 전역 인스턴스 생성
account_state = AccountState() 