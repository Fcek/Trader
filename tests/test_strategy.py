"""
tests/test_strategy.py
Tests for backend/strategy.py — signal generation and edge cases.

We build synthetic OHLCV bar lists to precisely control when crossovers occur,
making tests fully deterministic and independent of real market data.
"""

import pytest
from typing import List, Dict, Any
from backend.strategy import EMACrossStrategy, BaseStrategy


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_bars(prices: List[float]) -> List[Dict[str, Any]]:
    """
    Build minimal Alpaca-style bar dicts from a list of close prices.
    Open/High/Low are derived from close so ATR is well-defined.
    """
    bars = []
    for i, p in enumerate(prices):
        bars.append({
            "t": f"2024-01-{i+1:02d}T00:00:00Z",
            "o": str(p * 0.99),
            "h": str(p * 1.005),
            "l": str(p * 0.995),
            "c": str(p),
            "v": str(1000000 + i * 10000),
        })
    return bars


def trending_up_bars(n: int = 250, base: float = 100.0, slope: float = 0.3) -> List[Dict[str, Any]]:
    """Steadily rising prices — favourable for a BUY signal."""
    prices = [base + i * slope for i in range(n)]
    return make_bars(prices)


def flat_bars(n: int = 250, price: float = 100.0) -> List[Dict[str, Any]]:
    """Completely flat prices — EMAs converge, no crossover."""
    return make_bars([price] * n)


def bullish_cross_bars(n_before: int = 220, n_after: int = 30) -> List[Dict[str, Any]]:
    """
    Simulate a bullish EMA cross:
      Phase 1 (before): declining prices so short EMA < long EMA.
      Phase 2 (after):  sharply rising prices so short EMA crosses above long EMA.
    """
    declining = [100.0 - i * 0.1 for i in range(n_before)]
    rising    = [declining[-1] + i * 1.5 for i in range(1, n_after + 1)]
    return make_bars(declining + rising)


def bearish_cross_bars(n_before: int = 220, n_after: int = 30) -> List[Dict[str, Any]]:
    """
    Simulate a bearish EMA cross:
      Phase 1: rising prices so short EMA > long EMA.
      Phase 2: sharp drop so short EMA crosses below long EMA.
    """
    rising   = [100.0 + i * 0.1 for i in range(n_before)]
    dropping = [rising[-1] - i * 1.5 for i in range(1, n_after + 1)]
    return make_bars(rising + dropping)


# ── BaseStrategy ──────────────────────────────────────────────────────────────

def test_base_strategy_raises_not_implemented():
    strat = BaseStrategy("test")
    with pytest.raises(NotImplementedError):
        strat.generate_signal([])


# ── Insufficient data ─────────────────────────────────────────────────────────

def test_hold_on_empty_bars():
    strat = EMACrossStrategy()
    result = strat.generate_signal([])
    assert result["signal"] == "HOLD"


def test_hold_on_too_few_bars():
    strat = EMACrossStrategy()
    result = strat.generate_signal(make_bars([100.0] * 10))
    assert result["signal"] == "HOLD"


def test_hold_requires_at_least_trend_window_bars():
    strat = EMACrossStrategy(trend_window=200)
    # 210 bars should just barely be enough (200 + atr_window buffer)
    result = strat.generate_signal(make_bars([100.0] * 150))
    assert result["signal"] == "HOLD"
    assert "Insufficient" in result["reason"]


# ── Signal structure ──────────────────────────────────────────────────────────

def test_signal_dict_has_required_keys():
    strat = EMACrossStrategy()
    result = strat.generate_signal(flat_bars())
    for key in ("signal", "stop_loss", "trail_amount", "reason"):
        assert key in result


def test_signal_value_is_valid_enum():
    strat = EMACrossStrategy()
    result = strat.generate_signal(flat_bars())
    assert result["signal"] in ("BUY", "SELL", "HOLD")


# ── BUY signal ────────────────────────────────────────────────────────────────

def test_buy_signal_has_stop_loss_and_trail_amount():
    """On a confirmed BUY, SL and trail_amount must both be set."""
    strat = EMACrossStrategy(rsi_max=100.0)
    bars = bullish_cross_bars()
    result = strat.generate_signal(bars)
    if result["signal"] == "BUY":
        assert result["stop_loss"] is not None
        assert result["trail_amount"] is not None


def test_buy_stop_loss_below_take_profit():
    strat = EMACrossStrategy(rsi_max=100.0)
    bars = bullish_cross_bars()
    result = strat.generate_signal(bars)
    if result["signal"] == "BUY":
        assert result["stop_loss"] < result["take_profit"]


def test_buy_stop_loss_is_positive():
    strat = EMACrossStrategy(rsi_max=100.0)
    bars = bullish_cross_bars()
    result = strat.generate_signal(bars)
    if result["signal"] == "BUY":
        assert result["stop_loss"] > 0





# ── SELL signal ───────────────────────────────────────────────────────────────

def test_sell_signal_reason_is_non_empty():
    strat = EMACrossStrategy()
    bars = bearish_cross_bars()
    result = strat.generate_signal(bars)
    if result["signal"] == "SELL":
        assert len(result["reason"]) > 0


def test_sell_signal_has_no_sl_or_trail_amount():
    """A SELL exit signal doesn't carry SL/trail_amount — those are managed by the broker's bracket."""
    strat = EMACrossStrategy()
    bars = bearish_cross_bars()
    result = strat.generate_signal(bars)
    if result["signal"] == "SELL":
        assert result["stop_loss"] is None
        assert result["trail_amount"] is None


# ── HOLD signal ───────────────────────────────────────────────────────────────

def test_hold_on_flat_prices():
    strat = EMACrossStrategy()
    result = strat.generate_signal(flat_bars())
    assert result["signal"] == "HOLD"


def test_hold_reason_is_non_empty():
    strat = EMACrossStrategy()
    result = strat.generate_signal(flat_bars())
    assert len(result["reason"]) > 0


# ── Determinism ───────────────────────────────────────────────────────────────

def test_identical_inputs_produce_identical_outputs():
    """Strategy must be pure — same bars always return same signal."""
    strat = EMACrossStrategy()
    bars = flat_bars()
    result1 = strat.generate_signal(bars)
    result2 = strat.generate_signal(bars)
    assert result1 == result2


# ── Custom parameters ─────────────────────────────────────────────────────────

def test_custom_windows_accepted():
    """Strategy should be constructable with non-default parameters."""
    strat = EMACrossStrategy(short_window=5, long_window=13, trend_window=50, atr_window=10)
    assert strat.short_window == 5
    assert strat.long_window == 13
    assert strat.trend_window == 50


def test_strategy_name_is_ema_cross():
    strat = EMACrossStrategy()
    assert strat.name == "EMA_Cross"
