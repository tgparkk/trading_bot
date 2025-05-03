"""
로깅 시스템
"""
import logging
import os
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
# 순환 참조 문제 해결을 위해 설정 import를 함수 내부로 이동

class TradingLogger:
    """트레이딩 전용 로거"""
    
    def __init__(self):
        self._initialized = False
        # 기본 설정값
        self.log_dir = "logs"
        self.log_level = "INFO"
        self.log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        self.trade_log = "trade"
        self.error_log = "error"
        self.system_log = "system"
        
        # 기본 디렉토리와 로거 설정
        self._setup_directories()
        self._setup_loggers()
    
    def initialize_with_config(self):
        """설정을 사용하여 초기화"""
        if self._initialized:
            return
            
        try:
            # 순환 참조 방지를 위해 여기서 임포트
            from config.settings import config, LoggingConfig
            
            # 실제 설정값으로 업데이트
            log_config = config.get("logging", LoggingConfig())
            self.log_dir = log_config.log_dir
            self.log_level = log_config.log_level
            self.log_format = log_config.log_format
            self.trade_log = log_config.trade_log.replace('.log', '')
            self.error_log = log_config.error_log.replace('.log', '')
            self.system_log = log_config.system_log.replace('.log', '')
            
            # 로거 재설정
            self._setup_directories()
            self._setup_loggers()
            
            self._initialized = True
            self.log_system("Logger initialized with config")
        except Exception as e:
            print(f"Failed to initialize logger with config: {e}")
    
    def _setup_directories(self):
        """로그 디렉토리 생성"""
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
        
        # 날짜별 로그 디렉토리 생성
        today = datetime.now().strftime("%Y-%m-%d")
        self.daily_log_dir = os.path.join(self.log_dir, today)
        if not os.path.exists(self.daily_log_dir):
            os.makedirs(self.daily_log_dir)
    
    def _setup_loggers(self):
        """로거 설정"""
        # 트레이드 로거
        self.trade_logger = self._create_logger(
            'trade', 
            os.path.join(self.daily_log_dir, f"{self.trade_log}.log")
        )
        
        # 에러 로거
        self.error_logger = self._create_logger(
            'error', 
            os.path.join(self.daily_log_dir, f"{self.error_log}.log"),
            level=logging.ERROR
        )
        
        # 시스템 로거
        self.system_logger = self._create_logger(
            'system', 
            os.path.join(self.daily_log_dir, f"{self.system_log}.log")
        )
    
    def _create_logger(self, name: str, log_file: str, level=None) -> logging.Logger:
        """로거 생성"""
        logger = logging.getLogger(name)
        logger.setLevel(level or getattr(logging, self.log_level))
        
        # 기존 핸들러 제거 (재설정시 중복 방지)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
        
        # 파일 핸들러 (일간 로테이팅)
        file_handler = TimedRotatingFileHandler(
            log_file, 
            when='midnight',
            backupCount=30  # 30일 동안 보관
        )
        file_handler.setFormatter(logging.Formatter(self.log_format))
        file_handler.suffix = "%Y-%m-%d"  # 백업 파일 이름 형식
        logger.addHandler(file_handler)
        
        # 콘솔 핸들러
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(self.log_format))
        logger.addHandler(console_handler)
        
        return logger
    
    def log_trade(self, action: str, symbol: str, price: float, quantity: int, 
                  reason: str = None, **kwargs):
        """거래 로그"""
        # 현재 날짜의 로그 디렉토리 확인 및 생성
        self._ensure_daily_log_dir()
        
        message = f"[{action}] {symbol} - Price: {price}, Qty: {quantity}"
        if reason:
            message += f", Reason: {reason}"
        
        extra_info = ", ".join([f"{k}: {v}" for k, v in kwargs.items()])
        if extra_info:
            message += f", {extra_info}"
        
        self.trade_logger.info(message)
    
    def log_error(self, error: Exception, context: str = None):
        """에러 로그"""
        # 현재 날짜의 로그 디렉토리 확인 및 생성
        self._ensure_daily_log_dir()
        
        message = f"Error: {str(error)}"
        if context:
            message = f"[{context}] {message}"
        
        self.error_logger.error(message, exc_info=True)
    
    def log_system(self, message: str, level: str = "INFO"):
        """시스템 관련 로그"""
        # 현재 날짜의 로그 디렉토리 확인 및 생성
        self._ensure_daily_log_dir()
        
        level = level.upper()
        
        if level == "ERROR":
            self.system_logger.error(message)
        elif level == "WARNING":
            self.system_logger.warning(message)
        elif level == "DEBUG":
            self.system_logger.debug(message)
        else:  # Default to INFO
            self.system_logger.info(message)
    
    def log_performance(self, symbol: str, pnl: float, win_rate: float, 
                       total_trades: int):
        """성과 로그"""
        # 현재 날짜의 로그 디렉토리 확인 및 생성
        self._ensure_daily_log_dir()
        
        message = f"Performance - {symbol}: PnL: {pnl:.2f}, Win Rate: {win_rate:.2%}, " \
                 f"Total Trades: {total_trades}"
        self.trade_logger.info(message)
    
    def _ensure_daily_log_dir(self):
        """현재 날짜의 로그 디렉토리가 있는지 확인하고 없으면 생성"""
        today = datetime.now().strftime("%Y-%m-%d")
        current_daily_log_dir = os.path.join(self.log_dir, today)
        
        # 날짜가 바뀌었으면 로거 재설정
        if self.daily_log_dir != current_daily_log_dir:
            self.daily_log_dir = current_daily_log_dir
            if not os.path.exists(self.daily_log_dir):
                os.makedirs(self.daily_log_dir)
            
            # 로거 재설정
            self._setup_loggers()

# 싱글톤 인스턴스
logger = TradingLogger()
