"""
strategy.py – Pluggable strategy system.

Each strategy must inherit from BaseStrategy and implement generate_signal().
The signal dict contract:
    {
        "signal":      "BUY" | "SELL" | "HOLD",
        "stop_loss":   float | None,
        "take_profit": float | None,
        "reason":      str,
    }
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("strategy")


class BaseStrategy:
    def __init__(self, name: str) -> None:
        self.name = name

    def generate_signal(self, bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        raise NotImplementedError


class EMACrossStrategy(BaseStrategy):
    """
    Swing-trading trend-following strategy:

    Entry  – bullish EMA cross (short EMA crosses above long EMA) while
             price is above the 200-day EMA (macro uptrend filter).
    Exit   – bearish EMA cross (short EMA crosses below long EMA).
    SL/TP  – dynamically sized using ATR × risk_multiplier (default 2×)
             with a 1:3 risk-reward ratio for take-profit.
    """

    def __init__(
        self,
        short_window: int = 9,
        long_window: int = 21,
        trend_window: int = 200,
        atr_window: int = 14,
        risk_multiplier: float = 2.0,
    ) -> None:
        super().__init__("EMA_Cross")
        self.short_window = short_window
        self.long_window = long_window
        self.trend_window = trend_window
        self.atr_window = atr_window
        self.risk_multiplier = risk_multiplier

    def generate_signal(self, bars: List[Dict[str, Any]]) -> Dict[str, Any]:
        _hold = {"signal": "HOLD", "stop_loss": None, "take_profit": None, "reason": ""}

        min_bars = max(self.trend_window, self.long_window) + self.atr_window + 5
        if not bars or len(bars) < min_bars:
            _hold["reason"] = f"Insufficient data ({len(bars)} bars, need {min_bars})"
            return _hold

        # ── Build DataFrame ──────────────────────────────────────────────────
        df = pd.DataFrame(bars)
        # Alpaca returns keys: t, o, h, l, c, v
        df = df.rename(columns={"t": "time", "o": "open", "h": "high", "l": "low",
                                  "c": "close", "v": "volume"})
        for col in ("open", "high", "low", "close"):
            df[col] = pd.to_numeric(df[col])

        # ── Indicators ───────────────────────────────────────────────────────
        df["ema_s"] = df["close"].ewm(span=self.short_window, adjust=False).mean()
        df["ema_l"] = df["close"].ewm(span=self.long_window,  adjust=False).mean()
        df["ema_t"] = df["close"].ewm(span=self.trend_window, adjust=False).mean()

        # ATR
        hl  = df["high"] - df["low"]
        hpc = (df["high"] - df["close"].shift()).abs()
        lpc = (df["low"]  - df["close"].shift()).abs()
        df["atr"] = pd.concat([hl, hpc, lpc], axis=1).max(axis=1).ewm(
            span=self.atr_window, adjust=False
        ).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(last["close"])
        atr   = float(last["atr"])

        # ── Signal logic ─────────────────────────────────────────────────────
        bullish_cross = (prev["ema_s"] <= prev["ema_l"]) and (last["ema_s"] > last["ema_l"])
        bearish_cross = (prev["ema_s"] >= prev["ema_l"]) and (last["ema_s"] < last["ema_l"])
        above_trend   = price > float(last["ema_t"])

        if bullish_cross and above_trend:
            sl = round(price - atr * self.risk_multiplier, 2)
            tp = round(price + atr * self.risk_multiplier * 3, 2)
            return {
                "signal": "BUY",
                "stop_loss": sl,
                "take_profit": tp,
                "reason": (
                    f"Bullish EMA{self.short_window}/{self.long_window} cross "
                    f"above EMA{self.trend_window}. ATR SL={sl}, TP={tp}"
                ),
            }

        if bearish_cross:
            return {
                "signal": "SELL",
                "stop_loss": None,
                "take_profit": None,
                "reason": f"Bearish EMA{self.short_window}/{self.long_window} cross – exit signal.",
            }

        _hold["reason"] = "No crossover – holding."
        return _hold
