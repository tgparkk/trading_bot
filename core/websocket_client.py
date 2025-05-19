"""
한국투자증권 웹소켓 클라이언트
"""
import json
import asyncio
import websockets
import os
import threading
from typing import Dict, Any, Callable, List, Optional
from datetime import datetime
from config.settings import config, APIConfig
from utils.logger import logger

class KISWebSocketClient:
    """한국투자증권 웹소켓 클라이언트"""
    
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
            self.config = config.get("api", APIConfig.from_env())
            self.ws_url = self.config.ws_url
            self.app_key = self.config.app_key
            self.app_secret = self.config.app_secret
            self.ws = None
            self.running = False
            self.subscriptions = {}
            self.callbacks = {}
            self.reconnect_attempts = 0
            self.max_reconnect_attempts = 10
            self.reconnect_delay = 10
            self.auth_successful = False
            self.connection_lock = asyncio.Lock()
            self.subscription_semaphore = asyncio.Semaphore(5)
            self.last_connection_attempt = 0
            self.receive_lock = asyncio.Lock()  # 메시지 수신 동시성 제어를 위한 락 추가
            self.connection_check_interval = 30  # 연결 상태 확인 간격 (초)
            self.last_connection_check = 0  # 마지막 연결 상태 확인 시간
            
            # 웹소켓 접속키 캐싱 (1년 유효)
            self.approval_key = None
            self.approval_key_expire_time = None
            
            # 구독 제한 관련 설정
            self.max_subscriptions = 100
            self.subscription_delay = 0.1
            
            self._initialized = True
        
    async def connect(self) -> bool:
        """웹소켓 연결
        
        Returns:
            bool: 연결 성공 여부
        """
        # 연결 동시성 제어 - 하나의 연결 시도만 허용
        try:
            async with self.connection_lock:
                # 연결 상태 확인
                if self.is_connected():
                    logger.log_system("웹소켓이 이미 연결되어 있습니다.")
                    return True
                
                # 연결 시도 간격 관리 - 마지막 연결 시도와의 시간 간격 계산
                current_time = datetime.now().timestamp()
                time_since_last_attempt = current_time - self.last_connection_attempt
                
                # 너무 빠른 재시도 방지 - 최소 5초 간격 유지
                if time_since_last_attempt < 5:
                    wait_time = 5 - time_since_last_attempt
                    logger.log_system(f"연결 간격 제한: 마지막 시도 후 {time_since_last_attempt:.1f}초 경과. {wait_time:.1f}초 더 대기합니다...")
                    await asyncio.sleep(wait_time)
                
                # 연결 시도 시간 기록 (실제 시도 직전에 기록)
                self.last_connection_attempt = datetime.now().timestamp()
                
                # 최대 3번까지 재시도
                for attempt in range(3):
                    try:
                        logger.log_system(f"웹소켓 연결 시도 {attempt + 1}/3: {self.ws_url}")
                        
                        # 기존 연결이 있다면 완전히 정리
                        if self.ws:
                            logger.log_system("기존 웹소켓 연결 정리 중...")
                            try:
                                await self.close()
                                await asyncio.sleep(1)  # 자원 정리를 위한 추가 대기
                            except Exception as close_error:
                                logger.log_warning(f"기존 연결 정리 중 오류 (무시함): {close_error}")
                        
                        # 모든 상태 초기화
                        self.ws = None
                        self.running = False
                        self.auth_successful = False
                        
                        # 웹소켓 연결 옵션 설정
                        connection_options = {
                            "ping_interval": None,  # 자동 핑을 비활성화 (수동으로 처리)
                            "ping_timeout": None,   # 자동 핑 타임아웃도 비활성화
                            "close_timeout": 10,    # 종료 시 대기 시간 증가
                            "max_size": 1024 * 1024 * 10,  # 최대 메시지 크기 10MB
                            "max_queue": 32         # 최대 대기열 크기
                        }
                        
                        # 연결 시도
                        self.ws = await asyncio.wait_for(
                            websockets.connect(self.ws_url, **connection_options),
                            timeout=20.0  # 연결 타임아웃 증가
                        )
                        logger.log_system("웹소켓 초기 연결 성공")
                        
                        # 웹소켓 연결이 확립된 상태
                        self.running = True
                        self.reconnect_attempts = 0  # 재연결 시도 횟수 초기화
                        
                        # 인증 과정
                        logger.log_system("웹소켓 접속키 발급 시도...")
                        try:
                            await self._get_approval_key()
                            logger.log_system("웹소켓 접속키 발급 성공")
                            self.auth_successful = True
                        except Exception as key_error:
                            logger.log_error(key_error, "웹소켓 접속키 발급 실패")
                            await self.close()
                            if attempt < 2:  # 마지막 시도가 아니면 재시도
                                await asyncio.sleep(2)
                                continue
                            return False
                        
                        # 메시지 수신 루프 및 핑 태스크 시작
                        try:
                            # 태스크 생성 전에 기존 태스크가 있다면 취소
                            if hasattr(self, '_receive_task') and self._receive_task and not self._receive_task.done():
                                self._receive_task.cancel()
                            if hasattr(self, '_ping_task') and self._ping_task and not self._ping_task.done():
                                self._ping_task.cancel()
                            
                            # 메시지 수신 루프 시작
                            self._receive_task = asyncio.create_task(self._receive_messages())
                            
                            # 핑 태스크 시작
                            self._ping_task = asyncio.create_task(self._ping_loop())
                            
                            logger.log_system("웹소켓 관리 태스크 시작 완료")
                            return True
                            
                        except Exception as task_error:
                            logger.log_error(task_error, "웹소켓 태스크 시작 실패")
                            await self.close()
                            if attempt < 2:  # 마지막 시도가 아니면 재시도
                                await asyncio.sleep(2)
                                continue
                            return False
                            
                    except Exception as e:
                        logger.log_error(e, f"웹소켓 연결 시도 {attempt + 1}/3 실패")
                        await self.close()
                        if attempt < 2:  # 마지막 시도가 아니면 재시도
                            await asyncio.sleep(2)
                            continue
                        return False
                
                return False  # 모든 시도 실패
                
        except Exception as lock_error:
            logger.log_error(lock_error, "웹소켓 연결 락 획득 중 오류 발생")
            return False
    
    async def _get_approval_key(self) -> str:
        """웹소켓 접속키 발급 (1년 유효)
        
        한국투자증권 웹소켓 접속키를 발급받습니다. 접속키는 1년간 유효하며,
        캐싱하여 재사용합니다.
        
        Returns:
            str: 발급받은 웹소켓 접속키
        
        Raises:
            Exception: 접속키 발급 실패 시 발생
        """
        import aiohttp
        
        # 현재 시간
        current_time = datetime.now().timestamp()
        
        # 기존 접속키가 유효한지 확인 (1개월 이상 남아있으면 재사용)
        if self.approval_key and self.approval_key_expire_time:
            remaining_days = (self.approval_key_expire_time - current_time) / (24 * 60 * 60)
            if remaining_days > 30:  # 1개월 이상 남아있으면 재사용
                logger.log_debug(f"웹소켓 접속키 재사용 (만료까지 {remaining_days:.0f}일 남음)")
                return self.approval_key
            else:
                logger.log_system(f"웹소켓 접속키 만료 임박 ({remaining_days:.0f}일 남음), 새로 발급")
        
        # 새 접속키 발급
        logger.log_system("웹소켓 접속키 발급 시작")
        
        url = f"{self.config.base_url}/oauth2/Approval"
        headers = {
            "content-type": "application/json",
            "User-Agent": "Mozilla/5.0"
        }
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret
        }
        
        # 최대 3회 재시도
        for retry in range(3):
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=body, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            
                            if "approval_key" not in data:
                                logger.log_warning(f"API 응답에 approval_key 필드가 없습니다: {data}")
                                if retry < 2:
                                    await asyncio.sleep(2)
                                    continue
                                else:
                                    raise Exception("접속키 발급 응답에 approval_key 필드가 없습니다")
                                
                            # 접속키 저장 및 만료 시간 설정 (1년)
                            self.approval_key = data["approval_key"]
                            self.approval_key_expire_time = current_time + (365 * 24 * 60 * 60)
                            
                            logger.log_system("웹소켓 접속키 발급 성공 (1년간 유효)")
                            return self.approval_key
                        else:
                            error_text = await response.text()
                            logger.log_warning(f"접속키 발급 실패: HTTP {response.status}, {error_text}")
                            if retry < 2:
                                await asyncio.sleep(2)
                                continue
                            raise Exception(f"접속키 발급 실패 (HTTP {response.status})")
            except asyncio.TimeoutError:
                if retry < 2:
                    logger.log_warning(f"접속키 발급 요청 타임아웃 (재시도 {retry+1}/3)")
                    await asyncio.sleep(2)
                    continue
                raise Exception("접속키 발급 요청이 3회 타임아웃되었습니다")
            except Exception as e:
                # 마지막 시도가 아니면 재시도
                if retry < 2:
                    logger.log_warning(f"접속키 발급 오류: {str(e)} (재시도 {retry+1}/3)")
                    await asyncio.sleep(2)
                    continue
                # 마지막 시도에서 실패하면 예외 발생
                logger.log_error(e, "웹소켓 접속키 발급 최종 실패")
                raise
    
    async def subscribe_price(self, symbol: str, callback: Callable = None) -> bool:
        """실시간 가격 구독
        
        Args:
            symbol: 종목코드 (ex: "005930")
            callback: 가격 업데이트 시 호출될 콜백 함수
            
        Returns:
            bool: 구독 성공 여부
        """
        # 현재 구독 종목 수 확인
        if len(self.subscriptions) >= self.max_subscriptions:
            logger.log_warning(f"최대 구독 가능 종목 수({self.max_subscriptions})를 초과했습니다.")
            return False
            
        # 이미 구독 중인지 확인
        if symbol in self.subscriptions:
            logger.log_system(f"{symbol} 종목은 이미 구독 중입니다.")
            return True
            
        # 세마포어를 사용해 동시 구독 요청 제한
        async with self.subscription_semaphore:
            # 최대 3번까지 구독 시도
            for attempt in range(3):
                try:
                    # 연결 상태 확인
                    if not self.is_connected():
                        logger.log_system(f"웹소켓 연결이 없습니다. {symbol} 구독 전 연결 시도... (시도 {attempt + 1}/3)")
                        if not await self.connect():
                            if attempt < 2:  # 마지막 시도가 아니면
                                await asyncio.sleep(2)  # 2초 대기 후 재시도
                                continue
                            logger.log_error(Exception("WebSocket connection failed"), 
                                           f"{symbol} 구독을 위한 웹소켓 연결 실패")
                            return False
                    
                    # 구독 간 지연 시간 (서버 부하 방지)
                    await asyncio.sleep(self.subscription_delay)
                    
                    # 구독 데이터 구성
                    subscribe_data = {
                        "header": {
                            "tr_id": "H0STCNT0",  # 실시간 주식 체결가
                            "tr_key": symbol,  # 종목코드
                        }
                    }
                    
                    # 구독 요청 전송
                    await self.ws.send(json.dumps(subscribe_data))
                    logger.log_system(f"{symbol} 종목 실시간 가격 구독 요청")
                    
                    # 콜백 등록
                    if callback:
                        callback_key = f"H0STCNT0|{symbol}"
                        self.callbacks[callback_key] = callback
                    
                    # 구독 정보 추가
                    self.subscriptions[symbol] = {
                        "type": "price", 
                        "callback": callback,
                        "subscribed_at": datetime.now()
                    }
                    
                    logger.log_system(f"{symbol} 종목 실시간 가격 구독 성공 (현재 구독 종목 수: {len(self.subscriptions)})")
                    return True
                    
                except Exception as e:
                    logger.log_error(e, f"{symbol} 종목 구독 시도 {attempt + 1}/3 실패")
                    if attempt < 2:  # 마지막 시도가 아니면
                        await asyncio.sleep(2)  # 2초 대기 후 재시도
                        continue
                    return False
            
            return False  # 모든 시도 실패
    
    async def unsubscribe(self, symbol: str, feed_type: str = "price"):
        """구독 취소"""
        # 웹소켓 연결 상태 확인
        if not self.is_connected():
            logger.log_system(f"웹소켓 연결이 없어 {symbol}의 {feed_type} 구독 취소를 건너뜁니다.")
            
            # 단, 내부 상태는 업데이트
            if feed_type == "price" and symbol in self.subscriptions:
                if isinstance(self.subscriptions[symbol], dict) and "type" in self.subscriptions[symbol]:
                    callback_key = f"H0STCNT0|{symbol}"
                    if callback_key in self.callbacks:
                        del self.callbacks[callback_key]
                    del self.subscriptions[symbol]
                    logger.log_system(f"Cleaned up subscription info for {symbol} {feed_type}")
                else:
                    # 이전 버전 호환성 유지
                    tr_id = self.subscriptions.get(symbol)
                    callback_key = f"{tr_id}|{symbol}"
                    if callback_key in self.callbacks:
                        del self.callbacks[callback_key]
                    del self.subscriptions[symbol]
                    logger.log_system(f"Cleaned up subscription info for {symbol} {feed_type}")
            elif f"{symbol}_{feed_type}" in self.subscriptions:
                tr_id = self.subscriptions.get(f"{symbol}_{feed_type}")
                callback_key = f"{tr_id}|{symbol}"
                if callback_key in self.callbacks:
                    del self.callbacks[callback_key]
                del self.subscriptions[f"{symbol}_{feed_type}"]
                logger.log_system(f"Cleaned up subscription info for {symbol} {feed_type}")
                
            return
        
        if feed_type == "price":
            tr_id = self.subscriptions.get(symbol)
        else:
            tr_id = self.subscriptions.get(f"{symbol}_{feed_type}")
        
        if not tr_id:
            return
        
        try:
            unsubscribe_data = {
                "header": {
                    "approval_key": await self._get_approval_key(),
                    "custtype": "P",
                    "tr_type": "2",  # 해지
                    "content-type": "utf-8"
                },
                "body": {
                    "input": {
                        "tr_id": tr_id,
                        "tr_key": symbol
                    }
                }
            }
            
            await self.ws.send(json.dumps(unsubscribe_data))
            
            # 콜백 제거
            callback_key = f"{tr_id}|{symbol}"
            if callback_key in self.callbacks:
                del self.callbacks[callback_key]
            
            if feed_type == "price":
                del self.subscriptions[symbol]
            else:
                del self.subscriptions[f"{symbol}_{feed_type}"]
            
            logger.log_system(f"Unsubscribed from {feed_type} feed for {symbol}")
        except Exception as e:
            logger.log_error(e, f"Error unsubscribing from {feed_type} feed for {symbol}")
            
            # 웹소켓 연결 문제인 경우, 컬렉션에서 구독 정보는 삭제
            if "ConnectionClosed" in str(e) or "NoneType" in str(e):
                logger.log_system(f"Removing subscription info for {symbol} due to connection issues")
                callback_key = f"{tr_id}|{symbol}"
                if callback_key in self.callbacks:
                    del self.callbacks[callback_key]
                
                if feed_type == "price" and symbol in self.subscriptions:
                    del self.subscriptions[symbol]
                elif f"{symbol}_{feed_type}" in self.subscriptions:
                    del self.subscriptions[f"{symbol}_{feed_type}"]
    
    async def _receive_messages(self):
        """메시지 수신 루프"""
        try:
            while self.running and self.ws is not None:
                try:
                    # 메시지 수신 동시성 제어
                    async with self.receive_lock:
                        # 메시지 수신 시 타임아웃 적용 (무한 대기 방지)
                        message = await asyncio.wait_for(self.ws.recv(), timeout=60.0)
                        await self._process_message(message)
                except asyncio.TimeoutError:
                    # 60초 동안 메시지가 없으면 ping 전송
                    logger.log_system("WebSocket message receive timeout, sending ping")
                    if self.ws and not getattr(self.ws, 'closed', True):
                        try:
                            pong = await self.ws.ping()
                            await asyncio.wait_for(pong, timeout=5.0)
                            logger.log_system("WebSocket ping successful")
                        except Exception as ping_error:
                            logger.log_system(f"WebSocket ping failed: {str(ping_error)}, reconnecting")
                            await self._safe_reconnect()
                            break
                    else:
                        logger.log_system("WebSocket connection is not available, breaking receive loop")
                        break
                except websockets.exceptions.ConnectionClosedError as e:
                    logger.log_system(f"WebSocket connection closed: {str(e)}")
                    self.auth_successful = False
                    await self._safe_reconnect()
                    break
                except RuntimeError as e:
                    if "got Future" in str(e) and "attached to a different loop" in str(e):
                        logger.log_warning("이벤트 루프 불일치 오류 - 수신 루프를 중단합니다.")
                        break
                    else:
                        logger.log_error(e, "메시지 수신 루프에서 런타임 오류 발생")
                        await self._safe_reconnect()
                        break
                except Exception as e:
                    logger.log_error(e, "Error in message receive loop")
                    self.auth_successful = False
                    await self._safe_reconnect()
                    break
        except asyncio.CancelledError:
            logger.log_system("메시지 수신 루프가 취소되었습니다.")
        except Exception as e:
            logger.log_error(e, "메시지 수신 루프에서 예상치 못한 오류 발생")
    
    async def _process_message(self, message: str):
        """메시지 처리"""
        try:
            # 서버로부터 받은 메시지 길이 기록 (로그 길이 제한)
            max_log_length = 100
            shortened_message = message[:max_log_length] + ('...' if len(message) > max_log_length else '')
            logger.log_debug(f"수신 메시지: {shortened_message}")
            
            # JSON 형식인지 확인
            data = json.loads(message)
            
            # 오류 메시지 확인
            # 서버 오류 응답 처리 (rt_cd가 0이 아닌 경우)
            if "body" in data and "rt_cd" in data["body"] and data["body"]["rt_cd"] != "0":
                error_code = data["body"].get("rt_cd")
                error_msg = data["body"].get("msg1", "")
                error_cd = data["body"].get("msg_cd", "")
                
                logger.log_warning(f"서버 오류 응답: code={error_code}, msg_cd={error_cd}, msg={error_msg}")
                
                # 특정 오류 코드에 대한 처리
                if error_code == "9" and "OPSP9997" in error_cd:
                    logger.log_warning("서버가 메시지 형식을 인식하지 못함 - 무시하고 계속 진행")
                    return  # 무시하고 계속 진행
                
                # 심각한 오류인 경우 연결 재설정
                if error_code in ["1", "2", "3", "9"]:
                    logger.log_warning("심각한 서버 오류 발생 - 연결 재설정 필요")
                    # 연결 상태 유지하고 재연결은 하지 않음
                    # 다음 상태 체크 루프에서 필요 시 재연결
                    return
            
            # 시스템 메시지 처리
            if "header" in data and "tr_id" in data["header"]:
                tr_id = data["header"]["tr_id"]
                
                # 핑퐁 메시지 처리
                if tr_id == "PINGPONG":
                    logger.log_debug("서버 핑퐁 메시지 수신 - 응답 불필요")
                    return
                
                # 응답 코드가 있는 경우 (구독 응답 등)
                if "rslt_cd" in data.get("header", {}):
                    rslt_cd = data["header"]["rslt_cd"]
                    if rslt_cd == "0":
                        logger.log_system(f"서버 응답 성공: {shortened_message}")
                    else:
                        logger.log_warning(f"서버 응답 실패: {shortened_message}")
                    return
            
            # 실시간 데이터 처리
            if "body" in data:
                body = data.get("body", {})
                tr_id = data.get("header", {}).get("tr_id")
                tr_key = body.get("tr_key", "")
                
                if not tr_id or not tr_key:
                    logger.log_debug(f"형식 오류 메시지 (tr_id/tr_key 누락) - 처리 스킵: {shortened_message}")
                    return
                
                callback_key = f"{tr_id}|{tr_key}"
                callback = self.callbacks.get(callback_key)
                
                if callback:
                    await callback(body)
                else:
                    logger.log_debug(f"등록된 콜백 없음: {callback_key}")
            
        except json.JSONDecodeError:
            # 로그 길이 제한
            shortened = message[:100] + ('...' if len(message) > 100 else '')
            logger.log_debug(f"JSON 아닌 메시지 수신: {shortened}")
        except Exception as e:
            logger.log_error(e, f"메시지 처리 중 오류 발생")
    
    async def _handle_ping(self):
        """핑퐁 처리"""
        try:
            if not self.is_connected():
                logger.log_warning("웹소켓이 연결되지 않은 상태에서 ping 요청을 받았습니다.")
                return
                
            pong_data = {
                "header": {
                    "tr_id": "PINGPONG",
                    "datetime": datetime.now().strftime("%Y%m%d%H%M%S")
                }
            }
            
            try:
                await self.ws.send(json.dumps(pong_data))
            except websockets.exceptions.ConnectionClosedError as e:
                logger.log_warning(f"Pong 응답 전송 중 연결이 닫힘: {str(e)}")
                # 연결이 이미 닫혔을 때는 단순히 상태만 업데이트
                self.running = False
                self.auth_successful = False
                # 재연결은 별도 태스크로 시작하지 않고 상태만 업데이트
            except Exception as e:
                logger.log_error(e, "Error sending pong response")
                # 다른 예외의 경우에만 재연결 시도
                self.running = False
                self.auth_successful = False
                try:
                    # 별도 태스크가 아닌 직접 호출을 통해 이벤트 루프 충돌 회피
                    await self._safe_reconnect()
                except Exception as reconnect_error:
                    logger.log_error(reconnect_error, "Pong 응답 후 재연결 시도 중 오류")
        except Exception as e:
            logger.log_error(e, "Pong 처리 중 예상치 못한 오류 발생")

    async def _safe_reconnect(self):
        """안전한 재연결 처리 - 래핑 함수로 이벤트 루프 문제 방지"""
        try:
            logger.log_system("안전한 재연결 프로세스 시작")
            
            # 이미 재연결 중인지 확인
            if not self.running:
                logger.log_warning("이미 재연결 중이거나 연결이 종료된 상태")
                return
                
            # 상태 먼저 업데이트
            self.running = False
            self.auth_successful = False
            
            if self.reconnect_attempts >= self.max_reconnect_attempts:
                logger.log_error(
                    Exception("Max reconnection attempts reached"),
                    "WebSocket reconnection failed"
                )
                return
            
            self.reconnect_attempts += 1
            
            # 재연결 시도마다 대기 시간 증가 (지수적 백오프)
            wait_time = self.reconnect_delay * (2 ** (self.reconnect_attempts - 1))
            wait_time = min(wait_time, 60)  # 최대 60초로 제한
            
            logger.log_system(
                f"Attempting to reconnect ({self.reconnect_attempts}/{self.max_reconnect_attempts}) "
                f"after {wait_time} seconds"
            )
            
            # 기존 연결 정리
            if self.ws:
                try:
                    await self.ws.close()
                except Exception as close_error:
                    logger.log_warning(f"기존 웹소켓 종료 중 오류 (무시): {str(close_error)}")
                self.ws = None
            
            # 지연 시간 대기
            await asyncio.sleep(wait_time)
            
            # 단일 비동기 함수로 간단하게 구현 - Lock 사용 없이
            logger.log_system("웹소켓 재연결 시도 - 직접 방식")
            await self._basic_reconnect()
            
        except asyncio.CancelledError:
            logger.log_warning("안전한 재연결 작업이 취소되었습니다.")
        except Exception as e:
            logger.log_error(e, "안전한 재연결 중 오류 발생")
    
    async def _basic_reconnect(self):
        """기본 재연결 로직 - 락 없이 동작하는 단순 버전"""
        try:
            # 단순 연결 시도
            logger.log_system("기본 재연결 로직 실행 중...")
            
            # 웹소켓 연결 옵션 설정 - 타임아웃 값을 더 길게 설정
            connection_options = {
                "ping_interval": None,  # 자동 핑을 비활성화 (수동으로 처리)
                "ping_timeout": None,   # 자동 핑 타임아웃도 비활성화
                "close_timeout": 10,    # 종료 시 대기 시간 증가
                "max_size": 1024 * 1024 * 10,  # 최대 메시지 크기 10MB
                "max_queue": 32        # 최대 대기열 크기
            }
            
            try:
                # 웹소켓 연결 시도
                logger.log_system("기본 웹소켓 연결 시도...")
                self.ws = await asyncio.wait_for(
                    websockets.connect(self.ws_url, **connection_options),
                    timeout=20.0  # 연결 타임아웃 증가
                )
                logger.log_system("기본 웹소켓 연결 성공")
                
                # 보안 헤더 설정 (필요한 경우)
                # 웹소켓 접속키 발급
                await self._get_approval_key()
                
                # 성공적인 연결 설정
                self.running = True
                self.auth_successful = True
                self.reconnect_attempts = 0  # 재연결 성공 시 카운터 초기화
                
                # 메시지 수신 루프 시작
                logger.log_system("메시지 수신 루프 재시작...")
                asyncio.create_task(self._receive_messages())
                
                # 핑 루프 시작 - 연결 상태 확인용
                logger.log_system("핑 루프 재시작...")
                asyncio.create_task(self._ping_loop())
                
                logger.log_system("기본 재연결 성공!")
                return True
                
            except Exception as conn_error:
                logger.log_error(conn_error, "기본 재연결 연결 실패")
                self.ws = None
                self.running = False
                self.auth_successful = False
                return False
                
        except Exception as e:
            logger.log_error(e, "기본 재연결 로직 실행 중 오류 발생")
            return False
            
    async def _handle_reconnect(self):
        """재연결 처리 - 레거시 버전 유지 (이전 코드와의 호환성)"""
        try:
            # 안전한 재연결 메서드로 리다이렉트
            await self._safe_reconnect()
        except Exception as e:
            logger.log_error(e, "레거시 재연결 핸들러에서 오류 발생")
    
    async def _ping_loop(self):
        """주기적으로 연결 상태를 확인하는 루프"""
        try:
            while self.running:
                try:
                    # 연결 상태 확인 간격 조절
                    current_time = datetime.now().timestamp()
                    if current_time - self.last_connection_check < self.connection_check_interval:
                        await asyncio.sleep(1)
                        continue
                        
                    self.last_connection_check = current_time
                    
                    # 연결 상태 확인
                    if not self.is_connected():
                        logger.log_warning("연결 상태 확인 - 연결이 끊어진 상태 감지")
                        await self._safe_reconnect()
                        break
                    
                    # 헬스체크 메시지 전송
                    if self.ws and not getattr(self.ws, 'closed', True):
                        try:
                            ping_data = {
                                "header": {
                                    "tr_id": "PINGPONG",
                                    "datetime": datetime.now().strftime("%Y%m%d%H%M%S")
                                }
                            }
                            await self.ws.send(json.dumps(ping_data))
                            logger.log_debug("웹소켓 핑 메시지 전송 완료")
                        except Exception as ping_error:
                            logger.log_warning(f"핑 메시지 전송 실패: {str(ping_error)}")
                            await self._safe_reconnect()
                            break
                    
                except Exception as e:
                    logger.log_error(e, "연결 상태 확인 루프에서 오류 발생")
                    if self.running:
                        await self._safe_reconnect()
                    break
                    
        except asyncio.CancelledError:
            logger.log_system("연결 상태 확인 루프가 취소되었습니다")
        except Exception as e:
            logger.log_error(e, "연결 상태 확인 루프에서 예상치 못한 오류 발생")
    
    async def close(self):
        """웹소켓 연결 종료"""
        self.running = False
        self.auth_successful = False
        
        # 태스크 취소
        if hasattr(self, '_receive_task') and self._receive_task:
            self._receive_task.cancel()
        if hasattr(self, '_ping_task') and self._ping_task:
            self._ping_task.cancel()
            
        # 웹소켓 연결 종료
        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.log_debug(f"Error closing websocket: {e}")
            finally:
                self.ws = None
                
        logger.log_system("WebSocket connection closed")
    
    async def subscribe_multiple_prices(self, symbols: List[str], callback: Callable = None) -> Dict[str, bool]:
        """여러 종목의 실시간 가격 구독
        
        Args:
            symbols: 종목코드 리스트 (ex: ["005930", "000660"])
            callback: 가격 업데이트 시 호출될 콜백 함수
            
        Returns:
            Dict[str, bool]: 각 종목별 구독 성공 여부
        """
        results = {}
        total_symbols = len(symbols)
        success_count = 0
        failure_count = 0
        
        logger.log_system(f"{total_symbols}개 종목 실시간 가격 구독 시작")
        
        # 배치 처리를 위해 종목을 나눕니다
        batch_size = 10  # 한 번에 10개씩 처리
        
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            
            # 병렬 처리
            tasks = []
            for symbol in batch:
                if symbol in self.subscriptions:
                    results[symbol] = True  # 이미 구독 중
                    success_count += 1
                else:
                    task = self.subscribe_price(symbol, callback)
                    tasks.append((symbol, task))
            
            # 배치 내 종목들 동시 처리
            for symbol, task in tasks:
                try:
                    result = await task
                    results[symbol] = result
                    if result:
                        success_count += 1
                    else:
                        failure_count += 1
                except Exception as e:
                    logger.log_error(e, f"{symbol} 구독 중 오류 발생")
                    results[symbol] = False
                    failure_count += 1
            
            # 배치 간 지연시간 (서버 부하 방지)
            if i + batch_size < len(symbols):
                await asyncio.sleep(1.0)
                logger.log_system(f"구독 진행중: {min(i + batch_size, total_symbols)}/{total_symbols} 종목 완료")
        
        logger.log_system(
            f"종목 구독 완료 - 성공: {success_count}, 실패: {failure_count}, "
            f"총 구독 종목 수: {len(self.subscriptions)}/{self.max_subscriptions}"
        )
        
        return results
    
    def get_subscription_status(self) -> Dict[str, Any]:
        """현재 구독 상태 정보 반환"""
        return {
            "total_subscriptions": len(self.subscriptions),
            "max_subscriptions": self.max_subscriptions,
            "subscribed_symbols": list(self.subscriptions.keys()),
            "semaphore_available": self.subscription_semaphore._value,
            "is_connected": self.is_connected(),
            "auth_successful": self.auth_successful
        }
    
    def is_connected(self) -> bool:
        """웹소켓 연결 상태 확인"""
        # 웹소켓 객체가 존재하고, running 상태이며, 인증이 성공적으로 완료되었는지 확인
        ws_exists = self.ws is not None
        not_closed = False
        
        if ws_exists:
            try:
                # ClientConnection 객체의 closed 속성을 안전하게 확인
                # getattr를 사용하여 속성이 없는 경우 기본값 반환
                not_closed = not getattr(self.ws, 'closed', True)
            except Exception as e:
                logger.log_debug(f"웹소켓 상태 확인 중 오류 (무시): {str(e)}")
                not_closed = False
        
        is_active = ws_exists and not_closed and self.running and self.auth_successful
        
        # 디버그 로그가 너무 많은 것을 방지하기 위해 상태 변경 시에만 로깅
        if hasattr(self, '_last_connection_state') and self._last_connection_state != is_active:
            state_str = "연결됨" if is_active else "연결 안됨"
            details = f"ws_exists={ws_exists}, not_closed={not_closed}, running={self.running}, auth={self.auth_successful}"
            logger.log_debug(f"웹소켓 연결 상태 변경: {state_str} ({details})")
        
        # 마지막 상태 저장
        self._last_connection_state = is_active
        
        return is_active

# 싱글톤 인스턴스
ws_client = KISWebSocketClient()
