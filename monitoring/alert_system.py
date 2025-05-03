"""
알림 시스템
"""
import asyncio
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from typing import Dict, Any, Optional
from config.settings import config, AlertConfig
from utils.logger import logger
import os

class AlertSystem:
    """알림 시스템"""
    
    def __init__(self):
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
    
    async def send_alert(self, message: str, level: str = "INFO", 
                        channel: str = "all"):
        """알림 전송"""
        try:
            tasks = []
            
            if channel in ["all", "telegram"] and self.alert_config.telegram_token:
                tasks.append(self._send_telegram(message, level))
            
            if channel in ["all", "email"] and self.alert_config.email_sender:
                tasks.append(self._send_email(message, level))
            
            if tasks:
                await asyncio.gather(*tasks)
            
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
                "parse_mode": "Markdown"
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
        
        message = f"""
        *Trade Executed*
        Symbol: {trade_data['symbol']}
        Side: {trade_data['side']}
        Price: {trade_data['price']}
        Quantity: {trade_data['quantity']}
        Strategy: {trade_data.get('strategy', 'N/A')}
        Reason: {trade_data.get('reason', 'N/A')}
        """
        
        await self.send_alert(message, "TRADE")
    
    async def notify_error(self, error: Exception, context: str = None):
        """에러 알림"""
        if not self.alert_config.alert_on_error:
            return
        
        message = f"""
        *Error Occurred*
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
        *{direction} 감지*
        Symbol: {symbol}
        Price Change: {price_change:.2%}
        """
        
        if volume_surge:
            message += f"\nVolume Surge: {volume_surge:.1f}x"
        
        await self.send_alert(message, "WARNING")
    
    async def notify_system_status(self, status: str, details: str = None):
        """시스템 상태 알림"""
        message = f"""
        *System Status Update*
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
        *Daily Trading Report*
        Date: {report_data['date']}
        
        📊 Performance:
        Total Trades: {report_data['total_trades']}
        Win Rate: {report_data['win_rate']:.2%}
        Total P&L: ₩{report_data['total_pnl']:,.0f}
        
        📈 Top Gainers:
        {self._format_top_movers(report_data.get('top_gainers', []))}
        
        📉 Top Losers:
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
