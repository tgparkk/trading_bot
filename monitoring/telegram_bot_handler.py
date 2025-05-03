"""
텔레그램 봇 명령 처리기
외부에서 텔레그램을 통해 봇에 명령을 내릴 수 있도록 합니다.
"""
import asyncio
import logging
import requests
import aiohttp
import traceback
from typing import Dict, Any, List, Callable, Optional
from datetime import datetime
from config.settings import config
from core.order_manager import order_manager
from core.stock_explorer import stock_explorer
from strategies.scalping_strategy import scalping_strategy
from utils.logger import logger
from utils.database import db
from utils.dotenv_helper import dotenv_helper

class TelegramBotHandler:
    """텔레그램 봇 핸들러"""
    
    def __init__(self):
        # 설정에서 토큰과 채팅 ID 로드
        self.token = config["alert"].telegram_token
        self.chat_id = config["alert"].telegram_chat_id
        
        # 하드코딩된 기본값이거나 비어있는 경우 환경 변수에서 직접 로드
        if self.token == "your_telegram_bot_token" or not self.token:
            logger.log_system("텔레그램 토큰이 기본값이거나 설정되지 않았습니다. 환경 변수에서 로드합니다.", level="WARNING")
            env_token = dotenv_helper.get_value("TELEGRAM_TOKEN")
            if env_token:
                self.token = env_token
                logger.log_system(f"환경변수에서 텔레그램 토큰을 로드했습니다: {self.token[:10]}...")
        
        if self.chat_id == "your_chat_id" or not self.chat_id:
            logger.log_system("텔레그램 채팅 ID가 기본값이거나 설정되지 않았습니다. 환경 변수에서 로드합니다.", level="WARNING")
            env_chat_id = dotenv_helper.get_value("TELEGRAM_CHAT_ID")
            if env_chat_id:
                self.chat_id = env_chat_id
                logger.log_system(f"환경변수에서 텔레그램 채팅 ID를 로드했습니다: {self.chat_id}")
        
        # 로그 남기기
        logger.log_system(f"텔레그램 봇 설정 - 토큰: {self.token[:10]}..., 채팅 ID: {self.chat_id}")
        
        # 텔레그램 API 기본 URL 설정
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        
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
            '/help': self.get_help,
        }
        self.last_update_id = 0
        self.bot_running = False
        self.trading_paused = False
        self.shutdown_callback = None
        self.ready_event = None  # 초기화 시점에는 None으로 설정
        self.message_lock = asyncio.Lock()  # 메시지 전송 동시성 제어를 위한 락
        self._session = None  # aiohttp 세션 싱글톤
        
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
            await self._send_message("🤖 *트레이딩 봇 원격 제어 시작*\n\n명령어 목록을 보려면 /help를 입력하세요.")
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
                logger.log_error(e, "텔레그램 봇 세션 종료 중 오류 발생")

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
            
            # 수신 메시지 DB에 저장
            is_command = text.startswith('/')
            command = text.split()[0].lower() if is_command else None
            
            db.save_telegram_message(
                direction="INCOMING",
                chat_id=chat_id,
                message_text=text,
                message_id=str(message_id) if message_id else None,
                update_id=update_id,
                is_command=is_command,
                command=command
            )
            
            # 권한 확인 (설정된 chat_id와 일치해야 함)
            if str(chat_id) != str(self.chat_id):
                logger.log_system(f"허가되지 않은 접근 (채팅 ID: {chat_id})", level="WARNING")
                # 메시지 처리 실패 상태 업데이트
                if message_id:
                    db.update_telegram_message_status(
                        message_id=str(message_id),
                        processed=True,
                        status="FAIL",
                        error_message="Unauthorized chat ID"
                    )
                return
            
            if is_command:
                await self._handle_command(text, chat_id, message_id)
                
        except Exception as e:
            logger.log_error(e, f"텔레그램 업데이트 처리 오류: {update}")
            # 오류 발생 시 메시지 상태 업데이트
            message_id = update.get("message", {}).get("message_id")
            if message_id:
                db.update_telegram_message_status(
                    message_id=str(message_id),
                    processed=True,
                    status="FAIL",
                    error_message=str(e)
                )
    
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
        """내부 메시지 전송 (재시도 로직 포함)"""
        # 실제 메시지 전송 구현
        message_id = None
        error_message = None
        status = "FAIL"
        db_message_id = None
        
        params = {
            "chat_id": self.chat_id,
            "text": text
        }
        
        if reply_to:
            params["reply_to_message_id"] = reply_to
        
        # DB에 메시지 저장 시도 (실패해도 계속 진행)
        try:
            db_message_id = db.save_telegram_message(
                direction="OUTGOING",
                chat_id=self.chat_id,
                message_text=text,
                reply_to=reply_to
            )
            logger.log_system(f"발신 메시지 DB 저장 완료 (ID: {db_message_id})")
        except Exception as e:
            logger.log_system(f"발신 메시지 DB 저장 실패: {str(e)}", level="WARNING")
            # DB 저장 실패해도 메시지는 계속 전송 시도
            
        # 메시지 전송 시도
        for attempt in range(max_retries):
            try:
                logger.log_system(f"텔레그램 API 요청 시도 #{attempt+1}: {self.base_url}/sendMessage")
                
                # 세션이 없으면 생성
                if self._session is None or self._session.closed:
                    self._session = aiohttp.ClientSession()
                
                async with self._session.post(f"{self.base_url}/sendMessage", json=params, timeout=10) as response:
                    response_data = await response.json()
                    logger.log_system(f"텔레그램 API 응답 수신: {response.status}")
                    
                    if response.status == 200 and response_data.get("ok"):
                        message_id = response_data.get("result", {}).get("message_id")
                        status = "SUCCESS"
                        break
                    else:
                        error_message = response_data.get("description", f"HTTP 오류: {response.status}")
                        logger.log_system(f"텔레그램 메시지 전송 실패 (시도 #{attempt+1}): {error_message}", level="WARNING")
                        
                        # API 토큰 오류인 경우 더 이상 시도하지 않음
                        if "unauthorized" in error_message.lower() or "forbidden" in error_message.lower():
                            break
                        
                        await asyncio.sleep(1)  # 잠시 대기 후 재시도
            except asyncio.TimeoutError as e:
                error_message = f"요청 시간 초과: {str(e)}"
                logger.log_system(f"텔레그램 메시지 전송 타임아웃: {error_message}", level="WARNING")
                await asyncio.sleep(1)
            except aiohttp.ClientError as e:
                error_message = f"HTTP 클라이언트 오류: {str(e)}"
                logger.log_system(f"텔레그램 메시지 전송 중 aiohttp 오류: {error_message}", level="WARNING")
                
                # 세션이 손상된 경우 재생성
                try:
                    if self._session and not self._session.closed:
                        await self._session.close()
                    self._session = aiohttp.ClientSession()
                except Exception as se:
                    logger.log_system(f"세션 재생성 중 오류: {str(se)}", level="WARNING")
                
                await asyncio.sleep(1)
            except Exception as e:
                error_message = f"일반 오류: {str(e)}"
                logger.log_error(e, "텔레그램 메시지 전송 중 일반 오류")
                await asyncio.sleep(1)
        
        # DB에 전송 결과 업데이트 (실패해도 무시)
        if db_message_id:
            try:
                db.update_telegram_message(
                    db_message_id=db_message_id,
                    message_id=message_id,
                    status=status,
                    error_message=error_message
                )
                logger.log_system(f"발신 메시지 DB 저장 완료 (Status: {status})")
            except Exception as e:
                logger.log_system(f"발신 메시지 상태 업데이트 실패: {str(e)}", level="WARNING")
        
        return message_id
    
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
            # ready_event가 None이면 봇이 아직 시작되지 않은 것
            if self.ready_event is None:
                logger.log_system("텔레그램 봇 핸들러가 아직 시작되지 않았습니다. 자동으로 시작합니다.", level="WARNING")
                # 이벤트 초기화 및 폴링 시작
                self.ready_event = asyncio.Event()
                # 백그라운드에서 폴링 시작
                asyncio.create_task(self.start_polling())
                
            # 타임아웃과 함께 대기
            if timeout is not None:
                await asyncio.wait_for(self.ready_event.wait(), timeout=timeout)
            else:
                await self.ready_event.wait()
                
            return True
            
        except asyncio.TimeoutError:
            logger.log_system(f"텔레그램 봇 준비 시간 초과 ({timeout}초)", level="WARNING")
            raise
    
    # 명령어 핸들러들
    async def get_status(self, args: List[str]) -> str:
        """시스템 상태 조회"""
        status = db.get_system_status()
        return f"""📊 시스템 상태

상태: {status['status']}
마지막 업데이트: {status['updated_at']}
거래 일시정지: {'활성화 ⚠️' if self.trading_paused else '비활성화 ✅'}

성능 요약
금일 거래: {len(await order_manager.get_today_orders())}건
보유 포지션: {len(await order_manager.get_positions())}개"""
    
    async def get_help(self, args: List[str]) -> str:
        """도움말"""
        return """📝 사용 가능한 명령어

조회 명령어
/status - 시스템 상태 조회
/positions - 보유 종목 조회
/balance - 계좌 잔고 조회
/performance - 성과 조회
/price - 종목 현재가 조회

거래 명령어
/buy - 종목 매수
/sell - 종목 매도
/close_all - 모든 포지션 청산
/scan - 종목 탐색 실행

제어 명령어
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
            order_type="MARKET"
        )
        
        if result and result.get("rt_cd") == "0":
            order_no = result.get("output", {}).get("ODNO", "알 수 없음")
            total_amount = price * quantity
            return f"""
