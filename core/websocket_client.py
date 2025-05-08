"""
한국투자증권 웹소켓 클라이언트
"""
import json
import asyncio
import websockets
import os
from typing import Dict, Any, Callable, List, Optional
from datetime import datetime
from config.settings import config, APIConfig
from utils.logger import logger

class KISWebSocketClient:
    """한국투자증권 웹소켓 클라이언트"""
    
    def __init__(self):
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
        self.subscription_semaphore = asyncio.Semaphore(2)  # 최대 2개 동시 구독 요청
        self.last_connection_attempt = 0  # 마지막 연결 시도 시간
        
        # SKIP_WEBSOCKET 환경 변수 확인 (모든 공백 제거)
        skip_websocket_env = os.environ.get('SKIP_WEBSOCKET', '')
        self.skip_websocket = skip_websocket_env.strip().lower() in ('true', 't', '1', 'yes', 'y')
        
        # 환경 변수 확인 로그 강화
        logger.log_system(f"⚠️ SKIP_WEBSOCKET 환경 변수 상태: '{skip_websocket_env}', 적용 여부(strip 후): {self.skip_websocket}")
        
        if self.skip_websocket:
            logger.log_system("⚠️ SKIP_WEBSOCKET=True 환경 변수가 설정되어 있습니다. 웹소켓 연결을 건너뜁니다.")
        else:
            logger.log_system("⚠️ SKIP_WEBSOCKET 환경 변수가 False 또는 설정되지 않아 웹소켓 연결을 시도합니다.")
        
    async def connect(self):
        """웹소켓 연결"""
        # SKIP_WEBSOCKET이 True면 연결하지 않음
        if self.skip_websocket:
            logger.log_system("SKIP_WEBSOCKET=True 설정으로 웹소켓 연결을 건너뜁니다.")
            return False
            
        # 연결 동시성 제어 - 하나의 연결 시도만 허용
        async with self.connection_lock:
            # 이미 접속 중이거나 최근 5초 이내에 시도한 경우 스킵
            current_time = datetime.now().timestamp()
            if self.is_connected() or (current_time - self.last_connection_attempt < 5):
                return
                
            self.last_connection_attempt = current_time
            
            try:
                # 연결할 때 ping_interval과 ping_timeout 설정 추가
                self.ws = await websockets.connect(
                    self.ws_url,
                    ping_interval=30,  # 30초마다 ping 전송
                    ping_timeout=10,   # ping 응답 10초 대기
                    close_timeout=5    # 종료 시 5초 대기
                )
                self.running = True
                self.reconnect_attempts = 0
                logger.log_system("WebSocket connected successfully")
                
                # 인증
                auth_success = await self._authenticate()
                if not auth_success:
                    logger.log_system("WebSocket authentication failed, closing connection")
                    await self.close()
                    return
                
                # 메시지 수신 루프 시작
                asyncio.create_task(self._receive_messages())
                
            except Exception as e:
                logger.log_error(e, "WebSocket connection failed")
                await self._handle_reconnect()
    
    async def _authenticate(self):
        """웹소켓 인증"""
        try:
            auth_data = {
                "header": {
                    "approval_key": await self._get_approval_key(),
                    "custtype": "P",  # 개인
                    "tr_type": "1",  # 등록
                    "content-type": "utf-8"
                }
            }
            
            await self.ws.send(json.dumps(auth_data))
            logger.log_system("WebSocket authentication sent")
            
            # 인증 응답 대기 (최대 5초)
            try:
                response = await asyncio.wait_for(self.ws.recv(), timeout=5.0)
                auth_response = json.loads(response)
                
                # 응답 확인 - 인증 성공 여부
                if auth_response.get("header", {}).get("rslt_cd") == "0":
                    logger.log_system("WebSocket authentication successful")
                    self.auth_successful = True
                    return True
                else:
                    error_msg = auth_response.get("header", {}).get("rslt_msg", "Unknown error")
                    logger.log_system(f"WebSocket authentication failed: {error_msg}")
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
        """웹소켓 접속키 발급"""
        import aiohttp
        
        url = f"{self.config.base_url}/oauth2/Approval"
        headers = {"content-type": "application/json"}
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "secretkey": self.app_secret
        }
        
        try:
            # 타임아웃 추가
            timeout = aiohttp.ClientTimeout(total=10)
            
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        return data["approval_key"]
                    else:
                        error_text = await response.text()
                        logger.log_system(f"Failed to get approval key: {response.status}, {error_text}")
                        raise Exception(f"Failed to get approval key: {response.status}")
        except asyncio.TimeoutError:
            logger.log_system("Approval key request timed out")
            raise Exception("Approval key request timed out")
        except Exception as e:
            logger.log_system(f"Error getting approval key: {str(e)}")
            raise
    
    async def subscribe_price(self, symbol: str, callback: Callable = None) -> bool:
        """실시간 가격 구독"""
        # 매번 환경 변수를 새로 확인 (스크립트 실행 중 변경 가능성 고려)
        skip_websocket_env = os.environ.get('SKIP_WEBSOCKET', '')
        skip_websocket_val = skip_websocket_env.strip().lower() in ('true', 't', '1', 'yes', 'y')
        
        # 환경 변수 확인 로그 (상세)
        logger.log_system(f"🔍 {symbol} 구독 시도 - SKIP_WEBSOCKET 환경 변수 값: '{skip_websocket_env}'")
        logger.log_system(f"🔍 SKIP_WEBSOCKET 환경 변수 길이: {len(skip_websocket_env)}, 공백제거 후 길이: {len(skip_websocket_env.strip())}")
        logger.log_system(f"🔍 적용되는 SKIP_WEBSOCKET 값: {skip_websocket_val}")
        
        # 환경 변수와 객체 속성이 일치하는지 확인하고 필요시 업데이트
        if skip_websocket_val != self.skip_websocket:
            logger.log_system(f"⚠️ SKIP_WEBSOCKET 값 불일치 감지 - 환경 변수: {skip_websocket_val}, 인스턴스: {self.skip_websocket}. 업데이트합니다.")
            self.skip_websocket = skip_websocket_val
            
        # SKIP_WEBSOCKET이 True면 구독하지 않고 성공으로 처리
        if self.skip_websocket:
            logger.log_system(f"[OK] {symbol} - SKIP_WEBSOCKET=True 설정으로 웹소켓 구독을 건너뜁니다.")
            # 콜백이 있으면 등록은 함
            if callback:
                callback_key = f"H0STCNT0|{symbol}"
                self.callbacks[callback_key] = callback
            # 구독 정보 추가
            self.subscriptions[symbol] = {"type": "price", "callback": callback}
            return True
            
        # SKIP_WEBSOCKET이 False인 경우 웹소켓 연결 시도
        logger.log_system(f"🔌 {symbol} - 실제 웹소켓 구독을 시도합니다...")
        
        # 세마포어를 사용해 최대 동시 구독 요청 제한
        async with self.subscription_semaphore:
            try:
                # 연결 상태 확인
                if not self.is_connected():
                    logger.log_system(f"웹소켓 연결이 없습니다. {symbol} 구독 전 연결 시도... (시도 1/3)")
                    for i in range(3):  # 최대 3회 시도
                        await self.connect()
                        if self.is_connected():
                            break
                        if i < 2:  # 마지막 시도가 아니면
                            logger.log_system(f"웹소켓 연결이 없습니다. {symbol} 구독 전 연결 시도... (시도 {i+2}/3)")
                            await asyncio.sleep(3)  # 3초 대기 후 재시도
                    
                    if not self.is_connected():
                        raise Exception("WebSocket connection failed after 3 attempts")
                
                # 구독 데이터 구성
                subscribe_data = {
                    "header": {
                        "tr_id": "H0STCNT0",  # 실시간 주식 체결가
                        "tr_key": symbol,  # 종목코드
                    }
                }
                
                # 구독 요청 전송
                await self.ws.send(json.dumps(subscribe_data))
                
                # 콜백 등록
                if callback:
                    callback_key = f"H0STCNT0|{symbol}"
                    self.callbacks[callback_key] = callback
                
                # 구독 정보 추가
                self.subscriptions[symbol] = {"type": "price", "callback": callback}
                
                return True
                
            except Exception as e:
                logger.log_error(e, f"[Failed to subscribe price for {symbol}]")
                return False
    
    async def subscribe_orderbook(self, symbol: str, callback: Callable):
        """실시간 호가 구독"""
        # 구독 동시성 제어
        async with self.subscription_semaphore:
            # 웹소켓 연결 확인 및 연결 시도
            retry_count = 0
            max_retries = 3
            
            while not self.is_connected() and retry_count < max_retries:
                retry_count += 1
                logger.log_system(f"웹소켓 연결이 없습니다. {symbol} 호가 구독 전 연결 시도... (시도 {retry_count}/{max_retries})")
                await self.connect()
                
                # 연결 시도 후 잠시 대기
                await asyncio.sleep(3)
            
            # 연결 후에도 웹소켓이 None이면 오류 로그 남기고 종료
            if not self.is_connected():
                logger.log_error(Exception(f"WebSocket connection failed after {max_retries} attempts"), 
                                f"Failed to subscribe orderbook for {symbol}")
                return False
            
            # 인증 확인
            if not self.auth_successful:
                logger.log_system(f"WebSocket not authenticated, attempting re-authentication for {symbol} orderbook")
                auth_success = await self._authenticate()
                if not auth_success:
                    logger.log_system(f"Re-authentication failed, cannot subscribe to {symbol} orderbook")
                    return False
                
                # 인증 후 추가 대기
                await asyncio.sleep(1)
            
            tr_id = "H0STASP0"  # 실시간 호가
            tr_key = symbol
            
            try:
                subscribe_data = {
                    "header": {
                        "approval_key": await self._get_approval_key(),
                        "custtype": "P",
                        "tr_type": "1",
                        "content-type": "utf-8"
                    },
                    "body": {
                        "input": {
                            "tr_id": tr_id,
                            "tr_key": tr_key
                        }
                    }
                }
                
                # 웹소켓이 여전히 연결되어 있는지 확인
                if not self.is_connected():
                    logger.log_system(f"웹소켓 연결이 끊어졌습니다. {symbol} 호가 구독을 취소합니다.")
                    return False
                
                await self.ws.send(json.dumps(subscribe_data))
                
                # 구독 응답 대기 추가
                asyncio.create_task(self._wait_for_subscription_response(symbol))
                
                # 콜백 등록
                self.callbacks[f"{tr_id}|{tr_key}"] = callback
                self.subscriptions[f"{symbol}_orderbook"] = tr_id
                
                logger.log_system(f"Subscribed to orderbook feed for {symbol}")
                
                # 구독 후 잠시 대기 (서버 부하 방지)
                await asyncio.sleep(1)
                
                return True
                
            except Exception as e:
                error_type = type(e).__name__
                logger.log_error(e, f"Error subscribing to orderbook feed for {symbol}: {error_type}")
                
                # 연결 오류인 경우 재연결 시도를 위해 연결 객체 초기화
                if "ConnectionClosed" in error_type or "WebSocketClosedError" in error_type:
                    logger.log_system(f"{symbol} 호가 구독 중 웹소켓 연결이 닫혔습니다. 웹소켓을 초기화합니다.")
                    self.ws = None
                    self.running = False
                    self.auth_successful = False
                
                return False
    
    async def unsubscribe(self, symbol: str, feed_type: str = "price"):
        """구독 취소"""
        # SKIP_WEBSOCKET이 True면 구독 취소하지 않고 구독 정보만 제거
        if self.skip_websocket:
            logger.log_system(f"{symbol} - SKIP_WEBSOCKET 설정으로 웹소켓 구독 취소를 건너뜁니다.")
            # 내부 상태 업데이트 (콜백 및 구독 정보 제거)
            if symbol in self.subscriptions:
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
            return
            
        # 웹소켓 연결 상태 확인
        if not self.is_connected():
            logger.log_system(f"웹소켓 연결이 없어 {symbol}의 {feed_type} 구독 취소를 건너뜁니다.")
            
            # 단, 내부 상태는 업데이트
            if feed_type == "price" and symbol in self.subscriptions:
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
            await self.connect()
            
            # 연결에 성공했으면 기존 구독 복원 - 실패 시 신경 쓰지 않음
            if self.is_connected() and self.auth_successful:
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
    
    async def close(self):
        """웹소켓 연결 종료"""
        self.running = False
        self.auth_successful = False
        if self.ws:
            try:
                await self.ws.close()
            except:
                pass
            self.ws = None
        logger.log_system("WebSocket connection closed")
    
    def is_connected(self) -> bool:
        """웹소켓 연결 상태 확인"""
        # SKIP_WEBSOCKET이 True면 항상 연결된 것으로 간주
        if self.skip_websocket:
            return True
        return self.ws is not None and self.running

# 싱글톤 인스턴스
ws_client = KISWebSocketClient()
