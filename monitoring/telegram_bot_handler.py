"""
텔레그램 봇 명령 처리기
외부에서 텔레그램을 통해 봇에 명령을 내릴 수 있도록 합니다.
"""
import asyncio
import logging
import re
import time
import traceback
import os
import json
import aiohttp
import requests
from typing import Dict, Any, List, Callable, Optional
from datetime import datetime, timedelta
from config.settings import config
from core.order_manager import order_manager
from core.api_client import api_client
from core.stock_explorer import stock_explorer
from strategies.scalping_strategy import scalping_strategy
from monitoring.alert_system import alert_system
from utils.logger import logger
from utils.database import db
from utils.dotenv_helper import dotenv_helper

class TelegramBotHandler:
    """텔레그램 봇 핸들러"""
    
    def __init__(self):
        # 설정에서 토큰과 채팅 ID 로드
        self.token = config["alert"].telegram_token
        self.chat_id = config["alert"].telegram_chat_id
        
        # 토큰이 기본값이거나 비어 있으면 환경 변수에서 직접 읽기 시도
        if self.token == "your_telegram_bot_token" or not self.token:
            env_token = os.getenv("TELEGRAM_TOKEN")
            if env_token:
                self.token = env_token
                logger.log_system(f"환경 변수에서 텔레그램 토큰을 로드했습니다.")
            else:
                logger.log_system(f"텔레그램 토큰이 설정되지 않았습니다. 봇 기능이 작동하지 않습니다.", level="ERROR")
        
        # 채팅 ID가 기본값이거나 비어 있으면 환경 변수에서 직접 읽기 시도
        if self.chat_id == "your_chat_id" or not self.chat_id:
            env_chat_id = os.getenv("TELEGRAM_CHAT_ID")
            if env_chat_id:
                self.chat_id = env_chat_id
                logger.log_system(f"환경 변수에서 텔레그램 채팅 ID를 로드했습니다.")
            else:
                logger.log_system(f"텔레그램 채팅 ID가 설정되지 않았습니다. 메시지를 보낼 수 없습니다.", level="ERROR")
        
        # 토큰 유효성 기본 검사
        if self.token and len(self.token) < 20:
            logger.log_system(f"텔레그램 토큰이 너무 짧아 유효하지 않습니다. 올바른 토큰인지 확인하세요.", level="ERROR")
        
        # API 기본 URL 설정
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        
        # 봇 상태 변수
        self.last_update_id = 0
        self.bot_running = False
        self.trading_paused = False
        self.ready_event = None
        
        # 종료 콜백 함수 초기화
        self.shutdown_callback = None
        
        # aiohttp 세션
        self._session = None  # aiohttp 세션 싱글톤
        
        self.commands = {
            '/status': self.get_status,
            '/buy': self.buy_stock,
            '/sell': self.sell_stock,
            '/positions': self.get_positions,
            '/balance': self.get_balance,
            '/performance': self.get_performance,
            '/scan': self.scan_symbols,
            '/stop': self.stop_bot,
            '/pause': self.pause_trading,
            '/resume': self.resume_trading,
            '/close_all': self.close_all_positions,
            '/price': self.get_price,
            '/trades': self.get_trades,
            '/help': self.get_help,
        }
        self.message_lock = asyncio.Lock()  # 메시지 전송 동시성 제어를 위한 락
        
    def set_shutdown_callback(self, callback: Callable):
        """종료 콜백 설정"""
        self.shutdown_callback = callback
        
    async def start_polling(self):
        """메시지 폴링 시작"""
        # 현재 이벤트 루프에서 사용할 ready_event 초기화
        self.ready_event = asyncio.Event()
        
        self.bot_running = True
        logger.log_system("텔레그램 봇 핸들러 폴링 시작...")

        # 세션 초기화
        if self._session is None:
            self._session = aiohttp.ClientSession()
            logger.log_system("텔레그램 봇 aiohttp 세션 초기화")
            
        # 웹훅 설정 초기화하여 충돌 방지
        try:
            logger.log_system("텔레그램 봇 웹훅 초기화 시도...")
            async with self._session.get(f"{self.base_url}/deleteWebhook") as response:
                data = await response.json()
                if data.get("ok"):
                    logger.log_system("텔레그램 봇 웹훅 초기화 성공")
                else:
                    logger.log_system(f"텔레그램 봇 웹훅 초기화 실패: {data.get('description')}", level="WARNING")
        except Exception as e:
            logger.log_error(e, "텔레그램 봇 웹훅 초기화 중 오류 발생")

        # 시작 알림 메시지
        start_message_sent = False
        try:
            logger.log_system("텔레그램 봇 핸들러 시작 알림 메시지 전송 시도...")
            await self._send_message("🤖 <b>트레이딩 봇 원격 제어 시작</b>\n\n명령어 목록을 보려면 /help를 입력하세요.")
            start_message_sent = True
            logger.log_system("텔레그램 봇 핸들러 시작 알림 메시지 전송 성공.")
        except Exception as e:
            logger.log_error(e, "텔레그램 봇 핸들러 시작 알림 메시지 전송 실패.")
        finally:
             # 메시지 전송 성공 여부와 관계없이 핸들러는 준비된 것으로 간주
             logger.log_system("텔레그램 봇 핸들러 ready_event 설정 시도...")
             self.ready_event.set()
             logger.log_system(f"텔레그램 봇 핸들러 ready_event 설정 완료 (시작 메시지 전송: {start_message_sent}).")

        logger.log_system("텔레그램 봇 폴링 루프 진입.")
        try:
            while self.bot_running:
                try:
                    updates = await self._get_updates()
                    
                    for update in updates:
                        await self._process_update(update)
                        
                    # 업데이트 간격 (1초)
                    await asyncio.sleep(1)
                except Exception as e:
                    error_msg = f"텔레그램 봇 폴링 오류: {str(e)}"
                    logger.log_error(e, error_msg)
                    await asyncio.sleep(5)  # 오류 시 잠시 대기
        finally:
            # 봇 폴링 종료 시 세션 정리
            logger.log_system("텔레그램 봇 폴링 종료, 세션 정리 시도...")
            try:
                if self._session and not self._session.closed:
                    await self._session.close()
                    self._session = None
                    logger.log_system("텔레그램 봇 aiohttp 세션 정상 종료")
            except Exception as e:
                logger.log_error(e, "텔레그램 봇 세션 종료 중 오류")

    async def _get_updates(self) -> List[Dict[str, Any]]:
        """업데이트 가져오기"""
        try:
            params = {
                "offset": self.last_update_id + 1,
                "timeout": 30
            }
            logger.log_system(f"텔레그램 업데이트 요청: offset={self.last_update_id + 1}")
            
            # requests 대신 aiohttp 사용 (비동기 환경에서 중요)
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
                logger.log_system("텔레그램 업데이트용 새 aiohttp 세션 생성")
                
            async with self._session.get(f"{self.base_url}/getUpdates", params=params, timeout=30) as response:
                if response.status != 200:
                    logger.log_system(f"텔레그램 업데이트 요청 실패: {response.status}", level="WARNING")
                    # 409 오류인 경우 (충돌), 다른 인스턴스가 이미 실행 중일 수 있음
                    # 오프셋을 재설정하여 다시 시도
                    if response.status == 409:
                        # 다른 인스턴스가 이미 실행 중인 경우 처리
                        logger.log_system("텔레그램 봇 충돌 감지, 오프셋 재설정 시도", level="WARNING")
                        # 충돌 해결을 위해 오프셋 재설정 (deleteWebhook 호출로 상태 초기화)
                        try:
                            async with self._session.get(f"{self.base_url}/deleteWebhook") as reset_response:
                                reset_data = await reset_response.json()
                                logger.log_system(f"웹훅 초기화 결과: {reset_data}")
                                
                                # 잠시 대기 후 다음 루프에서 다시 시도
                                await asyncio.sleep(2)
                        except Exception as reset_error:
                            logger.log_error(reset_error, "웹훅 초기화 중 오류 발생")
                    
                    return []
                    
                data = await response.json()
                logger.log_system(f"텔레그램 응답: {data}")
                
                if data.get("ok") and data.get("result"):
                    updates = data["result"]
                    if updates:
                        self.last_update_id = max(update["update_id"] for update in updates)
                        logger.log_system(f"새 업데이트 ID: {self.last_update_id}")
                    return updates
            return []
        except aiohttp.ClientError as e:
            logger.log_error(e, "텔레그램 업데이트 조회 중 aiohttp 오류")
            return []
        except asyncio.TimeoutError:
            logger.log_system("텔레그램 업데이트 요청 타임아웃", level="WARNING")
            return []
        except Exception as e:
            logger.log_error(e, "텔레그램 업데이트 조회 오류")
            return []
    
    async def _process_update(self, update: Dict[str, Any]):
        """업데이트 처리"""
        try:
            message = update.get("message", {})
            chat_id = message.get("chat", {}).get("id")
            message_id = message.get("message_id")
            update_id = update.get("update_id")
            text = message.get("text", "")
            
            # 메시지가 없거나 텍스트가 없는 경우 무시
            if not message or not text:
                return
            
            logger.log_system(f"텔레그램 메시지 수신: {text[:30]}... (chat_id: {chat_id}, message_id: {message_id})")
                
            # 메시지 ID가 있는 경우 이미 처리된 메시지인지 확인
            if message_id:
                # DB에서 이 메시지 ID로 저장된 메시지가 있는지 확인
                try:
                    existing_messages = db.get_telegram_messages(
                        direction="INCOMING",
                        message_id=str(message_id),
                        limit=1
                    )
                    
                    # 메시지가 이미 저장되어 있고 처리된 경우 건너뜀
                    if existing_messages and existing_messages[0].get("processed"):
                        logger.log_system(f"이미 처리된 메시지 무시: ID {message_id}", level="INFO")
                        return
                except Exception as e:
                    logger.log_error(e, f"메시지 처리 상태 확인 중 오류: {message_id}")
                    # 오류가 발생해도 계속 진행 (중복 처리보다 누락이 더 위험함)
            
            # 수신 메시지 DB에 저장
            is_command = text.startswith('/')
            command = text.split()[0].lower() if is_command else None
            
            try:
                db_message_id = db.save_telegram_message(
                    direction="INCOMING",
                    chat_id=str(chat_id) if chat_id else "unknown",
                    message_text=text,
                    message_id=str(message_id) if message_id else None,
                    update_id=update_id,
                    is_command=is_command,
                    command=command
                )
                logger.log_system(f"수신 메시지 DB 저장 성공 (DB ID: {db_message_id})")
            except Exception as e:
                logger.log_error(e, "수신 메시지 DB 저장 실패")
                # DB 저장 실패해도 메시지 처리는 계속함
            
            # 권한 확인 (설정된 chat_id와 일치해야 함)
            if not chat_id or str(chat_id) != str(self.chat_id):
                logger.log_system(f"허가되지 않은 접근 (채팅 ID: {chat_id})", level="WARNING")
                # 메시지 처리 실패 상태 업데이트
                if message_id:
                    try:
                        db.update_telegram_message_status(
                            message_id=str(message_id),
                            processed=True,
                            status="FAIL",
                            error_message="Unauthorized chat ID"
                        )
                    except Exception as e:
                        logger.log_error(e, "메시지 상태 업데이트 실패")
                return
            
            if is_command:
                await self._handle_command(text, str(chat_id), str(message_id) if message_id else None)
                
        except Exception as e:
            logger.log_error(e, f"텔레그램 업데이트 처리 오류: {update}")
            # 오류 발생 시 메시지 상태 업데이트
            message_id = update.get("message", {}).get("message_id")
            if message_id:
                try:
                    db.update_telegram_message_status(
                        message_id=str(message_id),
                        processed=True,
                        status="FAIL",
                        error_message=str(e)
                    )
                except Exception as update_error:
                    logger.log_error(update_error, "메시지 상태 업데이트 실패")
    
    async def _handle_command(self, command_text: str, chat_id: str, message_id: str = None):
        """명령어 처리"""
        parts = command_text.split()
        command = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []
        
        logger.log_system(f"텔레그램 명령 수신: {command_text} (chat_id: {chat_id}, message_id: {message_id})")
        
        handler = self.commands.get(command)
        if handler:
            try:
                # 명령 처리 전 상태 업데이트
                if message_id:
                    db.update_telegram_message_status(
                        message_id=str(message_id),
                        processed=True,
                        status="PROCESSING"
                    )
                
                logger.log_system(f"텔레그램 명령 처리 시작: {command}")
                response = await handler(args)
                
                # 명령 처리 성공 상태 업데이트
                if message_id:
                    db.update_telegram_message_status(
                        message_id=str(message_id),
                        processed=True,
                        status="SUCCESS"
                    )
                
                # 응답 전송
                if response:  # None인 경우 응답하지 않음 (이미 다른 방식으로 응답한 경우)
                    logger.log_system(f"텔레그램 명령 응답 전송: {command} (길이: {len(response)})")
                    await self._send_message(response, reply_to=message_id)
                else:
                    logger.log_system(f"텔레그램 명령에 대한 응답 없음: {command}")
            except Exception as e:
                # 스택 트레이스를 포함한 상세 오류 로깅
                error_msg = f"명령 처리 중 오류 발생: {str(e)}"
                stack_trace = traceback.format_exc()
                logger.log_error(e, f"명령어 오류 ({command}): {error_msg}\n{stack_trace}")
                
                # 명령 처리 실패 상태 업데이트
                if message_id:
                    db.update_telegram_message_status(
                        message_id=str(message_id),
                        processed=True,
                        status="FAIL",
                        error_message=str(e)
                    )
                
                # 사용자에게 보여줄 간결한 오류 메시지
                user_error_msg = f"명령 처리 중 오류가 발생했습니다: {str(e)[:100]}"
                if "object has no attribute" in str(e):
                    user_error_msg += "\n\n이 기능은 현재 개발 중이거나 사용할 수 없습니다."
                    
                # 오류 응답
                await self._send_message(f"❌ *오류 발생*\n\n{user_error_msg}", reply_to=message_id)
        else:
            # 알 수 없는 명령어
            unknown_cmd_msg = f"알 수 없는 명령어입니다: {command}\n/help를 입력하여 사용 가능한 명령어를 확인하세요."
            logger.log_system(f"알 수 없는 텔레그램 명령: {command}")
            await self._send_message(unknown_cmd_msg, reply_to=message_id)
            
            # 알 수 없는 명령어 처리 상태 업데이트
            if message_id:
                db.update_telegram_message_status(
                    message_id=str(message_id),
                    processed=True,
                    status="FAIL",
                    error_message="Unknown command"
                )
    
    async def _send_message(self, text: str, reply_to: str = None, max_retries: int = 3):
        """텔레그램 메시지 전송 내부 메서드
        
        텔레그램 API를 통해 메시지를 전송하고 DB에 로그를 저장합니다.
        중요 메시지(오류, 경고, 종료)는 봇 중단 상태에서도 전송됩니다.
        
        Args:
            text: 전송할 메시지 텍스트
            reply_to: 답장할 메시지 ID
            max_retries: 최대 재시도 횟수
        
        Returns:
            성공 시 메시지 ID, 실패 시 None
        """
        # 중요 메시지 여부 확인 (오류, 경고, 봇 종료 관련 메시지)
        is_important = any(keyword in text for keyword in [
            "❌", "⚠️", "오류", "실패", "error", "fail", "종료", "stop", "ERROR", "WARNING", "CRITICAL"
        ])
        
        # 봇이 실행 중이 아니고, 중요 메시지도 아닌 경우
        if not self.bot_running and not is_important:
            logger.log_system("봇이 종료되어 일반 메시지를 전송하지 않습니다.", level="WARNING")
            return None
        
        # 이벤트 루프가 닫혀있는지 확인
        try:
            current_loop = asyncio.get_running_loop()
            if current_loop.is_closed():
                logger.log_system("이벤트 루프가 닫혀 메시지 전송이 불가능합니다.", level="WARNING")
                # DB에 저장만 하고 전송은 하지 않음
                db_message_id = db.save_telegram_message(
                    direction="OUTGOING",
                    chat_id=self.chat_id,
                    message_text=text,
                    reply_to=reply_to,
                    status="FAIL",
                    error_message="이벤트 루프 닫힘"
                )
                return None
        except RuntimeError:
            # 이벤트 루프가 없는 상태 - 프로그램 종료 중일 가능성이 높음
            logger.log_system("이벤트 루프를 가져올 수 없습니다. 프로그램이 종료 중일 수 있습니다.", level="WARNING")
            # DB에 저장만 하고 전송은 하지 않음
            db_message_id = db.save_telegram_message(
                direction="OUTGOING",
                chat_id=self.chat_id,
                message_text=text,
                reply_to=reply_to,
                status="FAIL", 
                error_message="이벤트 루프 없음"
            )
            return None
        
        # 메시지 ID 생성 및 DB 저장
        db_message_id = db.save_telegram_message(
            direction="OUTGOING",
            chat_id=self.chat_id,
            message_text=text,
            reply_to=reply_to
        )
        
        logger.log_system(f"발신 메시지 DB 저장 완료 (ID: {db_message_id})")
        
        # 텔레그램 API 요청 준비
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        params = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"  # HTML 형식 지정
        }
        
        if reply_to:
            params["reply_to_message_id"] = reply_to
        
        # 메시지 전송 시도
        for attempt in range(1, max_retries + 1):
            try:
                logger.log_system(f"텔레그램 API 요청 시도 #{attempt}: {url}")
                
                # 세션이 없거나 닫혀있는 경우 새로 생성
                if self._session is None or self._session.closed:
                    logger.log_system("텔레그램 API 세션이 없거나 닫혀 있어 새로 생성합니다.", level="WARNING")
                    try:
                        self._session = aiohttp.ClientSession()
                    except RuntimeError as e:
                        # 이벤트 루프 관련 오류 (이벤트 루프가 닫혀있을 가능성)
                        logger.log_system(f"세션 생성 중 이벤트 루프 오류: {str(e)}", level="WARNING")
                        db.update_telegram_message(db_message_id, status="FAIL", error_message=f"세션 생성 실패: {str(e)}")
                        return None
                
                # API 요청 전송 시도
                try:
                    async with self._session.post(url, params=params, timeout=10) as response:
                        logger.log_system(f"텔레그램 API 응답 수신: {response.status}")
                        
                        # 응답 처리
                        if response.status == 200:
                            result = await response.json()
                            
                            if result.get("ok"):
                                # 성공적으로 전송된 메시지 ID 저장
                                message_id = result.get("result", {}).get("message_id")
                                
                                # DB에 메시지 ID 및 상태 업데이트
                                db.update_telegram_message(db_message_id, message_id=str(message_id), status="SUCCESS")
                                logger.log_system(f"발신 메시지 DB 상태 업데이트 완료 (Status: SUCCESS)")
                                
                                return message_id
                            else:
                                # 텔레그램 API 오류 처리
                                error_code = result.get("error_code")
                                description = result.get("description", "알 수 없는 오류")
                                
                                logger.log_system(f"텔레그램 API 오류: {error_code} - {description}", level="ERROR")
                                
                                # DB에 오류 상태 업데이트
                                db.update_telegram_message(
                                    db_message_id, 
                                    status="FAIL", 
                                    error_message=f"API 오류: {error_code} - {description}"
                                )
                                
                                # 재시도 가능한 오류인 경우 계속 시도
                                if error_code in [429, 500, 502, 503, 504] and attempt < max_retries:
                                    await asyncio.sleep(attempt * 2)  # 지수 백오프
                                    continue
                                
                                return None
                        else:
                            # HTTP 오류 처리
                            error_text = await response.text()
                            logger.log_system(f"텔레그램 API HTTP 오류: {response.status} - {error_text}", level="ERROR")
                            
                            # DB에 오류 상태 업데이트
                            db.update_telegram_message(
                                db_message_id, 
                                status="FAIL", 
                                error_message=f"HTTP 오류: {response.status} - {error_text[:100]}"
                            )
                            
                            # 재시도 가능한 HTTP 오류인 경우 계속 시도
                            if response.status in [429, 500, 502, 503, 504] and attempt < max_retries:
                                await asyncio.sleep(attempt * 2)  # 지수 백오프
                                continue
                            
                            return None
                except RuntimeError as e:
                    # 이벤트 루프 관련 오류 (이벤트 루프가 닫혀있을 가능성)
                    if "loop is closed" in str(e) or "Event loop is closed" in str(e):
                        logger.log_system("이벤트 루프가 닫혀 텔레그램 API 요청을 보낼 수 없습니다.", level="WARNING")
                        db.update_telegram_message(db_message_id, status="FAIL", error_message="이벤트 루프 닫힘")
                        return None
                    raise  # 다른 런타임 오류는 다시 발생시킴
            
            except aiohttp.ClientError as e:
                # 네트워크 관련 오류 처리
                logger.log_system(f"텔레그램 API 요청 네트워크 오류: {str(e)}", level="ERROR")
                
                # DB에 오류 상태 업데이트
                db.update_telegram_message(db_message_id, status="FAIL", error_message=f"네트워크 오류: {str(e)}")
                
                # 재시도
                if attempt < max_retries:
                    await asyncio.sleep(attempt * 2)  # 지수 백오프
                    continue
                
                return None
                
            except asyncio.TimeoutError:
                # 타임아웃 오류 처리
                logger.log_system("텔레그램 API 요청 타임아웃", level="ERROR")
                
                # DB에 오류 상태 업데이트
                db.update_telegram_message(db_message_id, status="FAIL", error_message="요청 타임아웃")
                
                # 재시도
                if attempt < max_retries:
                    await asyncio.sleep(attempt * 2)  # 지수 백오프
                    continue
                
                return None
                
            except Exception as e:
                # 기타 예외 처리
                logger.log_error(e, "텔레그램 메시지 전송 중 예외 발생")
                
                # DB에 오류 상태 업데이트
                db.update_telegram_message(db_message_id, status="FAIL", error_message=f"예외: {str(e)}")
                
                # 치명적인 오류는 재시도하지 않음
                return None
        
        # 모든 시도 실패
        logger.log_system(f"텔레그램 메시지 전송 최대 시도 횟수 초과 ({max_retries}회)", level="ERROR")
        return None
    
    async def send_message(self, text: str, reply_to: str = None):
        """외부에서 호출할 수 있는 메시지 전송 메소드"""
        # 동시 전송 방지를 위한 락 사용
        async with self.message_lock:
            try:
                return await self._send_message(text, reply_to)
            except Exception as e:
                logger.log_error(e, "텔레그램 메시지 전송 실패")
                return None
    
    async def wait_until_ready(self, timeout: Optional[float] = None):
        """봇이 준비될 때까지 대기"""
        try:
            # 이벤트 루프가 닫혀있는지 확인
            try:
                current_loop = asyncio.get_running_loop()
                if current_loop.is_closed():
                    logger.log_system("이벤트 루프가 닫혀 있어 텔레그램 봇 준비 상태를 확인할 수 없습니다.", level="WARNING")
                    return False
            except RuntimeError:
                logger.log_system("현재 실행 중인 이벤트 루프를 가져올 수 없습니다.", level="WARNING")
                return False
                
            # ready_event가 None이면 봇이 아직 시작되지 않은 것
            if self.ready_event is None:
                logger.log_system("텔레그램 봇 핸들러가 아직 시작되지 않았습니다. 자동으로 시작합니다.", level="WARNING")
                # 이벤트 초기화 및 폴링 시작
                self.ready_event = asyncio.Event()
                # 백그라운드에서 폴링 시작
                try:
                    asyncio.create_task(self.start_polling())
                except RuntimeError as e:
                    if "loop is closed" in str(e) or "Event loop is closed" in str(e):
                        logger.log_system("이벤트 루프가 닫혀 텔레그램 봇을 시작할 수 없습니다.", level="WARNING")
                        return False
                    raise
                
            # 타임아웃과 함께 대기
            if timeout is not None:
                try:
                    await asyncio.wait_for(self.ready_event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.log_system(f"텔레그램 봇 준비 시간 초과 ({timeout}초)", level="WARNING")
                    raise
            else:
                await self.ready_event.wait()
                
            return True
            
        except asyncio.TimeoutError:
            logger.log_system(f"텔레그램 봇 준비 시간 초과 ({timeout}초)", level="WARNING")
            raise
        except RuntimeError as e:
            if "loop is closed" in str(e) or "Event loop is closed" in str(e):
                logger.log_system("이벤트 루프가 닫혀 있어 텔레그램 봇 준비 상태를 확인할 수 없습니다.", level="WARNING")
                return False
            raise
        except Exception as e:
            logger.log_error(e, "텔레그램 봇 준비 대기 중 오류 발생")
            return False
    
    # 명령어 핸들러들
    async def get_status(self, args: List[str]) -> str:
        """시스템 상태 조회"""
        status = db.get_system_status()
        
        # 시간 형식 개선 - 날짜 확인 및 현재 시간 사용
        updated_at = status['updated_at']
        try:
            # 시간 문자열 파싱 및 포매팅
            if updated_at:
                from datetime import datetime
                # DB에서 가져온 시간이 이상한 경우, 현재 시간으로 대체
                try:
                    dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                    # 날짜가 미래이거나 2025년 이전인 경우 현재 시간으로 대체
                    now = datetime.now()
                    if dt.year < 2025 or dt > now:
                        dt = now
                    updated_at = dt.strftime("%Y-%m-%d %H:%M:%S")
                except ValueError:
                    # 시간 형식이 잘못된 경우 현재 시간으로 대체
                    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            else:
                # updated_at이 없는 경우 현재 시간 사용
                updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            # 파싱 에러 발생 시 현재 시간 사용
            from datetime import datetime
            updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.log_error(e, "시스템 상태 시간 파싱 실패")
        
        # 상태에 따른 이모지 결정
        status_emoji = "✅" if status['status'] == "RUNNING" else "⚠️" if status['status'] == "PAUSED" else "❌"
        
        # 현재 시간 표시 추가
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 거래 건수와 포지션 가져오기
        today_orders_count = len(await order_manager.get_today_orders())
        positions_count = len(await order_manager.get_positions())
        
        return f"""<b>📊 시스템 상태</b>

상태: {status_emoji} <b>{status['status']}</b>
마지막 업데이트: {updated_at}
현재 시간: {current_time}
거래 일시정지: {'⚠️ 활성화' if self.trading_paused else '✅ 비활성화'}

<b>성능 요약</b>
금일 거래: {today_orders_count}건
보유 포지션: {positions_count}개"""
    
    async def get_help(self, args: List[str]) -> str:
        """도움말"""
        return """<b>📝 사용 가능한 명령어</b>

<b>조회 명령어</b>
/status - 시스템 상태 조회
/positions - 보유 종목 조회
/balance - 계좌 잔고 조회
/performance - 성과 조회
/price - 종목 현재가 조회
/trades - 최근 거래 내역 조회 (예시: /trades 005930 10)

<b>거래 명령어</b>
/buy - 종목 매수 (예시: /buy 005930 10)
/sell - 종목 매도 (예시: /sell 005930 10)
/close_all - 모든 포지션 청산
/scan - 종목 탐색 실행

<b>제어 명령어</b>
/pause - 자동 거래 일시정지
/resume - 자동 거래 재개
/stop - 프로그램 종료
/help - 도움말"""
    
    async def buy_stock(self, args: List[str]) -> str:
        """종목 매수"""
        if len(args) < 2:
            return "사용법: /buy 종목코드 수량"
        
        symbol = args[0]
        try:
            quantity = int(args[1])
        except ValueError:
            return "수량은 숫자여야 합니다."
        
        if self.trading_paused:
            return "⚠️ 거래가 일시정지되었습니다. /resume으로 재개하세요."
        
        # 현재가 조회
        stock_info = await stock_explorer.get_symbol_info(symbol)
        if not stock_info:
            return f"❌ {symbol} 종목 정보를 조회할 수 없습니다."
        
        price = stock_info.get("current_price", 0)
        if price <= 0:
            return f"❌ {symbol} 종목의 가격을 조회할 수 없습니다."
        
        result = await order_manager.place_order(
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            price=price,
            order_type="MARKET",
            reason="user_request_telegram"  # 사용자 요청으로 인한 주문임을 명시
        )
        
        if result and result.get("status") == "success":
            order_no = result.get("order_id", "알 수 없음")
            trade_data = result.get("trade_data", {})
            total_amount = price * quantity
            
            # 기존 포지션 정보 가져오기
            positions = await order_manager.get_positions()
            position = next((p for p in positions if p["symbol"] == symbol), None)
            avg_price = position["avg_price"] if position else price
            total_quantity = position["quantity"] if position else quantity
            
            return f"""
<b>💰 매수 주문 성공</b>
종목: {symbol} ({stock_info.get('name', symbol)})
수량: {quantity}주
체결가: {price:,}원
총액: {total_amount:,}원
주문번호: {order_no}

<b>포지션 정보</b>
평균단가: {avg_price:,}원
총보유수량: {total_quantity}주
예상수수료: {trade_data.get('commission', total_amount * 0.0005):,.0f}원
            """
        else:
            error = result.get("reason", "알 수 없는 오류") if result else "API 호출 실패"
            return f"❌ <b>매수 주문 실패</b>\n{symbol} 매수 실패: {error}"
    
    async def sell_stock(self, args: List[str]) -> str:
        """종목 매도"""
        if len(args) < 2:
            return "사용법: /sell 종목코드 수량"
        
        symbol = args[0]
        try:
            quantity = int(args[1])
        except ValueError:
            return "수량은 숫자여야 합니다."
        
        if self.trading_paused:
            return "⚠️ 거래가 일시정지되었습니다. /resume으로 재개하세요."
            
        # 보유 수량 확인
        positions = await order_manager.get_positions()
        position = next((p for p in positions if p["symbol"] == symbol), None)
        
        if not position:
            return f"❌ {symbol} 종목을 보유하고 있지 않습니다."
            
        if position["quantity"] < quantity:
            return f"❌ 보유 수량({position['quantity']}주)보다 많은 수량({quantity}주)을 매도할 수 없습니다."
        
        # 현재가 조회
        stock_info = await stock_explorer.get_symbol_info(symbol)
        if not stock_info:
            return f"❌ {symbol} 종목 정보를 조회할 수 없습니다."
        
        price = stock_info.get("current_price", 0)
        
        result = await order_manager.place_order(
            symbol=symbol,
            side="SELL",
            quantity=quantity,
            price=price,
            order_type="MARKET",
            reason="user_request_telegram"  # 사용자 요청으로 인한 주문임을 명시
        )
        
        if result and result.get("status") == "success":
            order_no = result.get("order_id", "알 수 없음")
            trade_data = result.get("trade_data", {})
            total_amount = price * quantity
            
            # 매도 후 포지션 정보 가져오기
            positions = await order_manager.get_positions()
            position = next((p for p in positions if p["symbol"] == symbol), None)
            remaining = position["quantity"] if position else 0
            
            # 손익 계산
            avg_buy_price = position["avg_price"] if position else 0
            pnl = trade_data.get("pnl", (price - avg_buy_price) * quantity)
            pnl_percent = ((price / avg_buy_price) - 1) * 100 if avg_buy_price > 0 else 0
            
            # 이모지 결정
            emoji = "🔴" if pnl < 0 else "🟢"
            
            return f"""
<b>💰 매도 주문 성공</b>
종목: {symbol} ({stock_info.get('name', symbol)})
수량: {quantity}주
체결가: {price:,}원
총액: {total_amount:,}원
주문번호: {order_no}

<b>거래 결과</b>
손익: {emoji} {pnl:,.0f}원 ({pnl_percent:.2f}%)
남은수량: {remaining}주
예상수수료: {trade_data.get('commission', total_amount * 0.0005):,.0f}원
            """
        else:
            error = result.get("reason", "알 수 없는 오류") if result else "API 호출 실패"
            return f"❌ <b>매도 주문 실패</b>\n{symbol} 매도 실패: {error}"
    
    async def get_positions(self, args: List[str]) -> str:
        """보유 종목 조회"""
        positions = await order_manager.get_positions()
        
        if not positions:
            return "현재 보유 중인 종목이 없습니다."
        
        result = "<b>현재 보유 종목</b>\n\n"
        
        total_value = 0
        for pos in positions:
            symbol = pos["symbol"]
            quantity = pos["quantity"]
            
            # 종목 정보 조회
            stock_info = await stock_explorer.get_symbol_info(symbol)
            name = stock_info.get("name", symbol) if stock_info else symbol
            current_price = stock_info.get("current_price", 0) if stock_info else 0
            
            # 매수 금액 및 현재 평가 금액
            avg_price = pos.get("avg_price", 0)
            buy_amount = avg_price * quantity
            eval_amount = current_price * quantity
            
            # 손익
            pnl = eval_amount - buy_amount
            pnl_pct = (pnl / buy_amount) * 100 if buy_amount > 0 else 0
            
            # 이모지 결정
            emoji = "🔴" if pnl < 0 else "🟢"
            
            result += f"{emoji} <b>{name}</b> ({symbol})\n"
            result += f"   수량: {quantity}주\n"
            result += f"   평균단가: {avg_price:,.0f}원\n"
            result += f"   현재가: {current_price:,.0f}원\n"
            result += f"   손익: {pnl:,.0f}원 ({pnl_pct:.2f}%)\n"
            result += f"   평가금액: {eval_amount:,.0f}원\n\n"
            
            total_value += eval_amount
        
        result += f"<b>총 평가금액: {total_value:,.0f}원</b>"
        return result
    
    async def get_balance(self, args: List[str]) -> str:
        """계좌 잔고 조회"""
        try:
            balance_data = await order_manager.get_account_balance()
            
            # 데이터 형식 로깅하여 확인
            logger.log_system(f"계좌 잔고 데이터 형식: {type(balance_data)}, 데이터: {balance_data}")
            
            if not balance_data:
                return "❌ 계좌 잔고를 조회할 수 없습니다."
            
            # 리스트인 경우 처리
            if isinstance(balance_data, list):
                if not balance_data:
                    return "❌ 계좌 잔고 정보가 비어 있습니다."
                    
                # 첫 번째 항목을 사용
                first_item = balance_data[0]
                if isinstance(first_item, dict):
                    total_balance = float(first_item.get("tot_evlu_amt", "0"))
                    deposit = float(first_item.get("dnca_tot_amt", "0"))
                    stock_value = float(first_item.get("scts_evlu_amt", "0"))
                    available = float(first_item.get("nass_amt", "0"))
                else:
                    return f"❌ 계좌 잔고 데이터 형식이 예상과 다릅니다: {first_item}"
            # 딕셔너리인 경우 처리
            elif isinstance(balance_data, dict):
                # output1이 비어있거나 리스트인 경우 output2 확인
                output1 = balance_data.get("output1", {})
                output2 = balance_data.get("output2", [])
                
                if (not output1 or isinstance(output1, list) and not output1) and output2 and isinstance(output2, list) and len(output2) > 0:
                    # output2의 첫 번째 항목 사용
                    first_item = output2[0]
                    if isinstance(first_item, dict):
                        total_balance = float(first_item.get("tot_evlu_amt", "0"))
                        deposit = float(first_item.get("dnca_tot_amt", "0"))
                        stock_value = float(first_item.get("scts_evlu_amt", "0"))
                        available = float(first_item.get("nass_amt", "0"))
                    else:
                        return f"❌ 계좌 잔고 데이터 형식이 예상과 다릅니다: {first_item}"
                else:
                    # 기존 코드 - output1에서 시도
                    if isinstance(output1, dict):
                        total_balance = float(output1.get("tot_evlu_amt", "0"))
                        deposit = float(output1.get("dnca_tot_amt", "0"))
                        stock_value = float(output1.get("scts_evlu_amt", "0"))
                        available = float(output1.get("nass_amt", "0"))
                    else:
                        return f"❌ 계좌 잔고 데이터 형식이 예상과 다릅니다: {output1}"
            else:
                return f"❌ 계좌 잔고 데이터 형식이 예상과 다릅니다: {type(balance_data)}"
            
            return f"""
<b>💵 계좌 잔고 정보</b>

총 평가금액: {total_balance:,.0f}원
예수금: {deposit:,.0f}원
주식 평가금액: {stock_value:,.0f}원
매수 가능금액: {available:,.0f}원
"""
        except Exception as e:
            logger.log_error(e, "계좌 잔고 조회 중 오류 발생")
            return f"❌ 계좌 잔고 조회 중 오류 발생: {str(e)}"
    
    async def get_performance(self, args: List[str]) -> str:
        """성과 조회"""
        today_orders = await order_manager.get_today_orders()
        
        if not today_orders:
            return "오늘의 거래 내역이 없습니다."
        
        buy_orders = [o for o in today_orders if o["side"] == "BUY"]
        sell_orders = [o for o in today_orders if o["side"] == "SELL"]
        
        total_buy = sum(o["price"] * o["quantity"] for o in buy_orders)
        total_sell = sum(o["price"] * o["quantity"] for o in sell_orders)
        
        # 간단한 손익 계산 (정확한 계산은 아님)
        realized_pnl = total_sell - total_buy
        
        result = f"""
<b>📈 오늘의 거래 성과</b>

총 거래: {len(today_orders)}건
- 매수: {len(buy_orders)}건 (₩{total_buy:,.0f})
- 매도: {len(sell_orders)}건 (₩{total_sell:,.0f})

잠정 손익: {realized_pnl:,.0f}원
"""
        
        # 개별 종목 성과 계산
        symbols = set([o["symbol"] for o in today_orders])
        
        if symbols:
            result += "\n<b>종목별 거래</b>\n"
            
            for symbol in symbols:
                symbol_orders = [o for o in today_orders if o["symbol"] == symbol]
                symbol_buys = [o for o in symbol_orders if o["side"] == "BUY"]
                symbol_sells = [o for o in symbol_orders if o["side"] == "SELL"]
                
                symbol_buy_amount = sum(o["price"] * o["quantity"] for o in symbol_buys)
                symbol_sell_amount = sum(o["price"] * o["quantity"] for o in symbol_sells)
                
                symbol_pnl = symbol_sell_amount - symbol_buy_amount
                pnl_emoji = "🔴" if symbol_pnl < 0 else "🟢"
                
                result += f"{pnl_emoji} {symbol}: {symbol_pnl:,.0f}원\n"
        
        return result
    
    async def scan_symbols(self, args: List[str]) -> str:
        """종목 탐색 수동 실행"""
        market_type = args[0].upper() if args and args[0].upper() in ["KOSPI", "KOSDAQ", "ALL"] else "ALL"
        
        await self._send_message(f"🔍 {market_type} 시장 종목 스캔을 시작합니다. 이 작업은 시간이 걸릴 수 있습니다...")
        
        try:
            symbols = await stock_explorer.get_tradable_symbols(market_type=market_type)
            
            if not symbols:
                return "❌ 거래 가능한 종목을 찾을 수 없습니다."
            
            # 전략 업데이트
            if hasattr(scalping_strategy, 'update_symbols'):
                await scalping_strategy.update_symbols(symbols[:50])
            
            result = f"✅ *종목 스캔 완료*\n\n{len(symbols)}개의 종목을 찾았습니다.\n\n"
            
            # 상위 10개 종목 표시
            result += "*상위 10개 종목*\n"
            
            for i, symbol in enumerate(symbols[:10], 1):
                stock_info = await stock_explorer.get_symbol_info(symbol)
                name = stock_info.get("name", symbol) if stock_info else symbol
                result += f"{i}. {name} ({symbol})\n"
            
            return result
            
        except Exception as e:
            logger.log_error(e, "종목 스캔 오류")
            return f"❌ 종목 스캔 중 오류 발생: {str(e)}"
    
    async def stop_bot(self, args: List[str]) -> str:
        """프로그램 종료"""
        # 확인 요청
        if not args or not args[0] == "confirm":
            return "⚠️ 정말로 트레이딩 봇을 종료하시겠습니까? 확인하려면 <code>/stop confirm</code>을 입력하세요."
        
        await self._send_message("🛑 <b>트레이딩 봇을 종료합니다...</b>")
        
        # 콜백이 없어도 자체적으로 종료 처리
        if self.shutdown_callback is None:
            logger.log_system("종료 콜백이 설정되지 않았습니다. 직접 종료 처리를 시도합니다.", level="WARNING")
            # 직접 종료 처리 시도
            asyncio.create_task(self._direct_shutdown())
        else:
            # 비동기로 종료 처리
            asyncio.create_task(self._shutdown_bot())
        
        return None  # 이미 메시지를 보냈으므로 추가 메시지 필요 없음
        
    async def _direct_shutdown(self):
        """콜백 없이 직접 종료 처리"""
        # 필요한 모듈 임포트
        import os
        import sys
        
        logger.log_system("직접 종료 처리 시작", level="INFO")
        # 잠시 대기 후 종료 (메시지 전송 시간 확보)
        await asyncio.sleep(2)
        self.bot_running = False
        
        # 세션 정리 (새 메시지 전송 방지)
        try:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
                logger.log_system("텔레그램 세션 정상 종료", level="INFO")
        except Exception as e:
            logger.log_error(e, "텔레그램 세션 종료 중 오류")
        
        # DB에 상태 업데이트
        try:
            db.update_system_status("STOPPED", "텔레그램 명령으로 시스템 종료됨")
            logger.log_system("시스템 상태를 '종료됨'으로 업데이트", level="INFO")
        except Exception as e:
            logger.log_error(e, "상태 업데이트 중 오류")
        
        # 프로그램 강제 종료 - 백엔드까지 종료
        logger.log_system("텔레그램 명령으로 프로그램을 종료합니다", level="INFO")
        
        # 2초 후 프로그램 종료
        await asyncio.sleep(2)
        
        # 먼저 Windows taskkill 명령을 실행하여 모든 관련 프로세스 종료 시도
        try:
            # Windows 환경인 경우
            if os.name == 'nt':
                logger.log_system("Windows taskkill 명령으로 관련 프로세스 종료 시도", level="INFO")
                # 백엔드 프로세스 종료 시도 (main.py)
                os.system('taskkill /f /im python.exe /fi "COMMANDLINE eq *main.py*"')
                # 다른 trading_bot 관련 프로세스 종료 시도
                os.system('taskkill /f /im python.exe /fi "COMMANDLINE eq *trading_bot*"')
                # 모든 Python 프로세스 종료 시도 (위험할 수 있으므로 마지막 수단)
                os.system('taskkill /f /im python.exe /fi "USERNAME eq %USERNAME%"')
                logger.log_system("taskkill 명령 실행 완료", level="INFO")
        except Exception as e:
            logger.log_error(e, "taskkill 명령 실행 중 오류")
        
        # Python 프로세스 종료 시도 (psutil 사용)
        try:
            # psutil을 이용해 백엔드 프로세스도 함께 종료
            import psutil
            current_pid = os.getpid()
            logger.log_system(f"현재 프로세스 PID: {current_pid}", level="INFO")
            current_process = psutil.Process(current_pid)
            
            # 현재 프로세스의 부모 찾기 (main.py 프로세스일 수 있음)
            try:
                parent = current_process.parent()
                logger.log_system(f"부모 프로세스: {parent.name()} (PID: {parent.pid})", level="INFO")
                
                # 부모가 python 프로세스인 경우 종료
                if "python" in parent.name().lower():
                    logger.log_system(f"부모 Python 프로세스 (PID: {parent.pid}) 종료 시도", level="INFO")
                    try:
                        parent.terminate()  # 부모 프로세스 종료 시도
                        gone, still_alive = psutil.wait_procs([parent], timeout=3)
                        if still_alive:
                            logger.log_system(f"부모 프로세스가 종료되지 않아 강제 종료합니다", level="WARNING")
                            parent.kill()  # 강제 종료
                    except psutil.NoSuchProcess:
                        logger.log_system("부모 프로세스가 이미 종료됨", level="INFO")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.log_system(f"부모 프로세스 접근 오류: {str(e)}", level="WARNING")
            
            # 모든 Python 프로세스 검색
            logger.log_system("관련 Python 프로세스 검색 시작", level="INFO")
            python_processes = []
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # 자기 자신은 제외
                    if proc.info['pid'] == current_pid:
                        continue
                        
                    # Python 프로세스 검색
                    proc_name = proc.info['name'].lower()
                    if "python" in proc_name or "pythonw" in proc_name:
                        cmd = proc.cmdline()
                        cmd_str = " ".join(cmd)
                        
                        # main.py, start_fixed_system.bat, 또는 trading_bot 관련 프로세스 검색
                        if any(target in cmd_str for target in ['main.py', 'trading_bot', 'start_fixed_system']):
                            python_processes.append(proc)
                            logger.log_system(f"종료 대상 프로세스 발견: PID {proc.info['pid']}, CMD: {cmd_str}", level="INFO")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            
            # 발견된 프로세스 종료
            if python_processes:
                logger.log_system(f"{len(python_processes)}개의 관련 프로세스 종료 시도", level="INFO")
                for proc in python_processes:
                    try:
                        proc.terminate()
                        logger.log_system(f"프로세스 PID {proc.pid} 종료 요청 완료", level="INFO")
                    except psutil.NoSuchProcess:
                        logger.log_system(f"프로세스 PID {proc.pid}가 이미 종료됨", level="INFO")
                    except Exception as e:
                        logger.log_error(e, f"프로세스 PID {proc.pid} 종료 중 오류")
                
                # 프로세스가 종료될 때까지 기다림
                logger.log_system("프로세스 종료 대기", level="INFO")
                _, still_alive = psutil.wait_procs(python_processes, timeout=5)
                
                # 여전히 살아있는 프로세스 강제 종료
                if still_alive:
                    logger.log_system(f"{len(still_alive)}개 프로세스가 여전히 실행 중, 강제 종료 시도", level="WARNING")
                    for proc in still_alive:
                        try:
                            proc.kill()  # 강제 종료
                            logger.log_system(f"프로세스 PID {proc.pid} 강제 종료 요청", level="WARNING")
                        except Exception as e:
                            logger.log_error(e, f"프로세스 PID {proc.pid} 강제 종료 중 오류")
            else:
                logger.log_system("종료할 관련 프로세스를 찾지 못했습니다", level="WARNING")
            
            # 1초 대기 후 현재 프로세스 종료
            await asyncio.sleep(1)
        except ImportError:
            logger.log_system("psutil 모듈이 설치되지 않아 프로세스 검색이 불가능합니다.", level="WARNING")
        except Exception as e:
            logger.log_error(e, "프로세스 종료 중 오류 발생")
        
        # 시스템 종료 명령 - 가장 강력한 방법 시도
        try:
            # 강제 종료 전에 로그 메시지
            logger.log_system("강제 종료 전 파이썬 종료 코드 시도", level="INFO")
            
            # 시스템 종료 시도 - 다양한 방법
            try:
                # 방법 1: sys.exit
                sys.exit(0)
            except Exception as e1:
                logger.log_system(f"sys.exit 실패: {str(e1)}", level="WARNING")
                
                # 방법 2: os._exit
                try:
                    logger.log_system("os._exit(0)를 통해 프로세스 강제 종료 시도", level="INFO")
                    os._exit(0)  # 강제 종료
                except Exception as e2:
                    logger.log_error(e2, "os._exit 실패")
        except Exception as e:
            logger.log_error(e, "시스템 종료 실패")
            
            # 최후의 수단
            os._exit(1)
            
    async def _shutdown_bot(self):
        """봇 종료 처리 (콜백 사용)"""
        logger.log_system("텔레그램 봇 종료 시작", level="INFO")
        
        # 잠시 대기 후 종료 (메시지 전송 시간 확보)
        await asyncio.sleep(2)
        
        # 봇 종료 상태 설정 및 세션 정리
        self.bot_running = False
        
        # 세션 정리 (새 메시지 전송 방지)
        try:
            if self._session and not self._session.closed:
                await self._session.close()
                self._session = None
                logger.log_system("텔레그램 세션 정상 종료", level="INFO")
        except Exception as e:
            logger.log_error(e, "텔레그램 세션 종료 중 오류")
        
        # 종료 콜백 실행
        if self.shutdown_callback:
            logger.log_system("시스템 종료 콜백 함수 실행", level="INFO")
            try:
                await self.shutdown_callback()
                logger.log_system("시스템 종료 콜백 함수 실행 완료", level="INFO")
            except Exception as e:
                logger.log_error(e, "시스템 종료 콜백 함수 실행 중 오류 발생")
                # 콜백 실행 실패 시 직접 종료 시도
                logger.log_system("콜백 실행 실패로 직접 종료 시도", level="WARNING")
                await self._direct_shutdown()
        else:
            logger.log_system("종료 콜백이 설정되지 않았습니다. 직접 종료 처리를 시도합니다.", level="WARNING")
            await self._direct_shutdown()
    
    async def pause_trading(self, args: List[str]) -> str:
        """거래 일시 중지"""
        self.trading_paused = True
        
        # 전략 일시 중지
        try:
            await scalping_strategy.pause()
            logger.log_system("스캘핑 전략을 성공적으로 일시 중지했습니다.")
        except Exception as e:
            logger.log_error(e, "스캘핑 전략 일시 중지 중 오류 발생")
            
        # order_manager에도 거래 일시 중지 상태 설정
        order_manager.pause_trading()
            
        db.update_system_status("PAUSED", "텔레그램 명령으로 거래 일시 중지됨")
        return "⚠️ <b>거래가 일시 중지되었습니다.</b>\n\n자동 매매가 중지되었지만, 수동 매매는 가능합니다.\n거래를 재개하려면 <code>/resume</code>을 입력하세요."
    
    async def resume_trading(self, args: List[str]) -> str:
        """거래 재개"""
        self.trading_paused = False
        
        # 전략 재개
        try:
            await scalping_strategy.resume()
            logger.log_system("스캘핑 전략을 성공적으로 재개했습니다.")
        except Exception as e:
            logger.log_error(e, "스캘핑 전략 재개 중 오류 발생")
            
        # order_manager에도 거래 재개 상태 설정
        order_manager.resume_trading()
            
        db.update_system_status("RUNNING", "텔레그램 명령으로 거래 재개됨")
        return "✅ <b>거래가 재개되었습니다.</b>"
    
    async def close_all_positions(self, args: List[str]) -> str:
        """모든 포지션 청산"""
        # 확인 요청
        if not args or not args[0] == "confirm":
            return "⚠️ 정말로 모든 포지션을 청산하시겠습니까? 확인하려면 <code>/close_all confirm</code>을 입력하세요."
            
        positions = await order_manager.get_positions()
        
        if not positions:
            return "현재 보유 중인 종목이 없습니다."
        
        success_count = 0
        failed_symbols = []
        
        await self._send_message(f"🔄 {len(positions)}개 종목의 포지션을 청산하는 중...")
        
        for pos in positions:
            symbol = pos["symbol"]
            quantity = pos["quantity"]
            
            # 현재가 조회
            stock_info = await stock_explorer.get_symbol_info(symbol)
            price = stock_info.get("current_price", 0) if stock_info else 0
            
            result = await order_manager.place_order(
                symbol=symbol,
                side="SELL",
                quantity=quantity,
                price=price,
                order_type="MARKET",
                reason="user_request_closeall"  # 사용자 요청으로 인한 주문임을 명시
            )
            
            if result and result.get("status") == "success":
                success_count += 1
            else:
                failed_symbols.append(symbol)
            
            # API 호출 간 약간의 간격을 둠
            await asyncio.sleep(0.5)
        
        if failed_symbols:
            return f"""
⚠️ <b>포지션 청산 일부 완료</b>
성공: {success_count}/{len(positions)}개 종목
실패 종목: {', '.join(failed_symbols)}
            """
        else:
            return f"✅ <b>모든 포지션 청산 완료</b>\n{success_count}개 종목이 청산되었습니다."
    
    async def get_price(self, args: List[str]) -> str:
        """종목 현재가 조회"""
        if not args:
            return "사용법: /price 종목코드"
            
        symbol = args[0]
        stock_info = await stock_explorer.get_symbol_info(symbol)
        
        if not stock_info:
            return f"❌ {symbol} 종목 정보를 찾을 수 없습니다."
            
        current_price = stock_info.get("current_price", 0)
        prev_close = stock_info.get("prev_close", 0)
        change_rate = stock_info.get("change_rate", 0)
        volume = stock_info.get("volume", 0)
        
        # 상승/하락 이모지
        emoji = "🔴" if change_rate < 0 else "🟢" if change_rate > 0 else "⚪"
        
        return f"""
<b>💹 {stock_info.get('name', symbol)} ({symbol})</b>

현재가: {current_price:,.0f}원 {emoji}
전일대비: {change_rate:.2f}%
거래량: {volume:,}주
"""

    async def get_trades(self, args: List[str]) -> str:
        """최근 거래 내역 조회"""
        # 파라미터 처리
        symbol = None
        limit = 5  # 기본값
        
        if args and len(args) > 0:
            if len(args) >= 1 and args[0].isdigit():
                limit = min(int(args[0]), 20)  # 최대 20개까지 제한
            else:
                symbol = args[0]
                if len(args) >= 2 and args[1].isdigit():
                    limit = min(int(args[1]), 20)
        
        # 현재 날짜 기준으로 최근 거래내역 조회
        today = datetime.now().strftime('%Y-%m-%d')
        one_week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
        
        trades = db.get_trades(symbol=symbol, start_date=one_week_ago, end_date=f"{today} 23:59:59")
        
        if not trades:
            return f"<b>📊 최근 거래 내역</b>\n\n최근 거래 내역이 없습니다." + (f"\n종목: {symbol}" if symbol else "")
        
        # 최신순으로 정렬하여 limit 개수만큼만 표시
        trades = sorted(trades, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]
        
        trades_text = ""
        for trade in trades:
            trade_time = trade.get("created_at", "").split(" ")[1][:5]  # HH:MM 형식
            trade_date = trade.get("created_at", "").split(" ")[0]
            side = trade.get("side", "")
            symbol = trade.get("symbol", "")
            price = trade.get("price", 0)
            quantity = trade.get("quantity", 0)
            pnl = trade.get("pnl", 0)
            
            # 매수/매도에 따른 이모지 및 색상
            emoji = "🟢" if side == "BUY" else "🔴"
            
            # 손익 표시 (매도의 경우)
            pnl_text = ""
            if side == "SELL" and pnl is not None:
                pnl_emoji = "🔴" if pnl < 0 else "🟢"
                pnl_text = f" ({pnl_emoji} {pnl:,.0f}원)"
            
            # 한 거래에 대한 텍스트 생성
            trade_info = f"{emoji} {trade_date} {trade_time} | {symbol} | {side} | {price:,.0f}원 x {quantity}주{pnl_text}\n"
            trades_text += trade_info
        
        # 요약 정보 계산
        buy_count = sum(1 for t in trades if t.get("side") == "BUY")
        sell_count = sum(1 for t in trades if t.get("side") == "SELL")
        total_pnl = sum(t.get("pnl", 0) or 0 for t in trades if t.get("side") == "SELL")
        
        summary = f"매수: {buy_count}건, 매도: {sell_count}건, 손익: {total_pnl:,.0f}원"
        
        return f"""<b>📊 최근 거래 내역</b>{f' ({symbol})' if symbol else ''}

{trades_text}
<b>요약</b>: {summary}

{f'종목코드 {symbol}의 ' if symbol else ''}최근 {len(trades)}건 표시 (최대 {limit}건)"""

    async def close_session(self):
        """세션 명시적 종료 메소드 - 프로그램 종료 전 호출용"""
        logger.log_system("텔레그램 봇 세션 명시적 종료 요청 받음...")
        try:
            if self._session and not self._session.closed:
                logger.log_system("텔레그램 봇 세션 닫기 시도...")
                await self._session.close()
                self._session = None
                logger.log_system("텔레그램 봇 세션 명시적으로 닫힘")
            else:
                logger.log_system("텔레그램 봇 세션이 이미 닫혀있거나 없음")
            
            # 봇 상태 업데이트
            self.bot_running = False
            return True
        except Exception as e:
            logger.log_error(e, "텔레그램 봇 세션 종료 중 오류")
            return False

# 싱글톤 인스턴스
telegram_bot_handler = TelegramBotHandler() 