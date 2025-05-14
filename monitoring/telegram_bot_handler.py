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
from core.api_client import KISAPIClient
from core.stock_explorer import stock_explorer
from strategies.combined_strategy import combined_strategy
from monitoring.alert_system import alert_system
from utils.logger import logger
from utils.database import database_manager
from utils.dotenv_helper import dotenv_helper

# API 클라이언트 인스턴스 생성
api_client = KISAPIClient()

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
        
        # ready_event는 나중에 초기화
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
    
    def safe_float(self, value, default=0.0):
        """문자열을 실수로 안전하게 변환"""
        try:
            return float(value) if value else default
        except (ValueError, TypeError):
            return default
    
    def set_shutdown_callback(self, callback: Callable):
        """종료 콜백 설정"""
        self.shutdown_callback = callback
        
    async def _send_message(self, text: str, reply_to_message_id: Optional[int] = None) -> Optional[int]:
        """텔레그램으로 메시지 전송"""
        if not self.token or not self.chat_id:
            logger.log_system("텔레그램 토큰 또는 채팅 ID가 설정되지 않았습니다. 메시지를 보낼 수 없습니다.", level="ERROR")
            return None
        
        # 메시지 전송 URL
        url = f"{self.base_url}/sendMessage"
        
        # 메시지 데이터
        data = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        
        # 답장 ID 추가 (있는 경우)
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        
        # 동시성 제어를 위한 락 사용
        async with self.message_lock:
            try:
                if self._session is None or self._session.closed:
                    logger.log_system("세션이 종료되어 있어 새 세션을 생성합니다.", level="WARNING")
                    self._session = aiohttp.ClientSession()
                
                async with self._session.post(url, json=data) as response:
                    result = await response.json()
                    
                    if result.get("ok"):
                        message_id = result.get("result", {}).get("message_id")
                        return message_id
                    else:
                        error_code = result.get("error_code")
                        description = result.get("description")
                        logger.log_system(f"텔레그램 메시지 전송 실패: {error_code} - {description}", level="ERROR")
                        return None
            except Exception as e:
                logger.log_error(e, "텔레그램 메시지 전송 중 오류 발생")
                return None
    
    async def _get_updates(self) -> List[Dict]:
        """텔레그램에서 새로운 메시지 업데이트 받기"""
        if not self.token:
            logger.log_system("텔레그램 토큰이 설정되지 않았습니다. 업데이트를 받을 수 없습니다.", level="ERROR")
            return []
        
        # 업데이트 URL
        url = f"{self.base_url}/getUpdates"
        
        # 파라미터
        params = {
            "offset": self.last_update_id + 1 if self.last_update_id > 0 else 0,
            "timeout": 5,  # 롱 폴링 타임아웃 (초)
            "allowed_updates": json.dumps(["message"])  # 메시지 업데이트만 허용
        }
        
        try:
            if self._session is None or self._session.closed:
                logger.log_system("세션이 종료되어 있어 새 세션을 생성합니다.", level="WARNING")
                self._session = aiohttp.ClientSession()
                
            async with self._session.get(url, params=params) as response:
                result = await response.json()
                
                if result.get("ok"):
                    updates = result.get("result", [])
                    
                    # 마지막 업데이트 ID 저장
                    if updates:
                        self.last_update_id = max(update.get("update_id", 0) for update in updates)
                    
                    return updates
                else:
                    error_code = result.get("error_code")
                    description = result.get("description")
                    logger.log_system(f"텔레그램 업데이트 조회 실패: {error_code} - {description}", level="ERROR")
                    return []
        except Exception as e:
            logger.log_error(e, "텔레그램 업데이트 조회 중 오류 발생")
            return []
    
    async def _process_update(self, update: Dict) -> None:
        """텔레그램 업데이트 처리"""
        try:
            # 메시지 추출
            message = update.get("message", {})
            if not message:
                return
            
            # 메시지 정보 추출
            chat_id = message.get("chat", {}).get("id")
            message_id = message.get("message_id")
            text = message.get("text", "")
            
            # 권한 확인
            if str(chat_id) != str(self.chat_id):
                logger.log_system(f"권한 없는 사용자 (Chat ID: {chat_id})로부터 메시지 수신: {text}", level="WARNING")
                return
            
            # 비어 있거나 명령어가 아닌 경우 무시
            if not text or not text.startswith("/"):
                return
            
            # 명령어 및 인자 추출
            parts = text.split()
            command = parts[0].lower()  # 명령어 (소문자로 변환)
            args = parts[1:] if len(parts) > 1 else []  # 인자 목록
            
            # 명령어 로깅
            logger.log_system(f"텔레그램 명령어 수신: {command} {args}", level="INFO")
            
            # 명령어 처리 메서드 가져오기
            handler = self.commands.get(command)
            
            if handler:
                try:
                    # 명령어 처리 및 응답
                    response = await handler(args)
                    if response:
                        await self._send_message(response, reply_to_message_id=message_id)
                except Exception as e:
                    logger.log_error(e, f"텔레그램 명령어 처리 중 오류: {command}")
                    error_response = f"❌ <b>명령어 처리 중 오류 발생</b>\n\n{str(e)}"
                    await self._send_message(error_response, reply_to_message_id=message_id)
            else:
                # 알 수 없는 명령어
                unknown_cmd_response = f"❌ 알 수 없는 명령어: {command}\n\n도움말을 보려면 /help를 입력하세요."
                await self._send_message(unknown_cmd_response, reply_to_message_id=message_id)
        except Exception as e:
            logger.log_error(e, "텔레그램 업데이트 처리 중 오류 발생")
        
    async def start_polling(self):
        """메시지 폴링 시작"""
        # 현재 이벤트 루프에서 사용할 ready_event 초기화
        # 반드시 현재 실행 중인 이벤트 루프에 맞는 Event 객체 생성
        logger.log_system("텔레그램 봇 핸들러 ready_event 생성 시작...")
        loop = asyncio.get_running_loop()
        self.ready_event = asyncio.Event()
        logger.log_system(f"텔레그램 봇 핸들러 ready_event 생성 완료(loop id: {id(loop)})")
        
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
        
        # DB에 상태 업데이트
        try:
            database_manager.update_system_status("STOPPED", "텔레그램 명령으로 시스템 종료됨")
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
            await combined_strategy.pause()
            logger.log_system("통합 전략을 성공적으로 일시 중지했습니다.")
        except Exception as e:
            logger.log_error(e, "통합 전략 일시 중지 중 오류 발생")
            
        # order_manager에도 거래 일시 중지 상태 설정
        order_manager.pause_trading()
            
        database_manager.update_system_status("PAUSED", "텔레그램 명령으로 거래 일시 중지됨")
        return "⚠️ <b>거래가 일시 중지되었습니다.</b>\n\n자동 매매가 중지되었지만, 수동 매매는 가능합니다.\n거래를 재개하려면 <code>/resume</code>을 입력하세요."
    
    async def resume_trading(self, args: List[str]) -> str:
        """거래 재개"""
        self.trading_paused = False
        
        # 전략 재개
        try:
            await combined_strategy.resume()
            logger.log_system("통합 전략을 성공적으로 재개했습니다.")
        except Exception as e:
            logger.log_error(e, "통합 전략 재개 중 오류 발생")
            
        # order_manager에도 거래 재개 상태 설정
        order_manager.resume_trading()
            
        database_manager.update_system_status("RUNNING", "텔레그램 명령으로 거래 재개됨")
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
            
        current_price = self.safe_float(stock_info.get("current_price", 0), 0)
        prev_close = self.safe_float(stock_info.get("prev_close", 0), 0)
        change_rate = self.safe_float(stock_info.get("change_rate", 0), 0)
        volume = self.safe_float(stock_info.get("volume", 0), 0)
        
        # 상승/하락 이모지
        emoji = "🔴" if change_rate < 0 else "🟢" if change_rate > 0 else "⚪"
        
        return f"""
<b>💹 {stock_info.get('name', symbol)} ({symbol})</b>

현재가: {current_price:,.0f}원 {emoji}
전일대비: {change_rate:.2f}%
거래량: {int(volume):,}주
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
        
        trades = database_manager.get_trades(symbol=symbol, start_date=one_week_ago, end_date=f"{today} 23:59:59")
        
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
            price = self.safe_float(trade.get("price", 0), 0)
            quantity = self.safe_float(trade.get("quantity", 0), 0)
            pnl = self.safe_float(trade.get("pnl", 0), 0)
            
            # 매수/매도에 따른 이모지 및 색상
            emoji = "🟢" if side == "BUY" else "🔴"
            
            # 손익 표시 (매도의 경우)
            pnl_text = ""
            if side == "SELL" and pnl is not None:
                pnl_emoji = "🔴" if pnl < 0 else "🟢"
                pnl_text = f" ({pnl_emoji} {pnl:,.0f}원)"
            
            # 한 거래에 대한 텍스트 생성
            trade_info = f"{emoji} {trade_date} {trade_time} | {symbol} | {side} | {price:,.0f}원 x {int(quantity)}주{pnl_text}\n"
            trades_text += trade_info
        
        # 요약 정보 계산
        buy_count = sum(1 for t in trades if t.get("side") == "BUY")
        sell_count = sum(1 for t in trades if t.get("side") == "SELL")
        total_pnl = sum(self.safe_float(t.get("pnl", 0), 0) for t in trades if t.get("side") == "SELL")
        
        summary = f"매수: {buy_count}건, 매도: {sell_count}건, 손익: {total_pnl:,.0f}원"
        
        return f"""<b>📊 최근 거래 내역</b>{f' ({symbol})' if symbol else ''}

