"""
로깅 시스템
"""
import logging
import os
import sys
import re
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from config.settings import config  # config 변수를 명시적으로 임포트
# 순환 참조 문제 해결을 위해 설정 import를 함수 내부로 이동

# 이모지 및 특수 유니코드 문자를 안전한 텍스트로 대체하는 매핑
EMOJI_REPLACEMENTS = {
    '✅': '[OK]',
    '❌': '[ERROR]',
    '⚠️': '[WARNING]',
    '🚀': '[LAUNCH]',
    '🔄': '[SYNC]',
    '🟢': '[GREEN]',
    '🔴': '[RED]',
    '⚪': '[NEUTRAL]',
    '📊': '[STATS]',
    '📈': '[CHART_UP]',
    '📉': '[CHART_DOWN]',
    '💰': '[MONEY]',
    '💹': '[MARKET]',
    '👍': '[THUMBS_UP]',
    '🤖': '[BOT]',
    '🔍': '[SEARCH]',
    'ℹ️': '[INFO]',
    '⭐': '[STAR]'
}

# 유니코드 이모지 제거 또는 대체
def sanitize_for_console(text):
    """콘솔 출력을 위해 유니코드 문자 제거 또는 대체"""
    if not text:
        return text
        
    # 알려진 이모지 대체
    for emoji, replacement in EMOJI_REPLACEMENTS.items():
        text = text.replace(emoji, replacement)
    
    # 다른 이모지나 지원되지 않는 유니코드 문자 제거
    # 기본 ASCII 범위(0-127) 이외의 문자 중 한글(0xAC00-0xD7A3)과 기본 라틴 확장(0x80-0xFF)은 유지
    def is_safe_char(c):
        code = ord(c)
        return (code < 127) or (0xAC00 <= code <= 0xD7A3) or (0x80 <= code <= 0xFF)
    
    # 안전하지 않은 문자는 '?' 또는 다른 대체 문자로 대체
    return ''.join(c if is_safe_char(c) else '?' for c in text)

# 유니코드 안전 출력을 위한 커스텀 StreamHandler
class SafeStreamHandler(logging.StreamHandler):
    def emit(self, record):
        try:
            # 먼저 레코드의 메시지를 직접 정리
            if hasattr(record, 'msg') and isinstance(record.msg, str):
                record.msg = sanitize_for_console(record.msg)
                
            # 포맷팅된 메시지 생성
            msg = self.format(record)
            stream = self.stream
            
            # Windows 콘솔을 위한 추가 정리
            if sys.platform == 'win32':
                msg = sanitize_for_console(msg)
                
            # 인코딩 오류 방지를 위한 추가 확인
            try:
                stream.write(msg + self.terminator)
                self.flush()
            except UnicodeEncodeError:
                # 인코딩 오류 시 ASCII로 안전하게 처리
                safe_msg = ''.join(c if ord(c) < 128 else '?' for c in msg)
                stream.write(safe_msg + self.terminator)
                self.flush()
        except Exception:
            self.handleError(record)

