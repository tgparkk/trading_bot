import os
from typing import Dict, Any
from dataclasses import dataclass
from datetime import time
from utils.dotenv_helper import dotenv_helper

# 환경 변수 로드 확인
dotenv_helper.load_env()

@dataclass
class APIConfig:
    """API 설정"""
    base_url: str
    app_key: str
    app_secret: str
    account_no: str
    ws_url: str  

    @classmethod
    def from_env(cls) -> "APIConfig":
        return cls(
            base_url=dotenv_helper.get_value("KIS_BASE_URL", ""),
            app_key=dotenv_helper.get_value("KIS_APP_KEY", ""),
            app_secret=dotenv_helper.get_value("KIS_APP_SECRET", ""),
            account_no=dotenv_helper.get_value("KIS_ACCOUNT_NO", ""),
            ws_url=dotenv_helper.get_value("KIS_WS_URL", ""),
        )

@dataclass
class TradingConfig:
    """거래 관련 설정"""
    market_open: time = time(9, 0)
    market_close: time = time(15, 30)
    scalping_params: Dict[str, Any] = None
    filters: Dict[str, Any] = None
    risk_params: Dict[str, Any] = None
    max_websocket_retries: int = 3  # 웹소켓 재시도 횟수

    def __post_init__(self):
        if self.scalping_params is None:
            self.scalping_params = {
                "tick_window": 10,
                "volume_multiplier": 1.5,
                "price_change_threshold": 0.002,
                "hold_time": 60,
                "stop_loss": 0.015,  # -1.5% 손절
                "take_profit": 0.02,  # 2% 익절
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
        if self.risk_params is None:
            self.risk_params = {
                # 포지션 크기 제한
                "max_position_size": 10_000_000,  # 최대 포지션 크기 (1천만원)
                "max_position_per_symbol": 5_000_000,  # 종목별 최대 포지션 (5백만원)
                "position_size_ratio": 0.5 ,  # 계좌 잔고 대비 포지션 크기 비율 (50%)
                
                # 리스크 제한
                "max_loss_rate": 0.02,  # 최대 손실률 (2%)
                "max_volatility": 0.05,  # 최대 허용 변동성 (5%)
                "max_daily_risk": 1_000_000,  # 일일 최대 리스크 금액 (100만원)
                
                # 거래 제한
                "min_daily_volume": 100_000,  # 최소 일일 거래량
                "max_trades_per_day": 50,  # 일일 최대 거래 횟수
                "max_open_positions": 10,  # 최대 동시 포지션 수
                "daily_sell_limit": 100_000_000,  # 일일 매도 한도 (1억원)
                
                # 진입/청산 임계값
                "profit_taking_threshold": 0.018,  # 이익실현 임계값 (1.8%)
                "stop_loss_threshold": 0.15,  # 손절 임계값 (1.5%)
                "trailing_stop_activation": 0.01,  # 트레일링 스탑 활성화 임계값 (1%)
                "trailing_stop_distance": 0.005,  # 트레일링 스탑 거리 (0.5%)
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
    db_path: str = "trading_bot.database_manager"
    backup_interval: int = 3600

class AlertConfig:
    """알림 설정"""
    # 환경 변수에서 텔레그램과 이메일 설정 로드
    telegram_token: str = dotenv_helper.get_value("TELEGRAM_TOKEN", "")
    telegram_chat_id: str = dotenv_helper.get_value("TELEGRAM_CHAT_ID", "")
    email_sender: str = dotenv_helper.get_value("EMAIL_SENDER", "")
    email_password: str = dotenv_helper.get_value("EMAIL_PASSWORD", "")
    email_receiver: str = dotenv_helper.get_value("EMAIL_RECEIVER", "")

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