{trades_text}
<b>요약</b>: {summary}

{f'종목코드 {symbol}의 ' if symbol else ''}최근 {len(trades)}건 표시 (최대 {limit}건)"""

    async def get_balance(self, args: List[str]) -> str:
        """계좌 잔고 조회"""
        try:
            # 실제 API 호출 시도
            account_info = await api_client.get_account_info()
            
            # API 응답 결과 로깅 (디버깅용)
            logger.log_system(f"API 계좌 응답 코드: {account_info.get('rt_cd', 'N/A')}")
            logger.log_system(f"API 계좌 응답 메시지: {account_info.get('msg1', 'N/A')}")
            
            # output, output1, output2 모두 확인
            output_data = account_info.get("output", {})
            
            # output이 없으면 output1 확인
            if not output_data and "output1" in account_info:
                output_data = account_info.get("output1", {})
            
            # output2가 있는지 확인 (예수금 정보는 여기에 있을 수 있음)
            output2_data = None
            if "output2" in account_info and isinstance(account_info["output2"], list) and account_info["output2"]:
                output2_data = account_info["output2"][0]  # 첫 번째 항목 사용
                logger.log_system(f"output2 데이터 발견: {output2_data}")
            
            # 주요 계좌 데이터 로깅
            #if output_data:
            #    logger.log_system(f"API 계좌 정보 output 키: {list(output_data.keys() if output_data else [])}")
            #    logger.log_system(f"dnca_tot_amt: {output_data.get('dnca_tot_amt', 'N/A')}")
            #    logger.log_system(f"tot_evlu_amt: {output_data.get('tot_evlu_amt', 'N/A')}")
            
            if output2_data:
                logger.log_system(f"output2 dnca_tot_amt: {output2_data.get('dnca_tot_amt', 'N/A')}")
            
            # API 응답 성공 시 데이터 추출 및 형식화
            if account_info.get("rt_cd") == "0":
                # 데이터 준비
                main_data = output_data if output_data else {}
                cash_data = output2_data if output2_data else {}
                
                if not main_data and not cash_data:
                    # "조회할 내용이 없습니다" 메시지일 때 기본 응답
                    if account_info.get("msg1") == "조회할 내용이 없습니다":
                        return "💰 계좌 정보 (실시간)\n\n계좌에 잔고나 주식이 없습니다.\n총 평가금액: 0원\n예수금: 0원"
                    return "💰 계좌 정보 (실시간)\n\n계좌 정보가 없습니다."
                
                # 계좌 잔고 정보 추출 (output 또는 output2에서)
                deposit = self.safe_float(cash_data.get("dnca_tot_amt"))  # 예수금
                total_assets = self.safe_float(cash_data.get("tot_evlu_amt", 0))  # 총 평가금액
                securities = self.safe_float(cash_data.get("scts_evlu_amt", 0))  # 유가증권 평가금액
                #today_profit = self.safe_float(cash_data.get("thdt_evlu_pfls_amt", 0))  # 금일 평가손익
                #total_profit = cash_data.get("evlu_pfls_smtl_amt", 0)  # 평가손익 합계금액
                
                # 출금 가능 금액 (실시간으로 중요한 정보)
                # withdrawable_amount = self.safe_float(main_data.get("psbl_wtdrw_amt") or cash_data.get("prvs_rcdl_excc_amt", 0))
                
                # 총 평가금액 정보가 없는 경우 예수금을 총 평가금액으로 설정
                if total_assets == 0:
                    total_assets = deposit
                
                # 형식화된 메시지 생성
                message = (
                    f"💰 계좌 정보 (실시간)\n\n"
                    f"총 평가금액: {total_assets:,.0f}원\n"
                )
                
                if securities > 0:
                    message += f"유가증권: {securities:,.0f}원\n"
                
                message += f"예수금: {deposit:,.0f}원\n"
                
                #if withdrawable_amount > 0:
                #    message += f"출금가능금액: {withdrawable_amount:,.0f}원\n"
                
                #if today_profit != 0:
                #    message += f"금일 손익: {today_profit:,.0f}원\n"
                
                #if total_profit != 0:
                #    message += f"총 손익: {total_profit:,.0f}원"
                
                # 오늘 수익률 계산 시도
                #try:
                    #if securities > 0 and today_profit != 0:
                    #    today_profit_rate = today_profit / (securities - today_profit) * 100
                    #    message += f"\n금일 수익률: {today_profit_rate:.2f}%"
                    
                    #if securities > 0 and total_profit != 0:
                    #    total_profit_rate = total_profit / (securities - total_profit) * 100
                    #    message += f"\n총 수익률: {total_profit_rate:.2f}%"
                #except Exception as e:
                #    logger.log_system(f"수익률 계산 중 오류: {str(e)}")
                
                return message
            else:
                # API 요청 실패 시 에러 메시지 반환
                error_message = account_info.get("msg1", "알 수 없는 오류") if account_info else "API 응답 없음"
                logger.log_system(f"계좌 정보 조회 실패: {error_message}", level="ERROR")
                return f"❌ 계좌 정보 조회 실패: {error_message}\n상세 내용은 로그를 확인하세요."
        
        except Exception as e:
            # 예외 발생 시 에러 메시지 반환
            error_detail = traceback.format_exc()
            logger.log_error(e, f"계좌 잔고 조회 중 예외 발생: {error_detail}")
            return f"❌ 계좌 정보 조회 중 오류 발생: {str(e)}"
    
    async def get_performance(self, args: List[str]) -> str:
        """성과 지표 조회"""
        try:
            # 기간 설정 (기본: 7일)
            days = 7
            if args and args[0].isdigit():
                days = min(int(args[0]), 30)  # 최대 30일
            
            # 시작 날짜 계산
            start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            end_date = datetime.now().strftime('%Y-%m-%d')
            
            # 거래 내역 조회
            trades = database_manager.get_trades(start_date=start_date, end_date=f"{end_date} 23:59:59")
            
            if not trades:
                return f"📊 <b>성과 지표</b>\n\n최근 {days}일간 거래 내역이 없습니다."
            
            # 통계 계산
            total_trades = len(trades)
            buy_trades = sum(1 for t in trades if t.get("side") == "BUY")
            sell_trades = sum(1 for t in trades if t.get("side") == "SELL")
            
            # 수익/손실 계산
            total_pnl = sum(self.safe_float(t.get("pnl", 0), 0) for t in trades if t.get("side") == "SELL")
            
            # 승률 계산
            win_trades = sum(1 for t in trades if t.get("side") == "SELL" and self.safe_float(t.get("pnl", 0), 0) > 0)
            loss_trades = sum(1 for t in trades if t.get("side") == "SELL" and self.safe_float(t.get("pnl", 0), 0) < 0)
            
            win_rate = win_trades / sell_trades * 100 if sell_trades > 0 else 0
            
            # 일평균 거래 횟수
            avg_trades_per_day = total_trades / days
            
            # 평균 수익/손실
            avg_pnl = total_pnl / sell_trades if sell_trades > 0 else 0
            
            # 수익/손실 이모지
            pnl_emoji = "🔴" if total_pnl < 0 else "🟢" if total_pnl > 0 else "⚪"
            
            return f"""📊 <b>성과 지표 (최근 {days}일)</b>

