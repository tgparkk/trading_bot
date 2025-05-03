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
    
    def _initialize(self):
        db_cfg = config.get("database", DatabaseConfig())
        self.db_path = db_cfg.db_path
        self.backup_interval = db_cfg.backup_interval
        self._initialize_db()
    
    @contextmanager
    def get_connection(self):
        """데이터베이스 연결 컨텍스트 매니저"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # 딕셔너리 형태로 결과 반환
        try:
            yield conn
        finally:
            conn.close()
    
    def _initialize_db(self):
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

# 싱글톤 인스턴스 생성
db = Database()