💰 *매수 주문 성공*
종목: {symbol} ({stock_info.get('name', symbol)})
수량: {quantity}주
예상 가격: {price:,}원
예상 총액: {total_amount:,}원
주문번호: {order_no}
            """
        else:
            error = result.get("msg1", "알 수 없는 오류") if result else "API 호출 실패"
            return f"❌ *매수 주문 실패*\n{symbol} 매수 실패: {error}"
    
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
            order_type="MARKET"
        )
        
        if result and result.get("rt_cd") == "0":
            order_no = result.get("output", {}).get("ODNO", "알 수 없음")
            total_amount = price * quantity
            return f"""
💰 *매도 주문 성공*
종목: {symbol} ({stock_info.get('name', symbol)})
수량: {quantity}주
예상 가격: {price:,}원
예상 총액: {total_amount:,}원
주문번호: {order_no}
            """
        else:
            error = result.get("msg1", "알 수 없는 오류") if result else "API 호출 실패"
            return f"❌ *매도 주문 실패*\n{symbol} 매도 실패: {error}"
    
    async def get_positions(self, args: List[str]) -> str:
        """보유 종목 조회"""
        positions = await order_manager.get_positions()
        
        if not positions:
            return "현재 보유 중인 종목이 없습니다."
        
        result = "*현재 보유 종목*\n\n"
        
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
            
            result += f"{emoji} *{name}* ({symbol})\n"
            result += f"   수량: {quantity}주\n"
            result += f"   평균단가: {avg_price:,.0f}원\n"
            result += f"   현재가: {current_price:,.0f}원\n"
            result += f"   손익: {pnl:,.0f}원 ({pnl_pct:.2f}%)\n"
            result += f"   평가금액: {eval_amount:,.0f}원\n\n"
            
            total_value += eval_amount
        
        result += f"*총 평가금액: {total_value:,.0f}원*"
        return result
    
    async def get_balance(self, args: List[str]) -> str:
        """계좌 잔고 조회"""
        balance_data = await order_manager.get_account_balance()
        
        if not balance_data:
            return "❌ 계좌 잔고를 조회할 수 없습니다."
        
        total_balance = float(balance_data.get("output1", {}).get("tot_evlu_amt", "0"))
        deposit = float(balance_data.get("output1", {}).get("dnca_tot_amt", "0"))
        stock_value = float(balance_data.get("output1", {}).get("scts_evlu_amt", "0"))
        available = float(balance_data.get("output1", {}).get("nass_amt", "0"))
        
        return f"""