class TradingLogger:
    """트레이딩 전용 로가 (싱글톤 패턴)"""
    
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        """싱글톤 패턴 구현을 위한 __new__ 메서드 오버라이드"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized_instance = False
        return cls._instance
    
    def __init__(self):
        # 이미 초기화된 경우 재초기화 방지
        if getattr(self, '_initialized_instance', False):
            return
            
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
        
        # 싱글톤 초기화 완료 표시
        self._initialized_instance = True
    
    def initialize_with_config(self):
        """설정 파일로부터 로거 초기화"""
        # 이미 초기화 되었으면 건너뜀
        if hasattr(self, '_initialized') and self._initialized:
            return
            
        log_config = config["logging"]
        self.log_dir = log_config.log_dir
        self.log_level = log_config.log_level
        self.log_format = log_config.log_format
        
        # 로그 디렉토리가 없으면 생성
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)
        
        # 오늘 날짜 디렉토리
        today = datetime.now().strftime("%Y-%m-%d")
        self.today_log_dir = os.path.join(self.log_dir, today)
        
        # 오늘 날짜 디렉토리가 없으면 생성
        if not os.path.exists(self.today_log_dir):
            os.makedirs(self.today_log_dir)
        
        # 로그 파일명에 확장자가 없으면 추가
        system_log_file = log_config.system_log if log_config.system_log.endswith('.log') else f"{log_config.system_log}.log"
        trade_log_file = log_config.trade_log if log_config.trade_log.endswith('.log') else f"{log_config.trade_log}.log"
        error_log_file = log_config.error_log if log_config.error_log.endswith('.log') else f"{log_config.error_log}.log"
        
        # 로거 설정 - 로그 레벨을 DEBUG로 변경
        logging.basicConfig(level=logging.DEBUG)
        
        # 기존 로거와 핸들러 모두 정리
        loggers = [logging.getLogger(""), logging.getLogger("system"), 
                  logging.getLogger("trade"), logging.getLogger("error")]
        
        for logger in loggers:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
            logger.setLevel(logging.DEBUG)
            logger.propagate = False
        
        # 시스템 로그
        system_handler = logging.FileHandler(
            os.path.join(self.today_log_dir, system_log_file),
            mode='a',
            encoding='utf-8'  # 명시적 UTF-8 인코딩 설정
        )
        system_handler.setFormatter(logging.Formatter(self.log_format))
        system_handler.setLevel(logging.DEBUG)  # 디버깅을 위해 DEBUG 레벨로 설정
        
        self.system_logger = logging.getLogger("system")
        self.system_logger.setLevel(logging.DEBUG)  # 디버깅을 위해 DEBUG 레벨로 설정
        self.system_logger.addHandler(system_handler)
        self.system_logger.propagate = False
        
        # 트레이드 로그
        trade_handler = logging.FileHandler(
            os.path.join(self.today_log_dir, trade_log_file),
            mode='a',
            encoding='utf-8'  # 명시적 UTF-8 인코딩 설정
        )
        trade_handler.setFormatter(logging.Formatter(self.log_format))
        trade_handler.setLevel(logging.DEBUG)  # 디버깅을 위해 DEBUG 레벨로 설정
        
        self.trade_logger = logging.getLogger("trade")
        self.trade_logger.setLevel(logging.DEBUG)  # 디버깅을 위해 DEBUG 레벨로 설정
        self.trade_logger.addHandler(trade_handler)
        self.trade_logger.propagate = False
        
        # 에러 로그
        error_handler = logging.FileHandler(
            os.path.join(self.today_log_dir, error_log_file),
            mode='a',
            encoding='utf-8'  # 명시적 UTF-8 인코딩 설정
        )
        error_handler.setFormatter(logging.Formatter(self.log_format))
        error_handler.setLevel(logging.DEBUG)  # 디버깅을 위해 DEBUG 레벨로 설정
        
        self.error_logger = logging.getLogger("error")
        self.error_logger.setLevel(logging.DEBUG)  # 디버깅을 위해 DEBUG 레벨로 설정
        self.error_logger.addHandler(error_handler)
        self.error_logger.propagate = False
        
        # 콘솔 로그 추가 (디버깅용) - SafeStreamHandler 사용 - 루트 로거에만 추가
        console_handler = SafeStreamHandler()
        console_handler.setFormatter(logging.Formatter(self.log_format))
        console_handler.setLevel(logging.DEBUG)  # 디버깅을 위해 DEBUG 레벨로 설정
        
        # 콘솔 핸들러를 루트 로거에만 추가 (중복 출력 방지)
        root_logger = logging.getLogger("")
        root_logger.addHandler(console_handler)
        
        # 초기화 완료 표시
        self._initialized = True
        
        # 로그 시작 메시지
        self.log_system(f"Logger initialized. Logs will be saved in: {self.today_log_dir}")
        self.log_system(f"System log: {log_config.system_log}")
        self.log_system(f"Trade log: {log_config.trade_log}")
        self.log_system(f"Error log: {log_config.error_log}")
        self.log_system(f"Log level: DEBUG")
    
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
            backupCount=30,  # 30일 동안 보관
            encoding='utf-8'  # 명시적 UTF-8 인코딩 설정
        )
        file_handler.setFormatter(logging.Formatter(self.log_format))
        file_handler.suffix = "%Y-%m-%d"  # 백업 파일 이름 형식
        logger.addHandler(file_handler)
        
        # 콘솔 핸들러는 초기화 시에만 루트 로거에 추가하고, 개별 로거에는 추가하지 않음
        
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
    
    def log_debug(self, message: str):
        """디버그 로그"""
        # 현재 날짜의 로그 디렉토리 확인 및 생성
        self._ensure_daily_log_dir()
        self.system_logger.debug(message)
    
    def log_performance(self, symbol: str, pnl: float, win_rate: float, 
                       total_trades: int):
        """성과 로그"""
        # 현재 날짜의 로그 디렉토리 확인 및 생성
        self._ensure_daily_log_dir()
        
        message = f"Performance - {symbol}: PnL: {pnl:.2f}, Win Rate: {win_rate:.2%}, " \
                 f"Total Trades: {total_trades}"
        self.trade_logger.info(message)
    
    def log_warning(self, message: str):
        """경고 로그"""
        # 현재 날짜의 로그 디렉토리 확인 및 생성
        self._ensure_daily_log_dir()
        self.system_logger.warning(message)
    
    def _ensure_daily_log_dir(self):
        """현재 날짜의 로그 디렉토리가 있는지 확인하고 없으면 생성"""
        # 현재 날짜 가져오기
        today = datetime.now().strftime("%Y-%m-%d")
        current_daily_log_dir = os.path.join(self.log_dir, today)
        
        # 기본 로그 디렉토리가 없으면 생성
        if not os.path.exists(self.log_dir):
            try:
                os.makedirs(self.log_dir)
                print(f"Created main log directory: {self.log_dir}")
            except Exception as e:
                print(f"Error creating log directory: {e}")
                return
        
        # 날짜별 로그 디렉토리가 없으면 생성
        if not os.path.exists(current_daily_log_dir):
            try:
                os.makedirs(current_daily_log_dir)
                print(f"Created daily log directory: {current_daily_log_dir}")
            except Exception as e:
                print(f"Error creating daily log directory: {e}")
                return
        
        # 날짜가 바뀌었는지 확인
        date_changed = False
        if not hasattr(self, 'daily_log_dir') or self.daily_log_dir != current_daily_log_dir:
            # 이전 날짜 기록
            old_date = getattr(self, 'daily_log_dir', '').split(os.sep)[-1] if hasattr(self, 'daily_log_dir') else None
            
            # 날짜 변경 상태 업데이트
            self.daily_log_dir = current_daily_log_dir
            date_changed = True
            
            # 시스템 로그에 날짜 변경 기록 - 시스템 로거가 있는 경우만
            if hasattr(self, 'system_logger') and self.system_logger:
                try:
                    self.system_logger.info(f"Date changed from {old_date} to {today}. Logs will be saved in: {current_daily_log_dir}")
                except Exception:
                    # 예외 무시 - 로그 설정 중에 예외가 발생할 수 있음
                    pass
            else:
                # 시스템 로거가 없는 경우 콘솔에 출력
                print(f"Date changed to {today}. Logs will be saved in: {current_daily_log_dir}")
            
            # 로거 재설정
            self._setup_loggers()
            
            # 재설정 후 다시 한번 로그 기록
            if hasattr(self, 'system_logger') and self.system_logger:
                try:
                    self.system_logger.info(f"Loggers have been reconfigured for new date: {today}")
                except Exception:
                    pass
        
        return date_changed

# 싱글톤 인스턴스 접근 함수
def get_logger() -> TradingLogger:
    """로거 싱글톤 인스턴스를 반환하는 함수"""
    return TradingLogger()

# 기본 싱글톤 인스턴스 (역호환성 유지)
logger = get_logger()
