"""
로깅 시스템
"""
import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from config.settings import config

class TradingLogger:
    """트레이딩 전용 로거"""
    
    def __init__(self):
        self.config = config["logging"]
        self._setup_directories()
        self._setup_loggers()
    
    def _setup_directories(self):
        """로그 디렉토리 생성"""
        if not os.path.exists(self.config.log_dir):
            os.makedirs(self.config.log_dir)
    
    def _setup_loggers(self):
        """로거 설정"""
        # 트레이드 로거
        self.trade_logger = self._create_logger(
            'trade', 
            os.path.join(self.config.log_dir, self.config.trade_log)
        )
        
        # 에러 로거
        self.error_logger = self._create_logger(
            'error', 
            os.path.join(self.config.log_dir, self.config.error_log),
            level=logging.ERROR
        )
        
        # 시스템 로거
        self.system_logger = self._create_logger(
            'system', 
            os.path.join(self.config.log_dir, self.config.system_log)
        )
    
    def _create_logger(self, name: str, log_file: str, level=None) -> logging.Logger:
        """로거 생성"""
        logger = logging.getLogger(name)
        logger.setLevel(level or getattr(logging, self.config.log_level))
        
        # 파일 핸들러 (로테이팅)
        file_handler = RotatingFileHandler(
            log_file, 
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )
        file_handler.setFormatter(logging.Formatter(self.config.log_format))
        logger.addHandler(file_handler)
        
        # 콘솔 핸들러
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(self.config.log_format))
        logger.addHandler(console_handler)
        
        return logger
    
    def log_trade(self, action: str, symbol: str, price: float, quantity: int, 
                  reason: str = None, **kwargs):
        """거래 로그"""
        message = f"[{action}] {symbol} - Price: {price}, Qty: {quantity}"
        if reason:
            message += f", Reason: {reason}"
        
        extra_info = ", ".join([f"{k}: {v}" for k, v in kwargs.items()])
        if extra_info:
            message += f", {extra_info}"
        
        self.trade_logger.info(message)
    
    def log_error(self, error: Exception, context: str = None):
        """에러 로그"""
        message = f"Error: {str(error)}"
        if context:
            message = f"[{context}] {message}"
        
        self.error_logger.error(message, exc_info=True)
    
    def log_system(self, message: str, level: str = "INFO"):
        """시스템 로그"""
        log_func = getattr(self.system_logger, level.lower())
        log_func(message)
    
    def log_performance(self, symbol: str, pnl: float, win_rate: float, 
                       total_trades: int):
        """성과 로그"""
        message = f"Performance - {symbol}: PnL: {pnl:.2f}, Win Rate: {win_rate:.2%}, " \
                 f"Total Trades: {total_trades}"
        self.trade_logger.info(message)

# 싱글톤 인스턴스
logger = TradingLogger()