<b>총 거래 횟수:</b> {total_trades}회 (매수: {buy_trades}, 매도: {sell_trades})
<b>일평균 거래:</b> {avg_trades_per_day:.1f}회

<b>총 손익:</b> {pnl_emoji} {total_pnl:,.0f}원
<b>평균 손익:</b> {pnl_emoji} {avg_pnl:,.0f}원/거래

<b>승률:</b> {win_rate:.1f}% ({win_trades}/{sell_trades})
<b>기간:</b> {start_date} ~ {end_date}
"""
        except Exception as e:
            logger.log_error(e, "성과 지표 조회 오류")
            return f"❌ <b>성과 지표 조회 중 오류 발생</b>\n{str(e)}"
    
    async def get_help(self, args: List[str]) -> str:
        """도움말"""
        return """🤖 <b>트레이딩 봇 명령어 도움말</b>

<b>기본 명령어:</b>
/status - 시스템 상태 확인
/scan [KOSPI|KOSDAQ] - 거래 가능 종목 스캔

<b>계좌 및 거래:</b>
/balance - 계좌 잔고 조회
/positions - 보유 종목 조회
/trades [숫자] - 최근 거래 내역 조회 (기본값: 5개)
/performance [일수] - 성과 지표 조회 (기본값: 7일)

