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
    def from_env(cls) -> "APIConfig":
        return cls(
            base_url=os.getenv("KIS_BASE_URL", ""),
            app_key=os.getenv("KIS_APP_KEY", ""),
            app_secret=os.getenv("KIS_APP_SECRET", ""),
            account_no=os.getenv("KIS_ACCOUNT_NO", ""),
            ws_url=os.getenv("KIS_WS_URL", "ws://ops.koreainvestment.com:21000"),
        )

@dataclass
class TradingConfig:
    """거래 관련 설정"""
    market_open: time = time(9, 0)
    market_close: time = time(15, 30)
    scalping_params: Dict[str, Any] = None
    filters: Dict[str, Any] = None

    def __post_init__(self):
        if self.scalping_params is None:
            self.scalping_params = {
                "tick_window": 10,
                "volume_multiplier": 1.5,
                "price_change_threshold": 0.002,
                "hold_time": 60,
                "stop_loss": 0.005,
                "take_profit": 0.01,
            }
        if self.filters is None:
            self.filters = {
                "price_min": 1_000,
                "price_max": 50_000,
                "avg_vol_days": 30,
                "min_avg_volume": 100_000,
                "vol_spike_ratio": 2.0,
                "max_volatility": 0.03,
                "max_symbols": 50,
            }

@dataclass
class LoggingConfig:
    """로깅 설정"""
    log_dir: str = "logs"
    log_level: str = "INFO"
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    trade_log: str = "trade.log"
    error_log: str = "error.log"
    system_log: str = "system.log"

@dataclass
class DatabaseConfig:
    """DB 설정"""
    db_path: str = "trading_bot.db"
    backup_interval: int = 3600

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
    "alert": AlertConfig(),
}
