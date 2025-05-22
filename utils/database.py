"""
데이터베이스 관리
"""
import sqlite3
import json
import threading
from datetime import datetime, timedelta
import pytz
from typing import Dict, List, Any, Optional
from contextlib import contextmanager
from config.settings import config, DatabaseConfig
from utils.logger import logger

# 한국 시간대 설정
KST = pytz.timezone('Asia/Seoul')

class DatabaseManager:
    """트레이딩 데이터베이스 (리팩토링: 쿼리 헬퍼 및 CRUD 간소화)"""
    
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
            db_cfg = config.get("database", DatabaseConfig())
            self.db_path = db_cfg.db_path
            self.backup_interval = db_cfg.backup_interval
            self._initialize_db()
            self._initialized = True
    
    @contextmanager
    def get_connection(self, max_retries=3, retry_delay=0.5):
        """데이터베이스 연결 컨텍스트 매니저
        
        Args:
            max_retries: 연결 시도 최대 횟수
            retry_delay: 재시도 간 대기 시간(초)
            
        Yields:
            sqlite3.Connection: 데이터베이스 연결 객체
        """
        conn = None
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                conn = sqlite3.connect(self.db_path, timeout=10)  # 10초 타임아웃 설정
                conn.row_factory = sqlite3.Row  # 딕셔너리 형태로 결과 반환
                
                # 한국 시간 변환을 위한 함수 등록
                conn.create_function("kst_datetime", 0, self._current_kst_datetime)
                
                yield conn
                
                # 예외 없이 종료된 경우 커밋
                if conn:
                    conn.commit()
                
                # 성공적으로 완료됨
                return
            except sqlite3.OperationalError as e:
                last_exception = e
                
                # 데이터베이스 잠금 오류인 경우 재시도
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    import time
                    if conn:
                        try:
                            conn.close()
                        except Exception:
                            pass
                    conn = None
                    logger.log_system(f"데이터베이스 잠금 오류, {retry_delay}초 후 재시도 ({attempt+1}/{max_retries})", level="WARNING")
                    time.sleep(retry_delay)
                else:
                    # 최대 시도 횟수 초과하거나 다른 오류
                    if conn:
                        try:
                            conn.rollback()
                            conn.close()
                        except Exception:
                            pass
                    logger.log_error(e, f"데이터베이스 연결 오류 (시도 {attempt+1}/{max_retries})")
                    raise
            except Exception as e:
                last_exception = e
                if conn:
                    try:
                        conn.rollback()
                        conn.close()
                    except Exception:
                        pass
                raise
            finally:
                # 마지막 시도에서 실패했고 아직 연결이 열려있으면 닫기
                if attempt == max_retries - 1 and conn and last_exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
    
    def _current_kst_datetime(self):
        """현재 한국 시간을 ISO 형식 문자열로 반환하는 SQLite 함수"""
        now_utc = datetime.now(pytz.UTC)
        now_kst = now_utc.astimezone(KST)
        return now_kst.strftime('%Y-%m-%d %H:%M:%S')
    
    # --- 쿼리 빌더/실행 헬퍼 ---
    def _build_where_clause(self, conditions):
        where_clauses = []
        params = []
        for key, value in (conditions or {}).items():
            if isinstance(value, tuple):
                operator, val = value
                where_clauses.append(f"{key} {operator} ?")
                params.append(val)
            else:
                where_clauses.append(f"{key} = ?")
                params.append(value)
        return (" AND ".join(where_clauses), params) if where_clauses else ("", [])

    def _build_query(self, operation, table, data=None, conditions=None, order_by=None, limit=None):
        params = []
        if operation == "SELECT":
            query = f"SELECT * FROM {table}"
            where, where_params = self._build_where_clause(conditions)
            if where:
                query += f" WHERE {where}"
                params.extend(where_params)
            if order_by:
                direction = "DESC" if order_by.startswith("-") else "ASC"
                column = order_by[1:] if order_by.startswith("-") else order_by
                query += f" ORDER BY {column} {direction}"
            if limit:
                query += f" LIMIT {limit}"
        elif operation == "INSERT":
            columns = list(data.keys())
            placeholders = ["?"] * len(columns)
            values = list(data.values())
            query = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
            params.extend(values)
        elif operation == "REPLACE" or operation == "UPSERT":
            columns = list(data.keys())
            placeholders = ["?"] * len(columns)
            values = list(data.values())
            query = f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({', '.join(placeholders)})"
            params.extend(values)

        elif operation == "UPDATE":
            set_clause = ", ".join([f"{k} = ?" for k in data.keys()])
            values = list(data.values())
            query = f"UPDATE {table} SET {set_clause}"
            params.extend(values)
            where, where_params = self._build_where_clause(conditions)
            if where:
                query += f" WHERE {where}"
                params.extend(where_params)
        return query, params

    def _execute_query(self, operation, table, data=None, conditions=None, order_by=None, limit=None, single=False):
        query, params = self._build_query(operation, table, data, conditions, order_by, limit)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            if operation == "SELECT":
                if single:
                    row = cursor.fetchone()
                    return dict(row) if row else None
                else:
                    return [dict(row) for row in cursor.fetchall()]
            elif operation == "INSERT":
                return cursor.lastrowid
            elif operation == "UPDATE":
                return cursor.rowcount

    # --- 테이블 스키마 정의 통합 ---
    def _initialize_db(self, force_initialize=False):
        table_schemas = {
            "orders": """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    price REAL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    filled_quantity INTEGER DEFAULT 0,
                    avg_price REAL,
                    commission REAL,
                    created_at TIMESTAMP DEFAULT (kst_datetime()),
                    updated_at TIMESTAMP DEFAULT (kst_datetime()),
                    strategy TEXT,
                    reason TEXT
                )
            """,
            "positions": """
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT UNIQUE,
                    quantity INTEGER NOT NULL,
                    avg_price REAL NOT NULL,
                    current_price REAL,
                    unrealized_pnl REAL,
                    realized_pnl REAL DEFAULT 0,
                    total_buy_amount REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT (kst_datetime()),
                    updated_at TIMESTAMP DEFAULT (kst_datetime())
                )
            """,
            "trades": """
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    pnl REAL,
                    commission REAL,
                    created_at TIMESTAMP DEFAULT (kst_datetime()),
                    strategy TEXT,
                    entry_reason TEXT,
                    exit_reason TEXT,
                    order_id TEXT,
                    order_type TEXT,
                    status TEXT,
                    time TEXT
                )
            """,
            "performance": """
                CREATE TABLE IF NOT EXISTS performance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL,
                    symbol TEXT,
                    total_trades INTEGER DEFAULT 0,
                    winning_trades INTEGER DEFAULT 0,
                    losing_trades INTEGER DEFAULT 0,
                    total_pnl REAL DEFAULT 0,
                    win_rate REAL,
                    sharpe_ratio REAL,
                    max_drawdown REAL,
                    daily_pnl REAL,
                    daily_trades INTEGER,
                    created_at TIMESTAMP DEFAULT (kst_datetime()),
                    UNIQUE(date, symbol)
                )
            """,
            "system_status": """
                CREATE TABLE IF NOT EXISTS system_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    last_heartbeat TIMESTAMP,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT (kst_datetime())
                )
            """,
            "token_logs": """
                CREATE TABLE IF NOT EXISTS token_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    token TEXT,
                    issue_time TIMESTAMP,
                    expire_time TIMESTAMP,
                    status TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT (kst_datetime())
                )
            """,
            "symbol_search_logs": """
                CREATE TABLE IF NOT EXISTS symbol_search_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_time TIMESTAMP,
                    total_symbols INTEGER,
                    filtered_symbols INTEGER,
                    search_criteria TEXT,
                    status TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT (kst_datetime())
                )
            """,
            "telegram_messages": """
                CREATE TABLE IF NOT EXISTS telegram_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    direction TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    message_id TEXT,
                    update_id INTEGER,
                    is_command INTEGER DEFAULT 0,
                    command TEXT,
                    processed INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'SUCCESS',
                    error_message TEXT,
                    reply_to TEXT,
                    created_at TIMESTAMP DEFAULT (kst_datetime())
                )
            """
        }
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for schema in table_schemas.values():
                cursor.execute(schema)
            
            # 기존 테이블에 새 컬럼 추가
            self._add_missing_columns(conn)
            
            logger.log_system("Database initialized successfully")
            self._update_timestamps_to_kst(conn)
            conn.commit()
            
    def _add_missing_columns(self, conn):
        """기존 테이블에 누락된 컬럼 추가"""
        try:
            cursor = conn.cursor()
            
            # positions 테이블에 total_buy_amount 컬럼 추가
            try:
                cursor.execute("SELECT total_buy_amount FROM positions LIMIT 1")
            except sqlite3.OperationalError:
                logger.log_system("positions 테이블에 total_buy_amount 컬럼 추가 중...")
                cursor.execute("ALTER TABLE positions ADD COLUMN total_buy_amount REAL DEFAULT 0")
                logger.log_system("positions 테이블에 total_buy_amount 컬럼 추가 완료")
            
            # positions 테이블에 total_sell_amount 컬럼 추가
            try:
                cursor.execute("SELECT total_sell_amount FROM positions LIMIT 1")
            except sqlite3.OperationalError:
                logger.log_system("positions 테이블에 total_sell_amount 컬럼 추가 중...")
                cursor.execute("ALTER TABLE positions ADD COLUMN total_sell_amount REAL DEFAULT 0")
                logger.log_system("positions 테이블에 total_sell_amount 컬럼 추가 완료")
            
            # positions 테이블에 profit_rate 컬럼 추가
            try:
                cursor.execute("SELECT profit_rate FROM positions LIMIT 1")
            except sqlite3.OperationalError:
                logger.log_system("positions 테이블에 profit_rate 컬럼 추가 중...")
                cursor.execute("ALTER TABLE positions ADD COLUMN profit_rate REAL DEFAULT 0")
                logger.log_system("positions 테이블에 profit_rate 컬럼 추가 완료")
            
            # trades 테이블에 time 컬럼 추가
            try:
                cursor.execute("SELECT time FROM trades LIMIT 1")
            except sqlite3.OperationalError:
                logger.log_system("trades 테이블에 time 컬럼 추가 중...")
                cursor.execute("ALTER TABLE trades ADD COLUMN time TEXT")
                logger.log_system("trades 테이블에 time 컬럼 추가 완료")
            
            # trades 테이블에 reason 컬럼 추가
            try:
                cursor.execute("SELECT reason FROM trades LIMIT 1")
            except sqlite3.OperationalError:
                logger.log_system("trades 테이블에 reason 컬럼 추가 중...")
                cursor.execute("ALTER TABLE trades ADD COLUMN reason TEXT")
                logger.log_system("trades 테이블에 reason 컬럼 추가 완료")
                
            conn.commit()
        except Exception as e:
            logger.log_error(e, "컬럼 추가 중 오류 발생")
            raise
    
    def _update_timestamps_to_kst(self, conn):
        """기존 데이터베이스의 타임스탬프를 KST로 변환"""
        try:
            timestamp_columns = {
                "orders": ["created_at", "updated_at"],
                "positions": ["created_at", "updated_at"],
                "trades": ["created_at"],
                "system_status": ["created_at", "last_heartbeat"],
                "token_logs": ["created_at", "issue_time", "expire_time"],
                "symbol_search_logs": ["created_at", "search_time"],
                "telegram_messages": ["created_at"]
            }
            cursor = conn.cursor()
            for table, columns in timestamp_columns.items():
                for column in columns:
                    cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
                    if cursor.fetchone():
                        cursor.execute(f"""
                            UPDATE {table} 
                            SET {column} = datetime({column}, '+9 hours')
                            WHERE {column} IS NOT NULL
                              AND {column} NOT LIKE '%+09:00%'
                              AND {column} NOT LIKE '%+0900%'
                        """)
        except Exception as e:
            logger.log_error(e, "타임스탬프 변환 중 오류 발생")
    
    def save_order(self, order_data: Dict[str, Any]) -> int:
        """주문 저장"""
        return self._execute_query("INSERT", "orders", data=order_data)
    
    def update_order(self, order_id: str, update_data: Dict[str, Any]):
        """주문 업데이트"""
        data = {**update_data, "updated_at": self._current_kst_datetime()}
        return self._execute_query("UPDATE", "orders", data=data, conditions={"order_id": order_id})
    
    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """주문 조회"""
        return self._execute_query("SELECT", "orders", conditions={"order_id": order_id}, single=True)
    
    def save_position(self, position_data: Dict[str, Any]):
        """포지션 저장/업데이트"""
        return self._execute_query("REPLACE", "positions", data=position_data)
    
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """포지션 조회"""
        return self._execute_query("SELECT", "positions", conditions={"symbol": symbol}, single=True)
    
    def get_all_positions(self) -> List[Dict[str, Any]]:
        """모든 포지션 조회"""
        return self._execute_query("SELECT", "positions", conditions={"quantity": ("!=", 0)})
    
    def save_trade(self, trade_data: Dict[str, Any]):
        """거래 기록 저장"""
        return self._execute_query("INSERT", "trades", data=trade_data)
    
    def get_trades(self, symbol: str = None, start_date: str = None, 
                   end_date: str = None) -> List[Dict[str, Any]]:
        """거래 기록 조회"""
        conditions = {}
        if symbol:
            conditions["symbol"] = symbol
        if start_date:
            conditions["created_at"] = (">=", start_date)
        if end_date:
            conditions["created_at"] = ("<=", end_date)
        return self._execute_query("SELECT", "trades", conditions=conditions, order_by="-created_at")
    
    def get_latest_trade(self, symbol: str, side: str = None) -> Optional[Dict[str, Any]]:
        """특정 종목의 최근 거래 기록 조회
        
        Args:
            symbol: 종목 코드
            side: 거래 방향 (BUY/SELL)
            
        Returns:
            최신 거래 기록 또는 None
        """
        conditions = {"symbol": symbol}
        if side:
            conditions["side"] = side
        return self._execute_query("SELECT", "trades", conditions=conditions, order_by="-created_at", limit=1, single=True)
    
    def save_performance(self, performance_data: Dict[str, Any]):
        """성과 기록 저장"""
        return self._execute_query("INSERT", "performance", data=performance_data)
    
    def update_system_status(self, status: str, error_message: str = None):
        """시스템 상태 업데이트"""
        data = {"status": status, "last_heartbeat": self._current_kst_datetime(), "error_message": error_message}
        return self._execute_query("INSERT", "system_status", data=data)
    
    def get_latest_system_status(self) -> Optional[Dict[str, Any]]:
        """최신 시스템 상태 조회"""
        return self._execute_query("SELECT", "system_status", order_by="-created_at", single=True)
    
    def backup_database(self, backup_path: Optional[str] = None):
        """데이터베이스 백업 (비활성화됨)"""
        # 백업 기능 비활성화
        logger.log_system("Database backup is disabled")
        return
        
        # 아래 코드는 비활성화됨
        # if backup_path is None:
        #     backup_path = f"{self.db_path}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        # 
        # with self.get_connection() as conn:
        #     backup_conn = sqlite3.connect(backup_path)
        #     conn.backup(backup_conn)
        #     backup_conn.close()
        # 
        # logger.log_system(f"Database backed up to {backup_path}")
    
    def save_token_log(self, event_type: str, token: str = None, 
                      issue_time: datetime = None, expire_time: datetime = None,
                      status: str = None, error_message: str = None):
        """토큰 관련 로그 저장"""
        data = {
            "event_type": event_type,
            "token": token,
            "issue_time": issue_time.isoformat() if issue_time else None,
            "expire_time": expire_time.isoformat() if expire_time else None,
            "status": status,
            "error_message": error_message
        }
        return self._execute_query("INSERT", "token_logs", data=data)
    
    def save_symbol_search_log(self, total_symbols: int, filtered_symbols: int,
                             search_criteria: Dict[str, Any], status: str,
                             error_message: str = None):
        """종목 탐색 로그 저장"""
        now_kst = datetime.now(KST).isoformat()
        data = {
            "search_time": now_kst,
            "total_symbols": total_symbols,
            "filtered_symbols": filtered_symbols,
            "search_criteria": json.dumps(search_criteria),
            "status": status,
            "error_message": error_message
        }
        return self._execute_query("INSERT", "symbol_search_logs", data=data)
    
    def get_token_logs(self, start_date: str = None, end_date: str = None,
                      event_type: str = None) -> List[Dict[str, Any]]:
        """토큰 로그 조회"""
        conditions = {}
        if start_date:
            conditions["created_at"] = (">=", start_date)
        if end_date:
            conditions["created_at"] = ("<=", end_date)
        if event_type:
            conditions["event_type"] = event_type
        return self._execute_query("SELECT", "token_logs", conditions=conditions, order_by="-created_at")
    
    def get_latest_valid_token(self) -> Optional[Dict[str, Any]]:
        """현재 유효한 최신 API 토큰 조회"""
        current_time = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
        conditions = {"status": "SUCCESS", "event_type": "ISSUE", "expire_time": (">", current_time)}
        return self._execute_query("SELECT", "token_logs", conditions=conditions, order_by="-id", single=True)
    
    def get_symbol_search_logs(self, start_date: str = None, 
                             end_date: str = None) -> List[Dict[str, Any]]:
        """종목 탐색 로그 조회"""
        conditions = {}
        if start_date:
            conditions["created_at"] = (">=", start_date)
        if end_date:
            conditions["created_at"] = ("<=", end_date)
        return self._execute_query("SELECT", "symbol_search_logs", conditions=conditions, order_by="-created_at")
    
    def save_telegram_message(self, direction: str, chat_id: str, message_text: str,
                             message_id: str = None, update_id: int = None, 
                             is_command: bool = False, command: str = None,
                             processed: bool = False, status: str = "SUCCESS",
                             error_message: str = None, reply_to: str = None):
        """텔레그램 메시지 저장
        
        Args:
            direction: 메시지 방향 (INCOMING/OUTGOING)
            chat_id: 텔레그램 채팅 ID
            message_text: 메시지 내용
            message_id: 텔레그램 메시지 ID (수신 메시지인 경우)
            update_id: 텔레그램 업데이트 ID (수신 메시지인 경우)
            is_command: 명령어 여부
            command: 명령어 (is_command가 True인 경우)
            processed: 처리 완료 여부
            status: 상태 (SUCCESS/FAIL)
            error_message: 오류 메시지 (status가 FAIL인 경우)
            reply_to: 답장 대상 메시지 ID
            
        Returns:
            새로운 메시지의 ID
        """
        data = {
            "direction": direction,
            "chat_id": str(chat_id),
            "message_text": message_text,
            "message_id": message_id,
            "update_id": update_id,
            "is_command": 1 if is_command else 0,
            "command": command,
            "processed": 1 if processed else 0,
            "status": status,
            "error_message": error_message,
            "reply_to": reply_to
        }
        return self._execute_query("INSERT", "telegram_messages", data=data)
    
    def update_telegram_message(self, db_message_id: int, message_id: str = None, 
                              status: str = None, error_message: str = None):
        """텔레그램 메시지 업데이트 (outgoing 메시지의 실제 전송 결과를 업데이트)"""
        data = {}
        if message_id is not None:
            data["message_id"] = message_id
        if status is not None:
            data["status"] = status
        if error_message is not None:
            data["error_message"] = error_message
        if not data:
            return
        return self._execute_query("UPDATE", "telegram_messages", data=data, conditions={"id": db_message_id})
    
    def update_telegram_message_status(self, message_id: str, processed: bool = True, 
                                     status: str = "SUCCESS", error_message: str = None):
        """텔레그램 메시지 상태 업데이트"""
        data = {"processed": 1 if processed else 0, "status": status, "error_message": error_message}
        return self._execute_query("UPDATE", "telegram_messages", data=data, conditions={"message_id": message_id})
    
    def get_telegram_messages(self, direction: str = None, chat_id: str = None,
                            is_command: bool = None, processed: bool = None,
                            start_date: str = None, end_date: str = None,
                            message_id: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        """텔레그램 메시지 조회"""
        conditions = {}
        if direction:
            conditions["direction"] = direction
        if chat_id:
            conditions["chat_id"] = str(chat_id)
        if message_id:
            conditions["message_id"] = message_id
        if is_command is not None:
            conditions["is_command"] = 1 if is_command else 0
        if processed is not None:
            conditions["processed"] = 1 if processed else 0
        if start_date:
            conditions["created_at"] = (">=", start_date)
        if end_date:
            conditions["created_at"] = ("<=", end_date)
        return self._execute_query("SELECT", "telegram_messages", conditions=conditions, order_by="-created_at", limit=limit)
    
    def get_system_status(self) -> Dict[str, Any]:
        """현재 시스템 상태 조회"""
        status = self.get_latest_system_status()
        if not status:
            # 현재 한국 시간 사용
            now_kst = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
            return {
                "status": "UNKNOWN",
                "updated_at": now_kst,
                "error_message": None
            }
        
        # 시간 형식 확인 및 수정
        updated_at = status.get("created_at")
        if updated_at:
            try:
                # 문자열 파싱하여 유효성 확인
                dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                
                # 미래 날짜이거나 2025년 이전인 경우 현재 시간으로 대체
                now = datetime.now()
                if dt.year < 2025 or dt > now:
                    updated_at = now.strftime("%Y-%m-%d %H:%M:%S")
                    logger.log_system(f"시스템 상태의 날짜가 이상하여 현재 시간으로 대체합니다: {dt} -> {now}", level="WARNING")
            except (ValueError, TypeError):
                # 파싱 실패 시 현재 시간 사용
                updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.log_system(f"시스템 상태의 날짜 형식이 잘못되어 현재 시간으로 대체합니다: {status.get('created_at')} -> {updated_at}", level="WARNING")
        else:
            # 시간 정보가 없는 경우 현재 시간 사용
            updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        return {
            "status": status["status"],
            "updated_at": updated_at,
            "error_message": status["error_message"]
        }
        
    def get_start_time(self) -> datetime:
        """시스템 시작 시간 반환
        시스템이 처음 'RUNNING' 상태가 된 시간을 반환하거나
        시작 시간 정보가 없는 경우 현재 시간을 반환합니다.
        
        Returns:
            datetime: 시스템 시작 시간
        """
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                # 가장 최근의 RUNNING 상태 기록을 찾음
                cursor.execute("""
                    SELECT created_at FROM system_status 
                    WHERE status = 'RUNNING' 
                    ORDER BY created_at ASC LIMIT 1
                """)
                result = cursor.fetchone()
                
                if result and result['created_at']:
                    try:
                        # 문자열 시간을 datetime 객체로 변환
                        start_time = datetime.strptime(result['created_at'], "%Y-%m-%d %H:%M:%S")
                        return start_time
                    except (ValueError, TypeError):
                        # 시간 파싱 오류 시 현재 시간 반환
                        logger.log_system("시작 시간 파싱 오류, 현재 시간 사용", level="WARNING")
                        return datetime.now()
                else:
                    # 시작 기록이 없으면 현재 시간 반환
                    return datetime.now()
        except Exception as e:
            # 오류 발생 시 현재 시간 반환
            logger.log_error(e, "시작 시간 조회 중 오류 발생, 현재 시간 사용")
            return datetime.now()

    def get_latest_token(self) -> Optional[Dict[str, Any]]:
        """가장 최근에 발급된 유효한 토큰 조회 (하위 호환용)"""
        try:
            # 우선 유효한 토큰 찾기
            valid_token = self.get_latest_valid_token()
            if valid_token:
                return valid_token
            
            # 유효한 토큰이 없으면 가장 최근에 발급된 토큰 반환 (만료 여부 무관)
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                query = """
                    SELECT * FROM token_logs 
                    WHERE event_type = 'ISSUE' AND status = 'SUCCESS' AND token IS NOT NULL 
                    ORDER BY id DESC LIMIT 1
                """
                
                cursor.execute(query)
                token_data = cursor.fetchone()
                
                if token_data:
                    return dict(token_data)
                return None
        except Exception as e:
            logger.log_error(e, "최신 토큰 조회 실패")
            return None

    def update_database_schema(self):
        """데이터베이스 스키마 강제 업데이트"""
        try:
            logger.log_system("데이터베이스 스키마 강제 업데이트 시작...")
            with self.get_connection() as conn:
                self._add_missing_columns(conn)
            logger.log_system("데이터베이스 스키마 업데이트 완료")
            return True
        except Exception as e:
            logger.log_error(e, "데이터베이스 스키마 업데이트 실패")
            return False

    def get_recent_trades_by_strategy(self, strategy: str, limit: int = 20):
        """특정 전략의 최근 거래 내역 조회"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM trades 
                    WHERE strategy = ? 
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (strategy, limit))
                
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.log_error(e, f"Failed to get recent trades for strategy {strategy}")
            return []
    
    def get_recent_trades_by_symbol(self, symbol: str, limit: int = 10):
        """특정 종목의 최근 거래 내역 조회"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT * FROM trades 
                    WHERE symbol = ? 
                    ORDER BY created_at DESC
                    LIMIT ?
                """, (symbol, limit))
                
                return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.log_error(e, f"Failed to get recent trades for symbol {symbol}")
            return []

# 싱글톤 인스턴스 생성
database_manager = DatabaseManager()

# 데이터베이스 스키마 즉시 업데이트 실행
try:
    database_manager.update_database_schema()
except Exception as e:
    logger.log_error(e, "자동 스키마 업데이트 실패 - 나중에 다시 시도하세요")
