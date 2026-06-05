"""
持久化存储 — SQLite 单文件方案
===============================
参考 JoinQuant EmuTrader / BigQuant user_store 模式设计。

表结构:
  account     — 当前资金状态 (key-value)
  positions   — 当前持仓
  orders      — 成交日志 (追加)
  nav_series  — 净值序列 (追加)
  signals     — 信号历史 (追加)

用法:
  store = PaperStore('data/paper_state.db')
  store.init_db()
  store.save_account({'cash': 500000, ...})
  store.save_positions({'000001.SZ': PositionInfo(...)})
  store.flush()
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS account (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS positions (
    stockcode TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL DEFAULT 0,
    available INTEGER NOT NULL DEFAULT 0,
    avg_cost REAL NOT NULL DEFAULT 0.0,
    market_value REAL NOT NULL DEFAULT 0.0,
    unrealized_pnl REAL NOT NULL DEFAULT 0.0,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    trade_time TEXT NOT NULL,
    stockcode TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL DEFAULT 0.0,
    commission REAL DEFAULT 0.0,
    stamp_duty REAL DEFAULT 0.0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS nav_series (
    date TEXT PRIMARY KEY,
    nav REAL NOT NULL,
    total_value REAL NOT NULL,
    cash REAL NOT NULL,
    position_count INTEGER DEFAULT 0,
    daily_return REAL DEFAULT 0.0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date TEXT NOT NULL,
    rebalance_date TEXT NOT NULL,
    target_count INTEGER DEFAULT 0,
    targets TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);
"""


