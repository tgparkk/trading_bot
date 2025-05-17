"""
알림 시스템
"""
import asyncio
import smtplib
import requests
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Dict, Any, Optional
from config.settings import config, AlertConfig
from utils.logger import logger
import os

class AlertSystem:
    """알림 시스템"""
    
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
            self.alert_config = config.get("alert", AlertConfig())
            # 토큰과 채팅 ID 설정 확인
            self.token = self.alert_config.telegram_token
            self.chat_id = self.alert_config.telegram_chat_id
            
            # 하드코딩된 기본값 대신 환경변수나 설정에서 로드된 값 사용
            if self.token == "your_telegram_bot_token" or not self.token:
                logger.log_warning("텔레그램 토큰이 기본값이거나 설정되지 않았습니다.")
                # 환경변수에서 직접 로드 시도
                env_token = os.getenv("TELEGRAM_TOKEN")
                if env_token:
                    self.token = env_token
                    logger.log_system(f"환경변수에서 텔레그램 토큰을 로드했습니다: {self.token[:10]}...")
            
            if self.chat_id == "your_chat_id" or not self.chat_id:
                logger.log_warning("텔레그램 채팅 ID가 기본값이거나 설정되지 않았습니다.")
                # 환경변수에서 직접 로드 시도
                env_chat_id = os.getenv("TELEGRAM_CHAT_ID")
                if env_chat_id:
                    self.chat_id = env_chat_id
                    logger.log_system(f"환경변수에서 텔레그램 채팅 ID를 로드했습니다: {self.chat_id}")
            
            # 로그 남기기
            logger.log_system(f"텔레그램 설정 - 토큰: {self.token[:10]}..., 채팅 ID: {self.chat_id}")
            
            # URL 생성
            self.telegram_bot_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            
            # 초기화 완료 표시
            self._initialized = True
    
    async def send_alert(self, message: str, level: str = "INFO", 
                        channel: str = "all"):
        """알림 전송"""
        try:
            #tasks = []
            
            #if channel in ["all", "telegram"] and self.alert_config.telegram_token:
            #    tasks.append(self._send_telegram(message, level))
            
            #if channel in ["all", "email"] and self.alert_config.email_sender:
            #    tasks.append(self._send_email(message, level))
            
            #if tasks:
            #    await asyncio.gather(*tasks)
            
            logger.log_system(f"Alert sent: {level} - {message[:50]}...")
            
        except Exception as e:
            logger.log_error(e, "Alert system error")
    
    async def _send_telegram(self, message: str, level: str):
        """텔레그램 알림"""
        try:
            emoji = {
                "ERROR": "🚨",
                "WARNING": "⚠️",
                "INFO": "ℹ️",
                "SUCCESS": "✅",
                "TRADE": "💰"
            }.get(level, "📢")
            
            formatted_message = f"{emoji} *{level}*\n\n{message}"
            
            payload = {
                "chat_id": self.chat_id,
                "text": formatted_message,
                "parse_mode": "HTML"
            }
            
            response = requests.post(self.telegram_bot_url, json=payload)
            response.raise_for_status()
            
        except Exception as e:
            logger.log_error(e, "Telegram alert failed")
    
    async def _send_email(self, message: str, level: str):
        """이메일 알림"""
        try:
            msg = MIMEMultipart()
            msg["From"] = self.alert_config.email_sender
            msg["To"] = self.alert_config.email_receiver
            msg["Subject"] = f"Trading Bot Alert - {level}"
            
            body = f"""
            Alert Level: {level}
            Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            
            Message:
            {message}
            """
            
            msg.attach(MIMEText(body, "plain"))
            
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(self.alert_config.email_sender, 
                           self.alert_config.email_password)
                server.send_message(msg)
            
        except Exception as e:
            logger.log_error(e, "Email alert failed")
    
    async def notify_trade(self, trade_data: Dict[str, Any]):
        """거래 알림"""
        if not self.alert_config.alert_on_trade:
            return
        
        # 거래 방향에 따른 이모지
        emoji = "🟢" if trade_data['side'] == "BUY" else "🔴"
        
        # 총 거래 금액 계산
        total_amount = trade_data['price'] * trade_data['quantity']
        
        # 현재 한국 시간
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        message = f"""
{emoji} <b>{trade_data['side']} 거래 체결</b>

<b>종목정보</b>
종목코드: <code>{trade_data['symbol']}</code>
거래방향: {trade_data['side']}
체결가격: {trade_data['price']:,.0f}원
체결수량: {trade_data['quantity']:,}주
총거래금액: {total_amount:,.0f}원

<b>거래상세</b>
주문유형: {trade_data['order_type']}
주문상태: {trade_data.get('status', 'PENDING')}
전략: {trade_data.get('strategy', 'N/A')}
사유: {trade_data.get('reason', 'N/A')}
시간: {now}

주문번호: {trade_data.get('order_id', 'N/A')}
        """
        
        await self.send_alert(message, "TRADE", "telegram")
    
    async def notify_error(self, error: Exception, context: str = None):
        """에러 알림"""
        if not self.alert_config.alert_on_error:
            return
        
        message = f"""
        <b>Error Occurred</b>
        Context: {context or 'Unknown'}
        Error: {str(error)}
        Type: {type(error).__name__}
        Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """
        
        await self.send_alert(message, "ERROR")
    
    async def notify_large_movement(self, symbol: str, price_change: float, 
                                  volume_surge: float = None):
        """급등/급락 알림"""
        if not self.alert_config.alert_on_large_movement:
            return
        
        if abs(price_change) < self.alert_config.large_movement_threshold:
            return
        
        direction = "급등" if price_change > 0 else "급락"
        
        message = f"""
        <b>{direction} 감지</b>
        Symbol: {symbol}
        Price Change: {price_change:.2%}
        """
        
        if volume_surge:
            message += f"\nVolume Surge: {volume_surge:.1f}x"
        
        await self.send_alert(message, "WARNING")
    
    async def notify_system_status(self, status: str, details: str = None):
        """시스템 상태 알림"""
        message = f"""
        <b>System Status Update</b>
        Status: {status}
        Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """
        
        if details:
            message += f"\nDetails: {details}"
        
        level = "ERROR" if status == "ERROR" else "INFO"
        await self.send_alert(message, level)
    
    async def send_daily_report(self, report_data: Dict[str, Any]):
        """일일 리포트"""
        message = f"""
        <b>Daily Trading Report</b>
        Date: {report_data.get('date', datetime.now().strftime('%Y-%m-%d'))}
        
        📊 <b>Performance:</b>
        Total Trades: {report_data.get('daily_trades', 0)}
        Win Rate: {report_data.get('win_rate', 0):.2%}
        Total P&L: ₩{report_data.get('total_pnl', 0):,.0f}
        
        📈 <b>Top Gainers:</b>
        {self._format_top_movers(report_data.get('top_gainers', []))}
        
        📉 <b>Top Losers:</b>
        {self._format_top_movers(report_data.get('top_losers', []))}
        
        💰 Portfolio Value: ₩{report_data.get('portfolio_value', 0):,.0f}
        """
        
        await self.send_alert(message, "INFO")
    
    def _format_top_movers(self, movers: list) -> str:
        """상승/하락 종목 포맷팅"""
        if not movers:
            return "None"
        
        formatted = []
        for mover in movers[:5]:  # 상위 5개만
            formatted.append(
                f"• {mover['symbol']}: {mover['change']:.2%} "
                f"(₩{mover['pnl']:,.0f})"
            )
        
        return "\n".join(formatted)

# 싱글톤 인스턴스
alert_system = AlertSystem()
