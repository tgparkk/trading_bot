"""
설정 파일 - API 키, 계좌 정보, 전략 설정 등
"""
import os
from typing import Dict, Any
from dataclasses import dataclass
from datetime import time

@dataclass
class APIConfig:
    """API 설정"""
    base_url: str
    app_key: str
    app_secret: str
    account_no: str
    ws_url: str = "ws://ops.koreainvestment.com:21000"
    
    @classmethod
    def from_env(cls):
        """환경 변수에서 설정 로드"""
        return cls(
            base_url=os.getenv("KIS_BASE_URL"),
            app_key=os.getenv("KIS_APP_KEY"),
            app_secret=os.getenv("KIS_APP_SECRET"),
            account_no=os.getenv("KIS_ACCOUNT_NO"),
            ws_url=os.getenv("KIS_WS_URL", "ws://ops.koreainvestment.com:21000")
        )

@dataclass
class TradingConfig:
    """트레이딩 설정"""
    # 거래 시간 설정
    market_open: time = time(9, 0)
    market_close: time = time(15, 30)
    
    # 리스크 관리
    max_position_size: int = 10_000_000  # 종목당 최대 포지션 금액
    max_loss_rate: float = 0.03  # 최대 손실률 3%
    max_profit_rate: float = 0.05  # 최대 이익률 5%
    
    # 초단기 전략 파라미터
    scalping_params: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.scalping_params is None:
            self.scalping_params = {
                "tick_window": 10,  # 틱 데이터 윈도우
                "volume_multiplier": 1.5,  # 거래량 증가 배수
                "price_change_threshold": 0.002,  # 가격 변동 임계값 0.2%
                "hold_time": 60,  # 보유 시간 (초)
                "stop_loss": 0.005,  # 손절선 0.5%
                "take_profit": 0.01,  # 익절선 1%
            }

@dataclass
class LoggingConfig:
    """로깅 설정"""
    log_dir: str = "logs"
    log_level: str = "INFO"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    # 로그 파일 설정
    trade_log: str = "trade.log"
    error_log: str = "error.log"
    system_log: str = "system.log"

@dataclass
class DatabaseConfig:
    """데이터베이스 설정"""
    db_path: str = "trading_bot.db"
    backup_interval: int = 3600  # 백업 주기 (초)

@dataclass
class AlertConfig:
    """알림 설정"""
    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    email_sender: str = os.getenv("EMAIL_SENDER", "")
    email_password: str = os.getenv("EMAIL_PASSWORD", "")
    email_receiver: str = os.getenv("EMAIL_RECEIVER", "")
    
    # 알림 조건
    alert_on_error: bool = True
    alert_on_trade: bool = True
    alert_on_large_movement: bool = True
    large_movement_threshold: float = 0.03  # 3% 이상 변동

# 전체 설정
config = {
    "api": APIConfig.from_env(),
    "trading": TradingConfig(),
    "logging": LoggingConfig(),
    "database": DatabaseConfig(),
    "alert": AlertConfig()
}