class PaperStore:
    """SQLite 持久化存储，线程安全。"""

    def __init__(self, db_path: str = 'data/paper_state.db'):
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    # ── 生命周期 ──────────────────────────────────────

    def init_db(self) -> None:
        """初始化数据库和表（幂等）。"""
        import os
        os.makedirs(os.path.dirname(self._db_path) or '.', exist_ok=True)
        with self._lock:
            conn = self._get_conn()
            conn.executescript(CREATE_TABLES_SQL)
            conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    def flush(self) -> None:
        """强制提交所有待写入数据。"""
        with self._lock:
            if self._conn:
                self._conn.commit()

    # ── 账户 ──────────────────────────────────────────

    def save_account(self, data: Dict[str, float]) -> None:
        """写入账户状态。data: {cash: ..., total_value: ..., initial_capital: ...}"""
        with self._lock:
            conn = self._get_conn()
            now = datetime.now().isoformat()
            for key, value in data.items():
                conn.execute(
                    "INSERT OR REPLACE INTO account (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, float(value), now),
                )
            conn.commit()

    def get_account(self) -> Dict[str, float]:
        conn = self._get_conn()
        rows = conn.execute("SELECT key, value FROM account").fetchall()
        return {row[0]: row[1] for row in rows}

    def get_initial_capital(self) -> float:
        row = self._get_conn().execute(
            "SELECT value FROM account WHERE key='initial_capital'"
        ).fetchone()
        return float(row[0]) if row else 0.0

    # ── 持仓 ──────────────────────────────────────────

    def save_positions(self, positions: dict) -> None:
        """
        写入当前持仓（全量替换）。
        positions: {stockcode: {quantity, available, avg_cost, market_value, unrealized_pnl}}
        """
        with self._lock:
            conn = self._get_conn()
            now = datetime.now().isoformat()
            conn.execute("DELETE FROM positions")
            for code, p in positions.items():
                conn.execute(
                    "INSERT INTO positions (stockcode, quantity, available, "
                    "avg_cost, market_value, unrealized_pnl, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (code,
                     int(p.get('quantity', 0)),
                     int(p.get('available', 0)),
                     float(p.get('avg_cost', 0)),
                     float(p.get('market_value', 0)),
                     float(p.get('unrealized_pnl', 0)),
                     now),
                )
            conn.commit()

    def get_positions(self) -> Dict[str, dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT stockcode, quantity, available, avg_cost, "
            "market_value, unrealized_pnl FROM positions"
        ).fetchall()
        return {
            row[0]: {
                'stockcode': row[0],
                'quantity': row[1],
                'available': row[2],
                'avg_cost': row[3],
                'market_value': row[4],
                'unrealized_pnl': row[5],
            }
            for row in rows
        }

    def position_count(self) -> int:
        row = self._get_conn().execute("SELECT COUNT(*) FROM positions").fetchone()
        return row[0] if row else 0

    # ── 订单（追加） ──────────────────────────────────

    def append_orders(self, orders: list) -> None:
        """追加成交记录。"""
        if not orders:
            return
        with self._lock:
            conn = self._get_conn()
            for o in orders:
                # 解析日期时间
                time_str = o.get('time', o.get('trade_time', ''))
                parts = time_str.split(' ') if time_str else ['', '']
                trade_date = parts[0][:8] if len(parts) > 0 else ''
                trade_time = parts[1][:8] if len(parts) > 1 else time_str

                conn.execute(
                    "INSERT INTO orders (trade_date, trade_time, stockcode, "
                    "side, quantity, price, amount) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (trade_date, trade_time,
                     str(o.get('stockcode', '')),
                     str(o.get('side', 'BUY')),
                     int(o.get('quantity', 0)),
                     float(o.get('price', 0)),
                     float(o.get('quantity', 0)) * float(o.get('price', 0))),
                )
            conn.commit()

    def get_orders(self, limit: int = 200,
                   date_from: str = None, date_to: str = None) -> list:
        query = "SELECT trade_date, trade_time, stockcode, side, quantity, price FROM orders"
        conditions = []
        params = []
        if date_from:
            conditions.append("trade_date >= ?")
            params.append(date_from)
        if date_to:
            conditions.append("trade_date <= ?")
            params.append(date_to)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)

        rows = self._get_conn().execute(query, params).fetchall()
        return [
            {'time': f'{r[0]} {r[1]}' if r[1] else r[0],
             'stockcode': r[2], 'side': r[3],
             'quantity': r[4], 'price': r[5]}
            for r in rows
        ]

    # ── 净值序列（追加） ──────────────────────────────

    def append_nav(self, record: dict) -> None:
        """
        追加一条净值记录（每天一条，同日期覆盖）。
        record: {date, nav, total_value, cash, position_count, daily_return}
        """
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO nav_series "
                "(date, nav, total_value, cash, position_count, daily_return) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (str(record.get('date', '')),
                 float(record.get('nav', 0)),
                 float(record.get('total_value', 0)),
                 float(record.get('cash', 0)),
                 int(record.get('position_count', 0)),
                 float(record.get('daily_return', 0))),
            )
            conn.commit()

    def get_nav_series(self) -> list:
        rows = self._get_conn().execute(
            "SELECT date, nav, total_value, cash, position_count, daily_return "
            "FROM nav_series ORDER BY date"
        ).fetchall()
        return [
            {'date': r[0], 'nav': r[1], 'total_value': r[2],
             'cash': r[3], 'position_count': r[4], 'daily_return': r[5]}
            for r in rows
        ]

    def nav_count(self) -> int:
        row = self._get_conn().execute("SELECT COUNT(*) FROM nav_series").fetchone()
        return row[0] if row else 0

    # ── 信号 ──────────────────────────────────────────

    def save_signal(self, signal_date: str, rebalance_date: str,
                    targets: List[str]) -> None:
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO signals (signal_date, rebalance_date, target_count, targets) "
                "VALUES (?, ?, ?, ?)",
                (signal_date, rebalance_date, len(targets), json.dumps(targets)),
            )
            conn.commit()

    def get_latest_signal(self) -> Optional[dict]:
        row = self._get_conn().execute(
            "SELECT signal_date, rebalance_date, targets, target_count "
            "FROM signals ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return {
            'signal_date': row[0], 'rebalance_date': row[1],
            'targets': json.loads(row[2]), 'target_count': row[3],
        }

    # ── 整体状态加载 ──────────────────────────────────

    def load_state(self) -> dict:
        """加载完整状态，返回 engine.report 格式的 dict。"""
        account = self.get_account()
        positions = self.get_positions()
        nav = self.get_nav_series()
        orders = self.get_orders(limit=500)
        latest_signal = self.get_latest_signal()

        return {
            'account': account,
            'positions': positions,
            'nav_series': nav,
            'orders': orders,
            'latest_signal': latest_signal,
            'has_positions': len(positions) > 0,
        }

    # ── 内部 ──────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        return self._conn
