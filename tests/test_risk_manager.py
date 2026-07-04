"""
tests/test_risk_manager.py
Tests for backend/risk_manager.py — position sizing, circuit-breaker, and order validation.
"""

import pytest
from backend.risk_manager import RiskManager
from backend.config import MAX_DRAWDOWN_PCT, MAX_RISK_PER_TRADE_PCT
from unittest.mock import patch


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def rm():
    """Fresh RiskManager with $10,000 starting balance."""
    return RiskManager(starting_balance=10_000.0)


# ── Position sizing ───────────────────────────────────────────────────────────

def test_position_size_basic(rm):
    """Qty should equal (equity × risk%) / risk_per_share."""
    qty = rm.calculate_position_size(entry_price=100.0, stop_loss_price=95.0, account_equity=10_000.0)
    expected = (10_000.0 * MAX_RISK_PER_TRADE_PCT) / (100.0 - 95.0)
    assert qty == pytest.approx(expected, rel=1e-4)


def test_position_size_scales_with_equity(rm):
    """Larger equity → larger position size (proportional)."""
    qty_small = rm.calculate_position_size(150.0, 145.0, 5_000.0)
    qty_large = rm.calculate_position_size(150.0, 145.0, 10_000.0)
    assert qty_large == pytest.approx(qty_small * 2, rel=1e-4)


def test_position_size_wider_stop_gives_smaller_qty(rm):
    """Wider stop-loss distance reduces position size."""
    qty_tight = rm.calculate_position_size(100.0, stop_loss_price=98.0, account_equity=10_000.0)
    qty_wide  = rm.calculate_position_size(100.0, stop_loss_price=90.0, account_equity=10_000.0)
    assert qty_tight > qty_wide


def test_position_size_returns_none_when_sl_above_entry(rm):
    """SL >= entry must be rejected with None."""
    result = rm.calculate_position_size(100.0, stop_loss_price=105.0, account_equity=10_000.0)
    assert result is None


def test_position_size_returns_none_when_sl_equals_entry(rm):
    result = rm.calculate_position_size(100.0, stop_loss_price=100.0, account_equity=10_000.0)
    assert result is None


def test_position_size_is_positive(rm):
    qty = rm.calculate_position_size(200.0, 195.0, 10_000.0)
    assert qty > 0


# ── Circuit-breaker / drawdown monitor ───────────────────────────────────────

def test_update_equity_returns_true_within_limit(rm):
    """A small equity drop (within limit) should return True."""
    ok = rm.update_equity(9_900.0)   # 1% drawdown on $10k
    assert ok is True


def test_update_equity_raises_flag_when_limit_breached(rm):
    """A drawdown exceeding MAX_DRAWDOWN_PCT should return False."""
    # Drive equity below high-water mark by more than the allowed %
    big_loss = 10_000.0 * (1 - MAX_DRAWDOWN_PCT - 0.01)
    ok = rm.update_equity(big_loss)
    assert ok is False


def test_update_equity_updates_high_water_mark(rm):
    """Equity rising above starting balance updates the high-water mark."""
    rm.update_equity(11_000.0)   # new ATH
    # A 1% drop from 11k should still be fine
    ok = rm.update_equity(10_890.0)
    assert ok is True


def test_update_equity_high_water_mark_does_not_go_back_down(rm):
    """The high-water mark should never decrease."""
    rm.update_equity(12_000.0)
    rm.update_equity(9_000.0)
    assert rm.high_water_mark == pytest.approx(12_000.0)


def test_circuit_breaker_relative_to_high_water_mark(rm):
    """
    Drawdown is measured from the HIGH-WATER mark, not starting balance.
    After reaching $12k, losing 16% of $12k should trigger the breaker.
    """
    rm.update_equity(12_000.0)
    drop = 12_000.0 * (1 - MAX_DRAWDOWN_PCT - 0.01)
    ok = rm.update_equity(drop)
    assert ok is False


# ── Default levels ────────────────────────────────────────────────────────────

def test_default_levels_sl_below_entry(rm):
    sl, tp = rm.default_levels(100.0)
    assert sl < 100.0


def test_default_levels_tp_above_entry(rm):
    sl, tp = rm.default_levels(100.0)
    assert tp > 100.0


def test_default_levels_positive_rrr(rm):
    """Take-profit distance must be greater than stop-loss distance."""
    entry = 100.0
    sl, tp = rm.default_levels(entry)
    assert (tp - entry) > (entry - sl)


# ── Order validation ──────────────────────────────────────────────────────────

@pytest.fixture
def valid_order_kwargs():
    return dict(
        symbol="AAPL",
        qty=10.0,
        entry_price=150.0,
        stop_loss_price=145.0,
        take_profit_price=165.0,
        free_cash=5_000.0,
    )


def test_validate_order_approves_valid_order(rm, valid_order_kwargs):
    with patch("backend.risk_manager.ALPACA_API_KEY", "real-key-abc"):
        ok, reason = rm.validate_order(**valid_order_kwargs)
    assert ok is True
    assert reason == ""


def test_validate_order_rejects_missing_api_key(rm, valid_order_kwargs):
    with patch("backend.risk_manager.ALPACA_API_KEY", "your_alpaca_key_here"):
        ok, reason = rm.validate_order(**valid_order_kwargs)
    assert ok is False
    assert "key" in reason.lower()


def test_validate_order_rejects_insufficient_cash(rm, valid_order_kwargs):
    """Order notional value (10 × $150 = $1 500) exceeds free_cash=$100."""
    with patch("backend.risk_manager.ALPACA_API_KEY", "real-key"):
        ok, reason = rm.validate_order(**{**valid_order_kwargs, "free_cash": 100.0})
    assert ok is False
    assert "cash" in reason.lower()


def test_validate_order_rejects_missing_stop_loss(rm, valid_order_kwargs):
    with patch("backend.risk_manager.ALPACA_API_KEY", "real-key"):
        ok, reason = rm.validate_order(**{**valid_order_kwargs, "stop_loss_price": None})
    assert ok is False
    assert "stop-loss" in reason.lower()


def test_validate_order_rejects_sl_above_entry(rm, valid_order_kwargs):
    with patch("backend.risk_manager.ALPACA_API_KEY", "real-key"):
        ok, reason = rm.validate_order(**{**valid_order_kwargs, "stop_loss_price": 160.0})
    assert ok is False
    assert "below" in reason.lower()


def test_validate_order_rejects_tp_below_entry(rm, valid_order_kwargs):
    with patch("backend.risk_manager.ALPACA_API_KEY", "real-key"):
        ok, reason = rm.validate_order(**{**valid_order_kwargs, "take_profit_price": 140.0})
    assert ok is False
    assert "above" in reason.lower()


def test_validate_order_accepts_none_take_profit(rm, valid_order_kwargs):
    """take_profit is optional — None should not trigger rejection."""
    with patch("backend.risk_manager.ALPACA_API_KEY", "real-key"):
        ok, reason = rm.validate_order(**{**valid_order_kwargs, "take_profit_price": None})
    assert ok is True
