"""
tests/test_database.py
Tests for backend/database.py — SQLite schema, CRUD helpers, and state store.
All tests use a temporary in-memory/file database so they never touch the
production trader.db.
"""

import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import patch


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """
    Redirect DB_FILE_PATH to a fresh temporary file for every test.
    This ensures tests are fully isolated from each other and from production.
    """
    db_file = tmp_path / "test_trader.db"
    monkeypatch.setattr("backend.config.DB_FILE_PATH", db_file)
    monkeypatch.setattr("backend.database.DB_FILE_PATH", db_file)
    yield db_file


# ── init_db ──────────────────────────────────────────────────────────────────

def test_init_db_creates_all_tables(temp_db):
    """init_db() should create all four expected tables."""
    from backend.database import init_db, get_db_connection
    init_db()

    conn = get_db_connection()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()

    assert "trades" in tables
    assert "equity_history" in tables
    assert "system_logs" in tables
    assert "bot_state" in tables


def test_init_db_is_idempotent(temp_db):
    """Calling init_db() twice must not raise or duplicate default rows."""
    from backend.database import init_db, get_bot_state
    init_db()
    init_db()  # second call — must be safe
    val = get_bot_state("bot_running")
    assert val == "0"


def test_init_db_seeds_bot_state(temp_db):
    """Default bot_state rows are inserted on first init."""
    from backend.database import init_db, get_bot_state
    init_db()
    assert get_bot_state("bot_running") == "0"
    assert get_bot_state("active_strategy") == "EMA_Cross"


# ── Logging helpers ───────────────────────────────────────────────────────────

def test_add_log_and_get_logs(temp_db):
    from backend.database import init_db, add_log, get_logs
    init_db()
    add_log("INFO", "hello world")
    logs = get_logs()
    assert len(logs) == 1
    assert logs[0]["level"] == "INFO"
    assert logs[0]["message"] == "hello world"


def test_get_logs_respects_limit(temp_db):
    from backend.database import init_db, add_log, get_logs
    init_db()
    for i in range(10):
        add_log("DEBUG", f"msg {i}")
    logs = get_logs(limit=3)
    assert len(logs) == 3


def test_get_logs_returns_newest_first(temp_db):
    from backend.database import init_db, add_log, get_logs
    init_db()
    add_log("INFO", "first")
    add_log("INFO", "second")
    logs = get_logs()
    assert logs[0]["message"] == "second"


def test_clear_logs(temp_db):
    from backend.database import init_db, add_log, get_logs, clear_logs
    init_db()
    add_log("INFO", "to be cleared")
    clear_logs()
    assert get_logs() == []


# ── Equity history ────────────────────────────────────────────────────────────

def test_save_and_get_equity_history(temp_db):
    from backend.database import init_db, save_equity_snapshot, get_equity_history
    init_db()
    save_equity_snapshot(balance=9500.0, equity=10100.0, unrealized_pnl=600.0)
    history = get_equity_history()
    assert len(history) == 1
    assert history[0]["balance"] == pytest.approx(9500.0)
    assert history[0]["equity"] == pytest.approx(10100.0)
    assert history[0]["unrealized_pnl"] == pytest.approx(600.0)


# ── Bot state ─────────────────────────────────────────────────────────────────

def test_set_and_get_bot_state(temp_db):
    from backend.database import init_db, set_bot_state, get_bot_state
    init_db()
    set_bot_state("my_key", "my_value")
    assert get_bot_state("my_key") == "my_value"


def test_get_bot_state_returns_default_for_missing_key(temp_db):
    from backend.database import init_db, get_bot_state
    init_db()
    result = get_bot_state("nonexistent_key", default="fallback")
    assert result == "fallback"


def test_set_bot_state_overwrites_existing(temp_db):
    from backend.database import init_db, set_bot_state, get_bot_state
    init_db()
    set_bot_state("bot_running", "1")
    set_bot_state("bot_running", "0")
    assert get_bot_state("bot_running") == "0"


# ── Trade CRUD ────────────────────────────────────────────────────────────────

def test_add_trade_returns_integer_id(temp_db):
    from backend.database import init_db, add_trade
    init_db()
    trade_id = add_trade("AAPL", qty=5.0, side="buy", entry_price=150.0)
    assert isinstance(trade_id, int)
    assert trade_id > 0


def test_get_open_trades_includes_new_trade(temp_db):
    from backend.database import init_db, add_trade, get_open_trades
    init_db()
    add_trade("MSFT", qty=2.0, side="buy", entry_price=300.0, stop_loss=290.0, take_profit=330.0)
    trades = get_open_trades()
    assert len(trades) == 1
    t = trades[0]
    assert t["symbol"] == "MSFT"
    assert t["status"] == "OPEN"
    assert t["stop_loss"] == pytest.approx(290.0)
    assert t["take_profit"] == pytest.approx(330.0)


def test_update_trade_exit_closes_trade(temp_db):
    from backend.database import init_db, add_trade, update_trade_exit, get_open_trades, get_closed_trades
    init_db()
    tid = add_trade("NVDA", qty=1.0, side="buy", entry_price=500.0)
    update_trade_exit(tid, exit_price=550.0, pnl=50.0, order_id="order-abc")

    assert get_open_trades() == []
    closed = get_closed_trades()
    assert len(closed) == 1
    assert closed[0]["pnl"] == pytest.approx(50.0)
    assert closed[0]["exit_price"] == pytest.approx(550.0)
    assert closed[0]["status"] == "CLOSED"


def test_multiple_trades_tracked_independently(temp_db):
    from backend.database import init_db, add_trade, get_open_trades
    init_db()
    add_trade("AAPL", 1.0, "buy", 150.0)
    add_trade("GOOG", 2.0, "buy", 2800.0)
    trades = get_open_trades()
    assert len(trades) == 2
    symbols = {t["symbol"] for t in trades}
    assert symbols == {"AAPL", "GOOG"}


def test_get_closed_trades_respects_limit(temp_db):
    from backend.database import init_db, add_trade, update_trade_exit, get_closed_trades
    init_db()
    for i in range(5):
        tid = add_trade(f"SYM{i}", 1.0, "buy", 100.0)
        update_trade_exit(tid, exit_price=110.0, pnl=10.0)
    closed = get_closed_trades(limit=3)
    assert len(closed) == 3
