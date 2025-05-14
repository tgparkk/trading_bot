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
            self.max_reconnect_attempts = 10  # 최대 재시도 횟수 증가
            self.reconnect_delay = 10  # 초기 재연결 대기 시간 증가 (10초)
            self.auth_successful = False  # 인증 성공 여부 추적
            self.connection_lock = asyncio.Lock()  # 연결 동시성 제어
            self.subscription_semaphore = asyncio.Semaphore(5)  # 동시 구독 요청을 5개로 제한 (throttling)
            self.last_connection_attempt = 0  # 마지막 연결 시도 시간
            
            # 웹소켓 접속키 캐싱 (1년 유효)
            self.approval_key = None
            self.approval_key_expire_time = None
            
            # 구독 제한 관련 설정
            self.max_subscriptions = 100  # 최대 구독 가능 종목 수
            self.subscription_delay = 0.1  # 구독 요청 간 지연 시간 (초)
            
            self._initialized = True
        
    async def connect(self) -> bool:
        """웹소켓 연결
        
        Returns:
            bool: 연결 성공 여부
        """
        # 연결 동시성 제어 - 하나의 연결 시도만 허용
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
            
            try:
                logger.log_system(f"웹소켓 연결 시도: {self.ws_url}")
                
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
                    "ping_interval": 30,    # 30초마다 ping 전송
                    "ping_timeout": 10,     # ping 응답 10초 대기
                    "close_timeout": 5,     # 종료 시 5초 대기
                    "max_size": 1024 * 1024 * 10,  # 최대 메시지 크기 10MB
                    "max_queue": 32        # 최대 대기열 크기
                    # extra_headers 파라미터 제거 - websockets 15.0.1 버전에서 지원하지 않음
                }
                
                # 연결 시도 - 타임아웃 처리
                try:
                    logger.log_system("웹소켓 연결 시도 시작...")
                    self.ws = await asyncio.wait_for(
                        websockets.connect(self.ws_url, **connection_options),
                        timeout=15.0  # 연결 타임아웃 15초로 증가
                    )
                    logger.log_system("웹소켓 초기 연결 성공")
                except asyncio.TimeoutError:
                    logger.log_error(Exception("WebSocket connection timeout"), "웹소켓 연결 타임아웃 (15초)")
                    self.ws = None
                    return False
                except websockets.exceptions.InvalidURI as uri_error:
                    logger.log_error(uri_error, f"웹소켓 URL 오류: {self.ws_url}")
                    self.ws = None
                    return False
                except (websockets.exceptions.InvalidHandshake, 
                        websockets.exceptions.InvalidState,
                        websockets.exceptions.ConnectionClosed) as ws_error:
                    logger.log_error(ws_error, f"웹소켓 프로토콜 오류: {type(ws_error).__name__}")
                    self.ws = None
                    return False
                except OSError as os_error:
                    logger.log_error(os_error, f"네트워크 오류: {os_error}")
                    self.ws = None
                    return False
                
                # 웹소켓 연결이 확립된 상태
                self.running = True
                self.reconnect_attempts = 0  # 재연결 시도 횟수 초기화
                
                # 인증 시도
                logger.log_system("웹소켓 인증 시도...")
                try:
                    # 인증 전 웹소켓 상태 확인
                    if self.ws is None:
                        logger.log_system("웹소켓 객체가 없습니다! 인증 시도 전 연결 한 번 더 확인 필요.")
                        # 한 번 더 연결 시도
                        try:
                            self.ws = await asyncio.wait_for(
                                websockets.connect(self.ws_url, **connection_options),
                                timeout=15.0
                            )
                            logger.log_system("웹소켓 연결 재시도 성공")
                        except Exception as reconnect_error:
                            logger.log_error(reconnect_error, "웹소켓 재연결 시도 실패")
                            raise
                    
                    # 인증 시도
                    auth_success = await asyncio.wait_for(
                        self._authenticate(),
                        timeout=10.0  # 인증 타임아웃 10초
                    )
                    
                    if not auth_success:
                        logger.log_error(Exception("WebSocket authentication failed"), "웹소켓 인증 실패")
                        # 인증 실패 시 연결 정리
                        await self.close()
                        return False
                        
                except asyncio.TimeoutError:
                    logger.log_error(Exception("WebSocket authentication timeout"), "웹소켓 인증 타임아웃 (10초)")
                    await self.close()
                    return False
                except Exception as auth_error:
                    logger.log_error(auth_error, f"웹소켓 인증 오류: {type(auth_error).__name__}")
                    await self.close()
                    return False
                
                logger.log_system("웹소켓 인증 성공")
                
                # 메시지 수신 루프 및 핑 태스크 시작
                try:
                    # 메시지 수신 루프 시작
                    self._receive_task = asyncio.create_task(self._receive_messages())
                    
                    # 태스크 예외 처리를 위한 콜백 정의
                    def handle_exception(task):
                        if task.done() and not task.cancelled():
                            exception = task.exception()
                            if exception:
                                task_name = "receive_task" if task == self._receive_task else "ping_task"
                                logger.log_error(exception, f"웹소켓 {task_name} 예외 발생: {type(exception).__name__}")
                                # 재연결 태스크 시작
                                asyncio.create_task(self._handle_reconnect())
                    
                    # 예외 처리 콜백 등록
                    self._receive_task.add_done_callback(handle_exception)
                    
                    # 핑 태스크 시작
                    self._ping_task = asyncio.create_task(self._ping_loop())
                    self._ping_task.add_done_callback(handle_exception)
                    
                    logger.log_system("웹소켓 관리 태스크 시작 완료")
                except Exception as task_error:
                    logger.log_error(task_error, "웹소켓 태스크 시작 실패")
                    await self.close()
                    return False
                
                return True
                
            except Exception as e:
                self.ws = None
                self.running = False
                self.auth_successful = False
                logger.log_error(e, f"웹소켓 연결 실패: {type(e).__name__}")
                return False
    
    async def _authenticate(self):
        """웹소켓 인증"""
        try:
            # 웹소켓 접속 상태 로그
            logger.log_system(f"[WEBSOCKET] 인증 시작 - 웹소켓 현재 상태: {self.ws is not None}")
            
            # 웹소켓이 없으면 인증 불가
            if self.ws is None:
                logger.log_error(Exception("WebSocket object is None"), "웹소켓 객체가 None이라 인증 실패")
                return False
            
            # 접속키 획득 (1년 유효)
            logger.log_system("접속키 획득 시도 중...")
            approval_key = await self._get_approval_key()
            logger.log_system(f"접속키 획득 성공: {approval_key[:5]}...")
            
            # 인증 데이터 형식 - 한국투자증권 표준 프로토콜 사용
            tr_id = "VTTC0802U" # 웹소켓 인증용 tr_id
            auth_data = {
                "header": {
                    "approval_key": approval_key,
                    "custtype": "P",  # 개인
                    "tr_type": "1",  # 등록
                    "comid": "M4",  # 회사 ID
                    "tr_id": tr_id
                }
            }
            
            logger.log_system(f"[WEBSOCKET] 인증 데이터 준비 완료: {json.dumps(auth_data, ensure_ascii=False)[:100]}...")
            
            await self.ws.send(json.dumps(auth_data))
            logger.log_system("WebSocket authentication sent")
            
            # 인증 응답 대기 (최대 5초)
            try:
                response = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
                auth_response = json.loads(response)
                
                # 응답 확인 - 인증 성공 여부
                # 응답 로깅 추가 (디버깅용)
                logger.log_system(f"WebSocket 인증 응답: {json.dumps(auth_response, ensure_ascii=False, indent=2)[:500]}")
                
                if auth_response.get("header", {}).get("rslt_cd") == "0":
                    logger.log_system("WebSocket authentication successful")
                    self.auth_successful = True
                    return True
                else:
                    header = auth_response.get("header", {})
                    error_cd = header.get("rslt_cd", "Unknown")
                    error_msg = header.get("rslt_msg", "Unknown error")
                    logger.log_system(f"WebSocket authentication failed: code={error_cd}, message={error_msg}")
                    
                    # 오류 처리
                    if error_cd == "40":  # 임의로 40을 예시로 사용, 실제 코드에 따라 조정 필요
                        logger.log_system("Approval key may be invalid, getting new key...")
                        # 접속키 강제 갱신
                        self.approval_key = None
                        self.approval_key_expire_time = None
                    
                    self.auth_successful = False
                    return False
                    
            except asyncio.TimeoutError:
                logger.log_system("WebSocket authentication timeout - no response received")
                self.auth_successful = False
                return False
                
        except Exception as e:
            logger.log_error(e, "WebSocket authentication error")
            self.auth_successful = False
            return False
    
    async def _get_approval_key(self) -> str:
        """웹소켓 접속키 발급 (1년 유효)"""
        import aiohttp
        
        # 현재 시간
        current_time = datetime.now().timestamp()
        
        # 토큰 정보 확인 - API 클라이언트에서 취득
        logger.log_system("[WEBSOCKET] API 클라이언트 토큰 정보 확인...")
        from core.api_client import api_client
        token_status = api_client.check_token_status()
        logger.log_system(f"[WEBSOCKET] 토큰 상태: {token_status['status']}, 메시지: {token_status['message']}")
        
        # API 클라이언트의 토큰이 유효하지 않으면 강제 갱신
        if token_status['status'] != 'valid':
            logger.log_system("[WEBSOCKET] API 클라이언트 토큰 강제 갱신 시도")
            refresh_result = api_client.force_token_refresh()
            logger.log_system(f"[WEBSOCKET] API 클라이언트 토큰 강제 갱신 결과: {refresh_result['status']}")
            await asyncio.sleep(1.0)  # 토큰 갱신 후 잠시 대기
        
        # 접속키 강제 재발급 표시가 있는지 확인
        force_refresh = False  # 접속키 강제 갱신 비활성화
        
        # 기존 접속키가 유효한지 확인 (1개월 이상 남아있으면 재사용)
        if self.approval_key and self.approval_key_expire_time and not force_refresh:
            remaining_days = (self.approval_key_expire_time - current_time) / (24 * 60 * 60)
            if remaining_days > 30:  # 1개월 이상 남아있으면 재사용
                logger.log_system(f"웹소켓 접속키 재사용 (만료까지 {remaining_days:.0f}일 남음)")
                return self.approval_key
            else:
                logger.log_system(f"웹소켓 접속키 만료 임박 ({remaining_days:.0f}일 남음), 새로 발급")
        
        # 새 접속키 발급 - 재시도 로직 추가
        logger.log_system("웹소켓 접속키 발급 시작...")
        
        url = f"{self.config.base_url}/oauth2/Approval"
        headers = {
            "content-type": "application/json",
            "User-Agent": "Mozilla/5.0"  # User-Agent 추가
        }
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret
        }
        
        # 최대 3회 재시도
        for retry in range(3):
            try:
                # 타임아웃 추가
                timeout = aiohttp.ClientTimeout(total=15)  # 타임아웃 증가
                
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=body, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            
                            # 응답 검증 - 원하는 필드가 있는지 확인
                            if "approval_key" not in data:
                                logger.log_warning(f"API 응답에 approval_key 필드가 없습니다: {json.dumps(data)[:200]}")
                                if retry < 2:
                                    logger.log_system(f"접속키 발급 재시도 ({retry+1}/3)...")
                                    await asyncio.sleep(2)
                                    continue
                                else:
                                    raise Exception("API response missing approval_key field")
                                
                            self.approval_key = data["approval_key"]
                            # 접속키 만료 시간 설정 (1년)
                            self.approval_key_expire_time = current_time + (365 * 24 * 60 * 60)
                            
                            logger.log_system(f"웹소켓 접속키 발급 성공 (1년간 유효)")
                            return self.approval_key
                        else:
                            error_text = await response.text()
                            logger.log_warning(f"Failed to get approval key: {response.status}, {error_text}")
                            if retry < 2:
                                logger.log_system(f"접속키 발급 재시도 ({retry+1}/3)...")
                                await asyncio.sleep(2)
                                continue
                            else:
                                raise Exception(f"Failed to get approval key: {response.status}")
            except asyncio.TimeoutError:
                logger.log_warning("Approval key request timed out")
                if retry < 2:
                    logger.log_system(f"접속키 발급 타임아웃 후 재시도 ({retry+1}/3)...")
                    await asyncio.sleep(2)
                    continue
                else:
                    raise Exception("Approval key request timed out after 3 retries")
            except Exception as e:
                logger.log_warning(f"Error getting approval key: {str(e)}")
                if retry < 2:
                    logger.log_system(f"오류 후 접속키 발급 재시도 ({retry+1}/3)...")
                    await asyncio.sleep(2)
                    continue
                else:
                    logger.log_error(e, "Failed to get approval key after 3 retries")
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
            try:
                # 연결 상태 확인
                if not self.is_connected():
                    logger.log_system(f"웹소켓 연결이 없습니다. {symbol} 구독 전 연결 시도... (시도 1/3)")
                    for i in range(3):  # 최대 3회 시도
                        if await self.connect():
                            break
                        if i < 2:  # 마지막 시도가 아니면
                            logger.log_system(f"웹소켓 연결이 없습니다. {symbol} 구독 전 연결 시도... (시도 {i+2}/3)")
                            await asyncio.sleep(3)  # 3초 대기 후 재시도
                    
                    if not self.is_connected():
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
                logger.log_error(e, f"{symbol} 종목 구독 실패")
                return False
    

    
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
                    # 메시지 수신 시 타임아웃 적용 (무한 대기 방지)
                    message = await asyncio.wait_for(self.ws.recv(), timeout=60.0)
                    await self._process_message(message)
                except asyncio.TimeoutError:
                    # 60초 동안 메시지가 없으면 ping 전송
                    logger.log_system("WebSocket message receive timeout, sending ping")
                    if self.ws:
                        try:
                            pong = await self.ws.ping()
                            await asyncio.wait_for(pong, timeout=5.0)
                            logger.log_system("WebSocket ping successful")
                        except:
                            logger.log_system("WebSocket ping failed, reconnecting")
                            await self._handle_reconnect()
                            break
                    else:
                        break
                        
        except websockets.exceptions.ConnectionClosed as e:
            logger.log_system(f"WebSocket connection closed: {str(e)}")
            self.auth_successful = False
            await self._handle_reconnect()
            
        except Exception as e:
            logger.log_error(e, "Error in message receive loop")
            self.auth_successful = False
            await self._handle_reconnect()
    
    async def _process_message(self, message: str):
        """메시지 처리"""
        try:
            data = json.loads(message)
            
            # 시스템 메시지 처리
            if data.get("header", {}).get("tr_id") == "PINGPONG":
                await self._handle_ping()
                return
            
            # 구독 응답 처리
            header = data.get("header", {})
            if header.get("rslt_cd") is not None:
                # 응답 코드가 있으면 응답 메시지
                if header.get("rslt_cd") == "0":
                    logger.log_system(f"Subscription operation successful: {message[:100]}...")
                else:
                    logger.log_system(f"Subscription operation failed: {message[:100]}...")
                return
            
            # 실시간 데이터 처리
            body = data.get("body", {})
            
            tr_id = header.get("tr_id")
            tr_key = body.get("tr_key")
            
            if not tr_id or not tr_key:
                logger.log_system(f"Invalid message format (missing tr_id or tr_key): {message[:100]}...")
                return
            
            callback_key = f"{tr_id}|{tr_key}"
            callback = self.callbacks.get(callback_key)
            
            if callback:
                await callback(body)
            
        except json.JSONDecodeError:
            logger.log_system(f"Non-JSON message received: {message[:100]}...")
        except Exception as e:
            logger.log_error(e, f"Error processing message: {message[:100]}...")
    
    async def _handle_ping(self):
        """핑퐁 처리"""
        try:
            pong_data = {
                "header": {
                    "tr_id": "PINGPONG",
                    "datetime": datetime.now().strftime("%Y%m%d%H%M%S")
                }
            }
            await self.ws.send(json.dumps(pong_data))
        except Exception as e:
            logger.log_error(e, "Error sending pong response")
            await self._handle_reconnect()
    
    async def _handle_reconnect(self):
        """재연결 처리"""
        async with self.connection_lock:
            if self.reconnect_attempts >= self.max_reconnect_attempts:
                logger.log_error(
                    Exception("Max reconnection attempts reached"),
                    "WebSocket reconnection failed"
                )
                self.running = False
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
                except:
                    pass
                self.ws = None
                
            self.running = False
            self.auth_successful = False
            
            await asyncio.sleep(wait_time)
            
            # 기존 상태 저장 및 초기화
            saved_callbacks = dict(self.callbacks)
            saved_subscriptions = dict(self.subscriptions)
            self.callbacks = {}
            self.subscriptions = {}
            
            # 연결 시도
            connection_success = await self.connect()
            
            # 연결에 실패한 경우 재시도 중단
            if not connection_success:
                logger.log_system("재연결 시도 실패, 다음 재시도를 준비합니다.")
                return
                
            # 연결에 성공했으면 기존 구독 복원 - 실패 시 신경 쓰지 않음
            if self.is_connected():
                logger.log_system("WebSocket reconnected. Restoring subscriptions...")
                
                # 복원 시 지연 시간 추가
                await asyncio.sleep(2)
                
                # 한 번에 너무 많은 구독 요청을 보내지 않기 위해 
                # 각 구독 사이에 지연을 두고 순차적으로 처리
                
                # 먼저 price 구독 복원
                restored_count = 0
                for symbol, tr_id in saved_subscriptions.items():
                    if "_" not in symbol:  # price 구독인 경우
                        callback = saved_callbacks.get(f"{tr_id}|{symbol}")
                        if callback:
                            # 구독 복원 사이에 지연 시간 추가 (서버 부하 방지)
                            await asyncio.sleep(1)
                            success = await self.subscribe_price(symbol, callback)
                            if success:
                                restored_count += 1
                            # 구독이 너무 많으면 일부만 복원 (최대 10개로 제한)
                            if restored_count >= 10:
                                break
                
                # 복원 성공 로그
                if restored_count > 0:
                    logger.log_system(f"Successfully restored {restored_count} price subscriptions")
    
    async def _ping_loop(self):
        """주기적으로 ping을 발송하여 연결 상태를 확인하는 루프"""
        try:
            while self.running and self.ws:
                try:
                    # 30초마다 ping 전송
                    await asyncio.sleep(30)
                    
                    if self.ws and self.ws.open:
                        pong_waiter = await self.ws.ping()
                        try:
                            await asyncio.wait_for(pong_waiter, timeout=5.0)
                            logger.log_debug("WebSocket ping-pong successful")
                        except asyncio.TimeoutError:
                            logger.log_warning("WebSocket pong timeout - connection may be unstable")
                            # 연결 불안정하면 재연결 시도
                            await self._handle_reconnect()
                            break
                    else:
                        logger.log_warning("WebSocket connection lost during ping loop")
                        await self._handle_reconnect()
                        break
                        
                except Exception as e:
                    logger.log_error(e, "Error in ping loop")
                    await self._handle_reconnect()
                    break
                    
        except asyncio.CancelledError:
            logger.log_system("Ping loop cancelled")
        except Exception as e:
            logger.log_error(e, "Unexpected error in ping loop")
    
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
        return self.ws is not None and self.running and self.auth_successful

# 싱글톤 인스턴스
ws_client = KISWebSocketClient()
