"""
한국투자증권 웹소켓 클라이언트
"""
import json
import asyncio
import websockets
from typing import Dict, Any, Callable, List, Optional
from datetime import datetime
from config.settings import config
from utils.logger import logger

class KISWebSocketClient:
    """한국투자증권 웹소켓 클라이언트"""
    
    def __init__(self):
        self.config = config["api"]
        self.ws_url = self.config.ws_url
        self.app_key = self.config.app_key
        self.app_secret = self.config.app_secret
        self.ws = None
        self.running = False
        self.subscriptions = {}
        self.callbacks = {}
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5
        self.reconnect_delay = 5  # seconds
        
    async def connect(self):
        """웹소켓 연결"""
        try:
            self.ws = await websockets.connect(self.ws_url)
            self.running = True
            self.reconnect_attempts = 0
            logger.log_system("WebSocket connected successfully")
            
            # 인증
            await self._authenticate()
            
            # 메시지 수신 루프 시작
            asyncio.create_task(self._receive_messages())
            
        except Exception as e:
            logger.log_error(e, "WebSocket connection failed")
            await self._handle_reconnect()
    
    async def _authenticate(self):
        """웹소켓 인증"""
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
        
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=body, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    return data["approval_key"]
                else:
                    raise Exception(f"Failed to get approval key: {response.status}")
    
    async def subscribe_price(self, symbol: str, callback: Callable):
        """실시간 체결가 구독"""
        tr_id = "H0STCNT0"  # 실시간 체결가
        tr_key = symbol
        
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
        
        await self.ws.send(json.dumps(subscribe_data))
        
        # 콜백 등록
        self.callbacks[f"{tr_id}|{tr_key}"] = callback
        self.subscriptions[symbol] = tr_id
        
        logger.log_system(f"Subscribed to price feed for {symbol}")
    
    async def subscribe_orderbook(self, symbol: str, callback: Callable):
        """실시간 호가 구독"""
        tr_id = "H0STASP0"  # 실시간 호가
        tr_key = symbol
        
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
        
        await self.ws.send(json.dumps(subscribe_data))
        
        # 콜백 등록
        self.callbacks[f"{tr_id}|{tr_key}"] = callback
        self.subscriptions[f"{symbol}_orderbook"] = tr_id
        
        logger.log_system(f"Subscribed to orderbook feed for {symbol}")
    
    async def unsubscribe(self, symbol: str, feed_type: str = "price"):
        """구독 취소"""
        if feed_type == "price":
            tr_id = self.subscriptions.get(symbol)
        else:
            tr_id = self.subscriptions.get(f"{symbol}_{feed_type}")
        
        if not tr_id:
            return
        
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
    
    async def _receive_messages(self):
        """메시지 수신 루프"""
        try:
            while self.running:
                message = await self.ws.recv()
                await self._process_message(message)
                
        except websockets.exceptions.ConnectionClosed:
            logger.log_system("WebSocket connection closed")
            await self._handle_reconnect()
            
        except Exception as e:
            logger.log_error(e, "Error in message receive loop")
            await self._handle_reconnect()
    
    async def _process_message(self, message: str):
        """메시지 처리"""
        try:
            data = json.loads(message)
            
            # 시스템 메시지 처리
            if data.get("header", {}).get("tr_id") == "PINGPONG":
                await self._handle_ping()
                return
            
            # 실시간 데이터 처리
            header = data.get("header", {})
            body = data.get("body", {})
            
            tr_id = header.get("tr_id")
            tr_key = body.get("tr_key")
            
            callback_key = f"{tr_id}|{tr_key}"
            callback = self.callbacks.get(callback_key)
            
            if callback:
                await callback(body)
            
        except Exception as e:
            logger.log_error(e, f"Error processing message: {message}")
    
    async def _handle_ping(self):
        """핑퐁 처리"""
        pong_data = {
            "header": {
                "tr_id": "PINGPONG",
                "datetime": datetime.now().strftime("%Y%m%d%H%M%S")
            }
        }
        await self.ws.send(json.dumps(pong_data))
    
    async def _handle_reconnect(self):
        """재연결 처리"""
        if self.reconnect_attempts >= self.max_reconnect_attempts:
            logger.log_error(
                Exception("Max reconnection attempts reached"),
                "WebSocket reconnection failed"
            )
            self.running = False
            return
        
        self.reconnect_attempts += 1
        logger.log_system(
            f"Attempting to reconnect ({self.reconnect_attempts}/{self.max_reconnect_attempts})"
        )
        
        await asyncio.sleep(self.reconnect_delay)
        await self.connect()
        
        # 기존 구독 복원
        for symbol, tr_id in self.subscriptions.items():
            if "_" in symbol:  # orderbook 등
                symbol_name, feed_type = symbol.rsplit("_", 1)
                if feed_type == "orderbook":
                    await self.subscribe_orderbook(
                        symbol_name, 
                        self.callbacks.get(f"{tr_id}|{symbol_name}")
                    )
            else:  # price
                await self.subscribe_price(
                    symbol, 
                    self.callbacks.get(f"{tr_id}|{symbol}")
                )
    
    async def close(self):
        """웹소켓 연결 종료"""
        self.running = False
        if self.ws:
            await self.ws.close()
        logger.log_system("WebSocket connection closed")
    
    def is_connected(self) -> bool:
        """연결 상태 확인"""
        return self.ws is not None and self.ws.open

# 싱글톤 인스턴스
ws_client = KISWebSocketClient()