<b>매매 명령어:</b>
/buy 종목코드 수량 [가격] - 종목 매수
/sell 종목코드 수량 [가격] - 종목 매도
/price 종목코드 - 종목 현재가 조회
/close_all confirm - 모든 포지션 청산 (confirm 필수)

<b>시스템 제어:</b>
/pause - 자동 거래 일시 중지
/resume - 자동 거래 재개
/stop - 프로그램 종료
"""

    async def scan_symbols(self, args: List[str]) -> str:
        """거래 가능 종목 스캔
        사용법: /scan [KOSPI|KOSDAQ]
        """
        # 스캔 시작 메시지 전송
        await self._send_message("🔍 종목 스캔 중... 잠시만 기다려주세요.")
        
        try:
            # 인자 처리 (KOSPI/KOSDAQ)
            market = None
            if args and args[0].upper() in ["KOSPI", "KOSDAQ"]:
                market = args[0].upper()
                
            # 종목 스캔 실행
            logger.log_system(f"텔레그램 명령(/scan)으로 종목 스캔 시작 - 시장: {market or '전체'}")
            
            # 토큰 유효성 확인 - 비동기 메소드 사용
            is_valid = await api_client.is_token_valid(min_hours=1.0)
            if is_valid:
                logger.log_system("텔레그램 스캔 명령어: 기존 토큰이 유효합니다. 토큰 재발급 없이 진행합니다.")
            else:
                logger.log_system("텔레그램 스캔 명령어: 토큰이 없거나 곧 만료됩니다. 토큰 발급을 진행합니다.")
                # 토큰 발급 요청
                await api_client.ensure_token()
            
            # stock_explorer를 통해 종목 스캔
            top_volume_symbols = await stock_explorer.get_top_volume_stocks(market=market, limit=20)
            
            if not top_volume_symbols:
                return "❌ <b>종목 스캔 실패</b>\n\n거래량 상위 종목을 찾을 수 없습니다."
            
            # 스캔 결과 포맷팅
            symbols_text = ""
            for idx, symbol_info in enumerate(top_volume_symbols, 1):
                symbol = symbol_info.get("symbol", "N/A")
                name = symbol_info.get("name", "N/A")
                volume = self.safe_float(symbol_info.get("volume", 0), 0)
                price = self.safe_float(symbol_info.get("current_price", 0), 0)
                change_rate = self.safe_float(symbol_info.get("change_rate", 0), 0)
                
                # 상승/하락 이모지
                emoji = "🔴" if change_rate < 0 else "🟢" if change_rate > 0 else "⚪"
                
                # 한 종목에 대한 텍스트
                symbols_text += f"{idx}. <b>{name}</b> ({symbol}) - {emoji} {change_rate:.2f}%\n"
                symbols_text += f"   가격: {price:,.0f}원 | 거래량: {int(volume):,}주\n\n"
            
            # 결과 메시지 구성 
            result = f"""📊 <b>거래량 상위 종목 스캔 결과</b>{f' ({market})' if market else ''}

