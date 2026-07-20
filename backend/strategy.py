"""
strategy.py – Pluggable strategy system.

Each strategy must inherit from BaseStrategy and implement generate_signal().
The signal dict contract:
    {
        "signal":           "BUY" | "SELL" | "HOLD",
        "stop_loss":        float | None,
        "activation_price": float | None,
        "trail_amount":     float | None,
        "reason":           str,
    }
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from backend.config import ALLOW_SHORT_SELLING

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
        volume_window: int = 20,
        volume_multiplier: float = 1.0,
        rsi_window: int = 14,
        rsi_max: float = 70.0,
        risk_multiplier: float = 2.5,
    ) -> None:
        super().__init__("EMA_Cross")
        self.short_window = short_window
        self.long_window = long_window
        self.trend_window = trend_window
        self.atr_window = atr_window
        self.volume_window = volume_window
        self.volume_multiplier = volume_multiplier
        self.rsi_window = rsi_window
        self.rsi_max = rsi_max
        self.risk_multiplier = risk_multiplier

    def generate_signal(
        self,
        bars: List[Dict[str, Any]],
        market_bars: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        _hold = {"signal": "HOLD", "stop_loss": None, "activation_price": None, "trail_amount": None, "reason": "", "metrics": None}

        min_bars = max(self.trend_window, self.long_window, self.volume_window, self.rsi_window) + self.atr_window + 5
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
        df["vol_sma"] = df["volume"].rolling(window=self.volume_window).mean()

        # RSI
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).ewm(alpha=1/self.rsi_window, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/self.rsi_window, adjust=False).mean()
        # Replace 0 with a tiny number to prevent divide-by-zero warnings
        rs = gain / loss.replace(0, 1e-10)
        df["rsi"] = 100 - (100 / (1 + rs))

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
        high_volume   = float(last["volume"]) > (float(last["vol_sma"]) * self.volume_multiplier)
        rsi_ok        = float(last["rsi"]) < self.rsi_max

        macro_ok = True
        macro_ok_short = True
        if market_bars and len(market_bars) >= self.trend_window:
            mdf = pd.DataFrame(market_bars)
            mdf = mdf.rename(columns={"c": "close"})
            mdf["close"] = pd.to_numeric(mdf["close"])
            mdf["ema_t"] = mdf["close"].ewm(span=self.trend_window, adjust=False).mean()
            market_price = float(mdf.iloc[-1]["close"])
            market_ema_t = float(mdf.iloc[-1]["ema_t"])
            macro_ok = market_price > market_ema_t
            macro_ok_short = market_price < market_ema_t

        metrics = {
            "price": price,
            "ema_s": float(last["ema_s"]),
            "ema_l": float(last["ema_l"]),
            "ema_t": float(last["ema_t"]),
            "vol": float(last["volume"]),
            "vol_sma": float(last["vol_sma"]),
            "rsi": float(last["rsi"]),
            "atr": atr,
            "macro_ok": macro_ok,
            "macro_ok_short": macro_ok_short,
        }

        if bullish_cross and above_trend and high_volume and rsi_ok and macro_ok:
            risk_amount = round(atr * self.risk_multiplier, 2)
            sl = round(price - risk_amount, 2)
            activation = round(price + (risk_amount * 2), 2)
            return {
                "signal": "BUY",
                "stop_loss": sl,
                "activation_price": activation,
                "trail_amount": risk_amount,
                "reason": (
                    f"Bullish EMA{self.short_window}/{self.long_window} cross "
                    f"above EMA{self.trend_window} with volume and RSI confirmation. Risk={risk_amount}, SL={sl}, Activate={activation}, Trail={risk_amount}"
                ),
                "metrics": metrics
            }

        below_trend = price < float(last["ema_t"])
        rsi_ok_short = float(last["rsi"]) > (100 - self.rsi_max)
        if bearish_cross and below_trend and high_volume and rsi_ok_short and macro_ok_short and ALLOW_SHORT_SELLING:
            risk_amount = round(atr * self.risk_multiplier, 2)
            sl = round(price + risk_amount, 2)
            activation = round(price - (risk_amount * 2), 2)
            return {
                "signal": "SELL_SHORT",
                "stop_loss": sl,
                "activation_price": activation,
                "trail_amount": risk_amount,
                "reason": (
                    f"Bearish EMA{self.short_window}/{self.long_window} cross "
                    f"below EMA{self.trend_window} with volume and RSI confirmation. Risk={risk_amount}, SL={sl}, Activate={activation}, Trail={risk_amount}"
                ),
                "metrics": metrics
            }

        if bearish_cross:
            return {
                "signal": "SELL",
                "stop_loss": None,
                "activation_price": None,
                "trail_amount": None,
                "reason": f"Bearish EMA{self.short_window}/{self.long_window} cross – exit long signal.",
                "metrics": metrics
            }

        if bullish_cross and ALLOW_SHORT_SELLING:
            return {
                "signal": "COVER",
                "stop_loss": None,
                "activation_price": None,
                "trail_amount": None,
                "reason": f"Bullish EMA{self.short_window}/{self.long_window} cross – exit short signal.",
                "metrics": metrics
            }

        _hold["reason"] = "No crossover – holding."
        _hold["metrics"] = metrics
        return _hold
