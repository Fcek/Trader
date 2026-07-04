"""
tests/test_config.py
Tests for backend/config.py — environment loading and constant types.
"""

import os
import importlib
from pathlib import Path


def test_config_constants_have_correct_types():
    """All exported constants must be the expected Python type."""
    from backend import config
    assert isinstance(config.ALPACA_API_KEY, str)
    assert isinstance(config.ALPACA_API_SECRET, str)
    assert isinstance(config.ALPACA_PAPER_TRADING, bool)
    assert isinstance(config.PORT, int)
    assert isinstance(config.HOST, str)
    assert isinstance(config.DB_FILE_PATH, Path)
    assert isinstance(config.BOT_RECOVERY_CODE, str)


def test_safety_constants_are_positive_fractions():
    """Risk percentages must be > 0 and < 1."""
    from backend import config
    assert 0 < config.MAX_DRAWDOWN_PCT < 1
    assert 0 < config.MAX_RISK_PER_TRADE_PCT < 1
    assert 0 < config.DEFAULT_STOP_LOSS_PCT < 1
    assert 0 < config.DEFAULT_TAKE_PROFIT_PCT < 1


def test_take_profit_exceeds_stop_loss():
    """Take-profit percentage must be greater than stop-loss percentage (maintains positive RRR)."""
    from backend import config
    assert config.DEFAULT_TAKE_PROFIT_PCT > config.DEFAULT_STOP_LOSS_PCT


def test_port_is_valid():
    """Port must be in the valid TCP range."""
    from backend import config
    assert 1024 <= config.PORT <= 65535


def test_env_override_via_monkeypatch(monkeypatch):
    """Config values should be overridable via environment variables."""
    monkeypatch.setenv("ALPACA_PAPER_TRADING", "False")
    monkeypatch.setenv("PORT", "9999")
    # Force module reload to pick up new env values
    import backend.config as cfg
    importlib.reload(cfg)
    assert cfg.PORT == 9999
    assert cfg.ALPACA_PAPER_TRADING is False
    # Restore
    importlib.reload(cfg)