💵 *계좌 잔고 정보*

총 평가금액: {total_balance:,.0f}원
예수금: {deposit:,.0f}원
주식 평가금액: {stock_value:,.0f}원
매수 가능금액: {available:,.0f}원
"""
    
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
📈 *오늘의 거래 성과*

총 거래: {len(today_orders)}건
- 매수: {len(buy_orders)}건 (₩{total_buy:,.0f})
- 매도: {len(sell_orders)}건 (₩{total_sell:,.0f})

잠정 손익: {realized_pnl:,.0f}원
"""
        
        # 개별 종목 성과 계산
        symbols = set([o["symbol"] for o in today_orders])
        
        if symbols:
            result += "\n*종목별 거래*\n"
            
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
        if self.shutdown_callback is None:
            return "❌ 종료 콜백이 설정되지 않았습니다."
            
        # 확인 요청
        if not args or not args[0] == "confirm":
            return "⚠️ 정말로 트레이딩 봇을 종료하시겠습니까? 확인하려면 `/stop confirm`을 입력하세요."
            
        await self._send_message("🛑 *트레이딩 봇을 종료합니다...*")
        
        # 비동기로 종료 처리
        asyncio.create_task(self._shutdown_bot())
        return None  # 이미 메시지를 보냈으므로 추가 메시지 필요 없음
    
    async def _shutdown_bot(self):
        """봇 종료 처리"""
        # 잠시 대기 후 종료 (메시지 전송 시간 확보)
        await asyncio.sleep(2)
        self.bot_running = False
        
        # 종료 콜백 실행
        if self.shutdown_callback:
            await self.shutdown_callback()
    
    async def pause_trading(self, args: List[str]) -> str:
        """거래 일시 중지"""
        self.trading_paused = True
        
        # 전략 일시 중지
        if hasattr(scalping_strategy, 'pause'):
            await scalping_strategy.pause()
            
        db.update_system_status("PAUSED", "텔레그램 명령으로 거래 일시 중지됨")
        return "⚠️ *거래가 일시 중지되었습니다.*\n\n자동 매매가 중지되었지만, 수동 매매는 가능합니다.\n거래를 재개하려면 `/resume`을 입력하세요."
    
    async def resume_trading(self, args: List[str]) -> str:
        """거래 재개"""
        self.trading_paused = False
        
        # 전략 재개
        if hasattr(scalping_strategy, 'resume'):
            await scalping_strategy.resume()
            
        db.update_system_status("RUNNING", "텔레그램 명령으로 거래 재개됨")
        return "✅ *거래가 재개되었습니다.*"
    
    async def close_all_positions(self, args: List[str]) -> str:
        """모든 포지션 청산"""
        # 확인 요청
        if not args or not args[0] == "confirm":
            return "⚠️ 정말로 모든 포지션을 청산하시겠습니까? 확인하려면 `/close_all confirm`을 입력하세요."
            
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
                order_type="MARKET"
            )
            
            if result and result.get("rt_cd") == "0":
                success_count += 1
            else:
                failed_symbols.append(symbol)
            
            # API 호출 간 약간의 간격을 둠
            await asyncio.sleep(0.5)
        
        if failed_symbols:
            return f"""
⚠️ *포지션 청산 일부 완료*
성공: {success_count}/{len(positions)}개 종목
실패 종목: {', '.join(failed_symbols)}
            """
        else:
            return f"✅ *모든 포지션 청산 완료*\n{success_count}개 종목이 청산되었습니다."
    
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
💹 *{stock_info.get('name', symbol)} ({symbol})*

현재가: {current_price:,.0f}원 {emoji}
전일대비: {change_rate:.2f}%
거래량: {volume:,}주
"""

# 싱글톤 인스턴스
telegram_bot_handler = TelegramBotHandler() 