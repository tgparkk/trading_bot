"""
데이터베이스 관리
"""
import sqlite3
import json
from datetime import datetime
from typing import Dict, List, Any, Optional
from contextlib import contextmanager
from config.settings import config, DatabaseConfig
from utils.logger import logger

class Database:
    """트레이딩 데이터베이스"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance
    
    def _initialize(self, _force_initialize=False):
        db_cfg = config.get("database", DatabaseConfig())
        self.db_path = db_cfg.db_path
        self.backup_interval = db_cfg.backup_interval
        self._initialize_db(_force_initialize)
    
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
    
    def _initialize_db(self, force_initialize=False):
        """데이터베이스 초기화"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # 주문 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT UNIQUE,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,  -- BUY/SELL
                    order_type TEXT NOT NULL,  -- MARKET/LIMIT
                    price REAL,
                    quantity INTEGER NOT NULL,
                    status TEXT NOT NULL,  -- PENDING/FILLED/CANCELLED/REJECTED
                    filled_quantity INTEGER DEFAULT 0,
                    avg_price REAL,
                    commission REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    strategy TEXT,
                    reason TEXT
                )
            """)
            
            # 포지션 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT UNIQUE,
                    quantity INTEGER NOT NULL,
                    avg_price REAL NOT NULL,
                    current_price REAL,
                    unrealized_pnl REAL,
                    realized_pnl REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 거래 기록 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    price REAL NOT NULL,
                    quantity INTEGER NOT NULL,
                    pnl REAL,
                    commission REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    strategy TEXT,
                    entry_reason TEXT,
                    exit_reason TEXT
                )
            """)
            
            # 성과 테이블
            cursor.execute("""
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
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date, symbol)
                )
            """)
            
            # 시스템 상태 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,  -- RUNNING/STOPPED/ERROR
                    last_heartbeat TIMESTAMP,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 토큰 관리 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS token_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,  -- ISSUE/ACCESS/FAIL
                    token TEXT,
                    issue_time TIMESTAMP,
                    expire_time TIMESTAMP,
                    status TEXT,  -- SUCCESS/FAIL
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 종목 탐색 테이블
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS symbol_search_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    search_time TIMESTAMP NOT NULL,
                    total_symbols INTEGER,
                    filtered_symbols INTEGER,
                    search_criteria TEXT,  -- JSON 형식으로 저장
                    status TEXT,  -- SUCCESS/FAIL
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 텔레그램 메시지 로그 테이블이 존재하는지 확인
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_messages'")
            telegram_table_exists = cursor.fetchone() is not None
            
            # 테이블이 없거나 강제 초기화가 요청된 경우 생성
            if not telegram_table_exists or force_initialize:
                # 기존 테이블이 있으면 삭제
                if telegram_table_exists and force_initialize:
                    cursor.execute("DROP TABLE telegram_messages")
                
                # 텔레그램 메시지 로그 테이블
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS telegram_messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        direction TEXT NOT NULL,  -- INCOMING/OUTGOING
                        chat_id TEXT NOT NULL,
                        message_text TEXT,
                        command TEXT,  -- 명령어인 경우 해당 명령어
                        message_id TEXT,  -- 텔레그램에서 제공하는 메시지 ID
                        update_id INTEGER,  -- 텔레그램 업데이트 ID
                        is_command BOOLEAN DEFAULT 0,  -- 명령어 여부
                        processed BOOLEAN DEFAULT 0,  -- 처리 완료 여부
                        status TEXT,  -- SUCCESS/FAIL
                        error_message TEXT,
                        reply_to TEXT,  -- 답장 대상 메시지 ID
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                logger.log_system("Telegram messages table created or re-initialized")
            else:
                # 테이블이 존재하는 경우 reply_to 필드가 있는지 확인
                cursor.execute("PRAGMA table_info(telegram_messages)")
                columns = cursor.fetchall()
                column_names = [column[1] for column in columns]
                
                # reply_to 필드가 없으면 추가
                if 'reply_to' not in column_names:
                    cursor.execute("ALTER TABLE telegram_messages ADD COLUMN reply_to TEXT")
                    logger.log_system("Added reply_to column to telegram_messages table")
            
            conn.commit()
            logger.log_system("Database initialized successfully")
    
    def save_order(self, order_data: Dict[str, Any]) -> int:
        """주문 저장"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO orders (
                    order_id, symbol, side, order_type, price, quantity, 
                    status, strategy, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                order_data.get('order_id'),
                order_data['symbol'],
                order_data['side'],
                order_data['order_type'],
                order_data.get('price'),
                order_data['quantity'],
                order_data['status'],
                order_data.get('strategy'),
                order_data.get('reason')
            ))
            
            conn.commit()
            return cursor.lastrowid
    
    def update_order(self, order_id: str, update_data: Dict[str, Any]):
        """주문 업데이트"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            set_clause = ", ".join([f"{k} = ?" for k in update_data.keys()])
            values = list(update_data.values()) + [order_id]
            
            cursor.execute(f"""
                UPDATE orders 
                SET {set_clause}, updated_at = CURRENT_TIMESTAMP
                WHERE order_id = ?
            """, values)
            
            conn.commit()
    
    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        """주문 조회"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def save_position(self, position_data: Dict[str, Any]):
        """포지션 저장/업데이트"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT OR REPLACE INTO positions (
                    symbol, quantity, avg_price, current_price, 
                    unrealized_pnl, realized_pnl
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                position_data['symbol'],
                position_data['quantity'],
                position_data['avg_price'],
                position_data.get('current_price'),
                position_data.get('unrealized_pnl', 0),
                position_data.get('realized_pnl', 0)
            ))
            
            conn.commit()
    
    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """포지션 조회"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM positions WHERE symbol = ?", (symbol,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_all_positions(self) -> List[Dict[str, Any]]:
        """모든 포지션 조회"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM positions WHERE quantity != 0")
            return [dict(row) for row in cursor.fetchall()]
    
    def save_trade(self, trade_data: Dict[str, Any]):
        """거래 기록 저장"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO trades (
                    symbol, side, price, quantity, pnl, commission,
                    strategy, entry_reason, exit_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_data['symbol'],
                trade_data['side'],
                trade_data['price'],
                trade_data['quantity'],
                trade_data.get('pnl'),
                trade_data.get('commission'),
                trade_data.get('strategy'),
                trade_data.get('entry_reason'),
                trade_data.get('exit_reason')
            ))
            
            conn.commit()
    
    def get_trades(self, symbol: str = None, start_date: str = None, 
                   end_date: str = None) -> List[Dict[str, Any]]:
        """거래 기록 조회"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM trades WHERE 1=1"
            params = []
            
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol)
            
            if start_date:
                query += " AND created_at >= ?"
                params.append(start_date)
            
            if end_date:
                query += " AND created_at <= ?"
                params.append(end_date)
            
            query += " ORDER BY created_at DESC"
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def save_performance(self, performance_data: Dict[str, Any]):
        """성과 기록 저장"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT OR REPLACE INTO performance (
                    date, symbol, total_trades, winning_trades, losing_trades,
                    total_pnl, win_rate, sharpe_ratio, max_drawdown
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                performance_data['date'],
                performance_data.get('symbol'),
                performance_data['total_trades'],
                performance_data['winning_trades'],
                performance_data['losing_trades'],
                performance_data['total_pnl'],
                performance_data.get('win_rate'),
                performance_data.get('sharpe_ratio'),
                performance_data.get('max_drawdown')
            ))
            
            conn.commit()
    
    def update_system_status(self, status: str, error_message: str = None):
        """시스템 상태 업데이트"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO system_status (status, last_heartbeat, error_message)
                VALUES (?, CURRENT_TIMESTAMP, ?)
            """, (status, error_message))
            
            conn.commit()
    
    def get_latest_system_status(self) -> Optional[Dict[str, Any]]:
        """최신 시스템 상태 조회"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM system_status 
                ORDER BY created_at DESC 
                LIMIT 1
            """)
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def backup_database(self, backup_path: str = None):
        """데이터베이스 백업"""
        if backup_path is None:
            backup_path = f"{self.db_path}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        with self.get_connection() as conn:
            backup_conn = sqlite3.connect(backup_path)
            conn.backup(backup_conn)
            backup_conn.close()
        
        logger.log_system(f"Database backed up to {backup_path}")
    
    def save_token_log(self, event_type: str, token: str = None, 
                      issue_time: datetime = None, expire_time: datetime = None,
                      status: str = None, error_message: str = None):
        """토큰 관련 로그 저장"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO token_logs (
                    event_type, token, issue_time, expire_time, 
                    status, error_message
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                event_type,
                token,
                issue_time.isoformat() if issue_time else None,
                expire_time.isoformat() if expire_time else None,
                status,
                error_message
            ))
            
            conn.commit()
    
    def save_symbol_search_log(self, total_symbols: int, filtered_symbols: int,
                             search_criteria: Dict[str, Any], status: str,
                             error_message: str = None):
        """종목 탐색 로그 저장"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO symbol_search_logs (
                    search_time, total_symbols, filtered_symbols,
                    search_criteria, status, error_message
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().isoformat(),
                total_symbols,
                filtered_symbols,
                json.dumps(search_criteria),
                status,
                error_message
            ))
            
            conn.commit()
    
    def get_token_logs(self, start_date: str = None, end_date: str = None,
                      event_type: str = None) -> List[Dict[str, Any]]:
        """토큰 로그 조회"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM token_logs"
            params = []
            where_clauses = []
            
            if start_date:
                where_clauses.append("created_at >= ?")
                params.append(start_date)
            
            if end_date:
                where_clauses.append("created_at <= ?")
                params.append(end_date)
            
            if event_type:
                where_clauses.append("event_type = ?")
                params.append(event_type)
            
            if where_clauses:
                query += " WHERE " + " AND ".join(where_clauses)
            
            query += " ORDER BY created_at DESC"
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_latest_token(self) -> Optional[Dict[str, Any]]:
        """가장 최근에 발급된 유효한 토큰 조회"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                
                # 가장 최근에 성공적으로 발급된 토큰 조회
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
    
    def get_symbol_search_logs(self, start_date: str = None, 
                             end_date: str = None) -> List[Dict[str, Any]]:
        """종목 탐색 로그 조회"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM symbol_search_logs WHERE 1=1"
            params = []
            
            if start_date:
                query += " AND created_at >= ?"
                params.append(start_date)
            
            if end_date:
                query += " AND created_at <= ?"
                params.append(end_date)
            
            query += " ORDER BY created_at DESC"
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
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
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                INSERT INTO telegram_messages (
                    direction, chat_id, message_text, message_id, update_id,
                    is_command, command, processed, status, error_message, reply_to
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                direction,
                str(chat_id),
                message_text,
                message_id,
                update_id,
                1 if is_command else 0,
                command,
                1 if processed else 0,
                status,
                error_message,
                reply_to
            ))
            
            db_message_id = cursor.lastrowid
            conn.commit()
            return db_message_id
    
    def update_telegram_message(self, db_message_id: int, message_id: str = None, 
                              status: str = None, error_message: str = None):
        """텔레그램 메시지 업데이트 (outgoing 메시지의 실제 전송 결과를 업데이트)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            set_parts = []
            params = []
            
            if message_id is not None:
                set_parts.append("message_id = ?")
                params.append(message_id)
                
            if status is not None:
                set_parts.append("status = ?")
                params.append(status)
                
            if error_message is not None:
                set_parts.append("error_message = ?")
                params.append(error_message)
                
            if not set_parts:
                return  # 업데이트할 내용이 없음
                
            params.append(db_message_id)
            
            query = f"UPDATE telegram_messages SET {', '.join(set_parts)} WHERE id = ?"
            cursor.execute(query, params)
            
            conn.commit()
            
    def update_telegram_message_status(self, message_id: str, processed: bool = True, 
                                     status: str = "SUCCESS", error_message: str = None):
        """텔레그램 메시지 상태 업데이트"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE telegram_messages
                SET processed = ?, status = ?, error_message = ?
                WHERE message_id = ?
            """, (
                1 if processed else 0,
                status,
                error_message,
                message_id
            ))
            
            conn.commit()
    
    def get_telegram_messages(self, direction: str = None, chat_id: str = None,
                            is_command: bool = None, processed: bool = None,
                            start_date: str = None, end_date: str = None,
                            message_id: str = None, limit: int = 100) -> List[Dict[str, Any]]:
        """텔레그램 메시지 조회"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            query = "SELECT * FROM telegram_messages WHERE 1=1"
            params = []
            
            if direction:
                query += " AND direction = ?"
                params.append(direction)
            
            if chat_id:
                query += " AND chat_id = ?"
                params.append(str(chat_id))
            
            if message_id:
                query += " AND message_id = ?"
                params.append(message_id)
            
            if is_command is not None:
                query += " AND is_command = ?"
                params.append(1 if is_command else 0)
            
            if processed is not None:
                query += " AND processed = ?"
                params.append(1 if processed else 0)
            
            if start_date:
                query += " AND created_at >= ?"
                params.append(start_date)
            
            if end_date:
                query += " AND created_at <= ?"
                params.append(end_date)
            
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            
            cursor.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]
    
    def get_system_status(self) -> Dict[str, Any]:
        """현재 시스템 상태 조회"""
        status = self.get_latest_system_status()
        if not status:
            return {
                "status": "UNKNOWN",
                "updated_at": datetime.now().isoformat(),
                "error_message": None
            }
        
        return {
            "status": status["status"],
            "updated_at": status["created_at"],
            "error_message": status["error_message"]
        }

# 싱글톤 인스턴스 생성
db = Database()