{symbols_text}
<b>거래 방법</b>: /buy 종목코드 수량 [가격]

{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 기준
"""
            
            # 스캔 종료 로그
            logger.log_system(f"텔레그램 종목 스캔 완료 - {len(top_volume_symbols)}개 종목 발견")
            
            return result
            
        except Exception as e:
            error_msg = str(e)
            logger.log_error(e, "텔레그램 종목 스캔 중 오류 발생")
            return f"❌ <b>종목 스캔 중 오류 발생</b>\n\n{error_msg}"

    async def stop_bot(self, args: List[str]) -> str:
        """봇 종료"""
        # 확인 요청
        if not args or not args[0] == "confirm":
            return "⚠️ <b>시스템 종료 확인</b>\n\n정말로 시스템을 종료하시겠습니까? 확인하려면 <code>/stop confirm</code>을 입력하세요."
            
        # 종료 확인
        logger.log_system("텔레그램 명령으로 시스템 종료 요청 수신됨", level="INFO")
        
        # 종료 메시지 전송
        response = "🔴 <b>시스템을 종료합니다...</b>\n\n잠시 후 시스템이 완전히 종료됩니다."
        await self._send_message(response)
        
        # 시스템 상태 업데이트
        try:
            database_manager.update_system_status("STOPPED", "텔레그램 명령으로 시스템 종료됨")
            logger.log_system("시스템 상태를 '종료됨'으로 업데이트", level="INFO")
        except Exception as e:
            logger.log_error(e, "상태 업데이트 중 오류")
        
        # 봇 종료 프로세스 시작 - 비동기로 실행
        asyncio.create_task(self._shutdown_bot())
        
        # 종료 메시지 반환 (이것은 실제로 전송되지 않을 수 있음)
        return response

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

    def is_ready(self) -> bool:
        """봇이 준비되었는지 확인"""
        return self.ready_event is not None and self.ready_event.is_set()

    async def get_status(self, args: List[str]) -> str:
        """시스템 상태 조회"""
        try:
            # 통합 전략 상태
            strategy_status = combined_strategy.get_strategy_status()
            
            # 시스템 상태
            system_status = database_manager.get_system_status()
            status = system_status.get("status", "UNKNOWN")
            status_emoji = {
                "RUNNING": "✅",
                "PAUSED": "⚠️",
                "STOPPED": "❌",
                "ERROR": "⚠️",
                "INITIALIZING": "🔄"
            }.get(status, "❓")
            
            # 포지션 수
            positions_count = len(await order_manager.get_positions())
            
            # 잔고 정보
            account_info = await api_client.get_account_info()
            
            # 안전하게 잔고 정보 추출
            try:
                balance_value = 0
                if account_info and account_info.get("rt_cd") == "0":
                    balance_str = account_info.get("output", {}).get("dnca_tot_amt", "0")
                    balance_value = self.safe_float(balance_str, 0)
                balance = f"{int(balance_value):,}"
            except Exception as e:
                logger.log_error(e, "잔고 정보 변환 오류")
                balance = "0"
            
            # API 상태
            api_status = "🟢 정상" if await api_client.is_token_valid() else "🔴 오류"
            
            # 웹소켓 상태 확인 및 연결 시도
            from core.websocket_client import ws_client
            
            # 웹소켓이 연결되어 있지 않으면 연결 시도
            if not ws_client.is_connected():
                logger.log_system("웹소켓 연결이 끊어져 있어 재연결 시도합니다.")
                try:
                    # 비동기 연결 시도
                    connect_success = await ws_client.connect()
                    if connect_success:
                        logger.log_system("웹소켓 재연결 성공")
                    else:
                        logger.log_system("웹소켓 재연결 실패", level="WARNING")
                except Exception as e:
                    logger.log_error(e, "웹소켓 재연결 중 오류 발생")
            
            # 연결 상태 확인
            ws_status = "🟢 연결됨" if ws_client.is_connected() else "🔴 끊김"
            
            # 실행 시간
            uptime = datetime.now() - database_manager.get_start_time()
            hours, remainder = divmod(uptime.total_seconds(), 3600)
            minutes, seconds = divmod(remainder, 60)
            uptime_str = f"{int(hours)}시간 {int(minutes)}분"
            
            return f"""📊 <b>시스템 상태</b>

{status_emoji} 상태: <b>{status}</b>
⏱ 실행 시간: {uptime_str}
💰 계좌 잔고: {balance}원
📈 포지션: {positions_count}개
🔌 API: {api_status}
🌐 웹소켓: {ws_status}

<b>전략 정보:</b>
- 실행 중: {'✅' if strategy_status.get('running', False) else '❌'}
- 일시 중지: {'⚠️' if strategy_status.get('paused', False) else '✅'}
- 감시 중인 종목: {strategy_status.get('symbols', 0)}개
"""
        except Exception as e:
            logger.log_error(e, "상태 조회 오류")
            return f"❌ <b>상태 조회 중 오류 발생</b>\n{str(e)}"
    
    async def buy_stock(self, args: List[str]) -> str:
        """주식 매수"""
        if len(args) < 2:
            return "사용법: /buy 종목코드 수량 [가격]"
            
        symbol = args[0]
        
        try:
            quantity = int(args[1])
            if quantity <= 0:
                return "❌ 수량은 양수여야 합니다."
        except ValueError:
            return "❌ 올바른 수량을 입력하세요."
        
        price = 0  # 시장가 주문
        if len(args) >= 3:
            try:
                price = self.safe_float(args[2], 0)
            except ValueError:
                return "❌ 올바른 가격을 입력하세요."
        
        # 현재가 조회
        if price == 0:
            try:
                stock_info = await stock_explorer.get_symbol_info(symbol)
                if stock_info:
                    price = self.safe_float(stock_info.get("current_price", 0), 0)
            except Exception as e:
                logger.log_error(e, f"현재가 조회 실패: {symbol}")
        
        # 주문 실행
        order_type = "LIMIT" if price > 0 else "MARKET"
        result = await order_manager.place_order(
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            price=price,
            order_type=order_type,
            reason="telegram_command"
        )
        
        if result and result.get("status") == "success":
            order_id = result.get("order_id", "N/A")
            return f"""✅ <b>매수 주문 성공</b>

종목: {symbol}
수량: {quantity}주
가격: {price:,.0f}원
타입: {order_type}
주문번호: {order_id}"""
        else:
            error = result.get("reason", "알 수 없는 오류") if result else "주문 실패"
            return f"❌ <b>매수 주문 실패</b>\n{error}"
    
    async def sell_stock(self, args: List[str]) -> str:
        """주식 매도"""
        if len(args) < 2:
            return "사용법: /sell 종목코드 수량 [가격]"
            
        symbol = args[0]
        
        try:
            quantity = int(args[1])
            if quantity <= 0:
                return "❌ 수량은 양수여야 합니다."
        except ValueError:
            return "❌ 올바른 수량을 입력하세요."
        
        price = 0  # 시장가 주문
        if len(args) >= 3:
            try:
                price = self.safe_float(args[2], 0)
            except ValueError:
                return "❌ 올바른 가격을 입력하세요."
        
        # 현재가 조회
        if price == 0:
            try:
                stock_info = await stock_explorer.get_symbol_info(symbol)
                if stock_info:
                    price = self.safe_float(stock_info.get("current_price", 0), 0)
            except Exception as e:
                logger.log_error(e, f"현재가 조회 실패: {symbol}")
        
        # 주문 실행
        order_type = "LIMIT" if price > 0 else "MARKET"
        result = await order_manager.place_order(
            symbol=symbol,
            side="SELL",
            quantity=quantity,
            price=price,
            order_type=order_type,
            reason="telegram_command"
        )
        
        if result and result.get("status") == "success":
            order_id = result.get("order_id", "N/A")
            return f"""✅ <b>매도 주문 성공</b>

종목: {symbol}
수량: {quantity}주
가격: {price:,.0f}원
타입: {order_type}
주문번호: {order_id}"""
        else:
            error = result.get("reason", "알 수 없는 오류") if result else "주문 실패"
            return f"❌ <b>매도 주문 실패</b>\n{error}"
    
    async def get_positions(self, args: List[str]) -> str:
        """보유 포지션 조회"""
        positions = await order_manager.get_positions()
        
        if not positions:
            return "📊 <b>보유 종목 없음</b>\n\n현재 보유 중인 종목이 없습니다."
        
        positions_text = ""
        total_value = 0
        
        for pos in positions:
            symbol = pos.get("symbol", "N/A")
            quantity = self.safe_float(pos.get("quantity", 0), 0)
            avg_price = self.safe_float(pos.get("avg_price", 0), 0)
            current_price = self.safe_float(pos.get("current_price", 0), 0)
            
            # 현재가가 없으면 API에서 조회
            if current_price == 0:
                try:
                    stock_info = await stock_explorer.get_symbol_info(symbol)
                    if stock_info:
                        current_price = self.safe_float(stock_info.get("current_price", 0), 0)
                except Exception:
                    pass
            
            # 손익 계산
            if avg_price > 0 and current_price > 0:
                profit_pct = (current_price - avg_price) / avg_price * 100
                profit_emoji = "🔴" if profit_pct < 0 else "🟢" if profit_pct > 0 else "⚪"
            else:
                profit_pct = 0
                profit_emoji = "⚪"
            
            # 종목 총 가치
            position_value = current_price * quantity
            total_value += position_value
            
            # 종목명 가져오기
            stock_name = "N/A"
            try:
                stock_info = await stock_explorer.get_symbol_info(symbol)
                if stock_info:
                    stock_name = stock_info.get("name", "N/A")
            except Exception:
                pass
            
            positions_text += f"{profit_emoji} <b>{symbol}</b> - {stock_name}\n"
            positions_text += f"   {int(quantity)}주 | {avg_price:,.0f}원 → {current_price:,.0f}원 | {profit_pct:.2f}%\n"
            positions_text += f"   가치: {position_value:,.0f}원\n\n"
        
        return f"""📊 <b>보유 종목 현황</b>

{positions_text}
<b>총 자산 가치:</b> {total_value:,.0f}원
<b>보유 종목 수:</b> {len(positions)}개
"""

# 싱글톤 인스턴스
telegram_bot_handler = TelegramBotHandler() 