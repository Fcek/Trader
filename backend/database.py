import os
import threading
import urllib.request
import json
import sqlite3
import logging
from datetime import datetime
from backend.config import DB_FILE_PATH

logger = logging.getLogger("db")


def get_db_connection() -> sqlite3.Connection:
    """Returns a sqlite3 connection with Row factory enabled."""
    conn = sqlite3.connect(str(DB_FILE_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialises all tables if they do not already exist."""
    conn = get_db_connection()
    cur = conn.cursor()

    # 1. Trade log
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol                  TEXT    NOT NULL,
            qty                     REAL    NOT NULL,
            side                    TEXT    NOT NULL,
            entry_price             REAL    NOT NULL,
            exit_price              REAL,
            entry_time              TEXT    NOT NULL,
            exit_time               TEXT,
            status                  TEXT    NOT NULL DEFAULT 'OPEN',
            pnl                     REAL    DEFAULT 0.0,
            stop_loss               REAL,
            take_profit             REAL,
            alpaca_entry_order_id   TEXT,
            alpaca_exit_order_id    TEXT
        )
    """)

    # 2. Equity history (for performance chart)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS equity_history (
            timestamp       TEXT PRIMARY KEY,
            balance         REAL NOT NULL,
            equity          REAL NOT NULL,
            unrealized_pnl  REAL NOT NULL
        )
    """)

    # 3. System log (visible in dashboard)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS system_logs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT    NOT NULL,
            level     TEXT    NOT NULL,
            message   TEXT    NOT NULL
        )
    """)

    # 4. Key-value bot state store
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_state (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cur.execute("INSERT OR IGNORE INTO bot_state (key, value) VALUES ('bot_running', '0')")
    cur.execute("INSERT OR IGNORE INTO bot_state (key, value) VALUES ('active_strategy', 'EMA_Cross')")

    conn.commit()
    conn.close()
    logger.info(f"Database ready at {DB_FILE_PATH}")


# ── Logging helpers ───────────────────────────────────────────────────────────

import asyncio
from datetime import timezone

_log_callbacks = []

def register_log_callback(cb) -> None:
    _log_callbacks.append(cb)

def _send_discord_alert(msg: str) -> None:
    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return
    def _post():
        try:
            req = urllib.request.Request(webhook_url, method="POST")
            req.add_header("Content-Type", "application/json")
            data = json.dumps({"content": msg}).encode("utf-8")
            urllib.request.urlopen(req, data=data, timeout=5)
        except Exception as e:
            print(f"[Discord] Failed to send alert: {e}")
    threading.Thread(target=_post, daemon=True).start()

def add_log(level: str, message: str) -> None:
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        
        if level in ("ERROR", "CRITICAL"):
            _send_discord_alert(f"🚨 **{level}** 🚨\n```\n{message}\n```")
        elif "Order FILLED:" in message or "PnL:" in message or "Soft SL triggered" in message:
            _send_discord_alert(f"🔔 **TRADE UPDATE** 🔔\n```\n{message}\n```")

        conn = get_db_connection()
        conn.execute(
            "INSERT INTO system_logs (timestamp, level, message) VALUES (?, ?, ?)",
            (timestamp, level, message),
        )
        conn.commit()
        conn.close()
        
        # Broadcast to any registered callbacks (like WebSockets)
        log_data = {"level": level, "message": message, "timestamp": timestamp}
        for cb in _log_callbacks:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(cb(log_data))
            except RuntimeError:
                pass # No running event loop
    except Exception as exc:
        print(f"[DB log error] {exc}")


def get_logs(limit: int = 100):
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT id, timestamp, level, message FROM system_logs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_logs_by_days(days: int):
    conn = get_db_connection()
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT timestamp, level, message FROM system_logs WHERE timestamp >= ? ORDER BY timestamp DESC",
        (cutoff,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_logs() -> None:
    conn = get_db_connection()
    conn.execute("DELETE FROM system_logs")
    conn.commit()
    conn.close()


# ── Equity snapshot ───────────────────────────────────────────────────────────

def save_equity_snapshot(balance: float, equity: float, unrealized_pnl: float) -> None:
    conn = get_db_connection()
    conn.execute(
        "INSERT OR REPLACE INTO equity_history (timestamp, balance, equity, unrealized_pnl)"
        " VALUES (?, ?, ?, ?)",
        (datetime.now().isoformat(), balance, equity, unrealized_pnl),
    )
    conn.commit()
    conn.close()


def get_equity_history(limit: int = 5000, timeframe: str = "ALL"):
    conn = get_db_connection()
    from datetime import datetime, timedelta
    now = datetime.now()
    if timeframe == "1D":
        dt = now - timedelta(days=1)
    elif timeframe == "1W":
        dt = now - timedelta(days=7)
    elif timeframe == "1M":
        dt = now - timedelta(days=30)
    else:
        dt = datetime(1970, 1, 1)
        
    time_filter = dt.isoformat()
    
    rows = conn.execute(
        "SELECT timestamp, balance, equity, unrealized_pnl"
        " FROM equity_history WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
        (time_filter, limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in reversed(rows)]


# ── Bot state ─────────────────────────────────────────────────────────────────

def get_bot_state(key: str, default: str = None) -> str:
    conn = get_db_connection()
    row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_bot_state(key: str, value: str) -> None:
    conn = get_db_connection()
    conn.execute(
        "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, str(value))
    )
    conn.commit()
    conn.close()


# ── Trade CRUD ────────────────────────────────────────────────────────────────

def add_trade(
    symbol: str,
    qty: float,
    side: str,
    entry_price: float,
    stop_loss: float = None,
    take_profit: float = None,
    order_id: str = None,
) -> int:
    conn = get_db_connection()
    cur = conn.execute(
        """INSERT INTO trades
               (symbol, qty, side, entry_price, entry_time, status,
                stop_loss, take_profit, alpaca_entry_order_id)
           VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)""",
        (symbol, qty, side.lower(), entry_price, datetime.now().isoformat(),
         stop_loss, take_profit, order_id),
    )
    trade_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def update_trade_exit(trade_id: int, exit_price: float, pnl: float, order_id: str = None) -> None:
    conn = get_db_connection()
    conn.execute(
        """UPDATE trades
           SET exit_price = ?, exit_time = ?, status = 'CLOSED',
               pnl = ?, alpaca_exit_order_id = ?
           WHERE id = ?""",
        (exit_price, datetime.now().isoformat(), pnl, order_id, trade_id),
    )
    conn.commit()
    conn.close()


def update_trade_stop_loss(trade_id: int, new_stop_loss: float) -> None:
    conn = get_db_connection()
    conn.execute(
        "UPDATE trades SET stop_loss = ? WHERE id = ?",
        (new_stop_loss, trade_id),
    )
    conn.commit()
    conn.close()


def get_open_trades():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM trades WHERE status = 'OPEN'").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_closed_trades(limit: int = 100):
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT * FROM trades WHERE status = 'CLOSED' ORDER BY exit_time DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
