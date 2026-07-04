import logging
from typing import Optional, Tuple, Dict, Any
from backend.config import (
    MAX_DRAWDOWN_PCT,
    MAX_RISK_PER_TRADE_PCT,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_TAKE_PROFIT_PCT,
)
from backend.database import add_log

logger = logging.getLogger("risk_manager")


class RiskManager:
    """
    The bot's financial circuit breaker. Every proposed order must pass
    through this class before it is sent to the broker.

    Responsibilities:
      - Calculate safe position size (qty) based on fixed-fraction risk.
      - Enforce a hard daily-max-drawdown limit (kills the bot if breached).
      - Reject orders that would violate margin or risk constraints.
    """

    def __init__(self, starting_balance: float):
        self.starting_balance = starting_balance
        self.high_water_mark = starting_balance   # best equity we've seen today
        self.current_equity = starting_balance

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def update_equity(self, equity: float) -> bool:
        """
        Called periodically with the latest account equity.

        Returns:
            True  – equity is within acceptable drawdown.
            False – daily drawdown limit breached; bot must stop.
        """
        self.current_equity = equity
        if equity > self.high_water_mark:
            self.high_water_mark = equity

        drawdown = self._current_drawdown_pct()
        if drawdown >= MAX_DRAWDOWN_PCT:
            msg = (
                f"⛔ CIRCUIT BREAKER: Daily drawdown {drawdown:.1%} exceeded "
                f"limit of {MAX_DRAWDOWN_PCT:.1%}. Bot halted."
            )
            add_log("CRITICAL", msg)
            logger.critical(msg)
            return False
        return True

    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss_price: float,
        account_equity: float,
    ) -> Optional[float]:
        """
        Fixed-fractional position sizing.

        Risk per trade = equity × MAX_RISK_PER_TRADE_PCT
        Risk per share = entry_price − stop_loss_price
        Qty            = risk_per_trade / risk_per_share

        Returns the number of shares (float, Alpaca supports fractional
        shares for many symbols) or None if the order would be rejected.
        """
        if stop_loss_price >= entry_price:
            add_log("WARNING", "Invalid stop-loss: SL must be below entry price.")
            return None

        risk_amount = account_equity * MAX_RISK_PER_TRADE_PCT
        risk_per_share = entry_price - stop_loss_price

        qty = risk_amount / risk_per_share
        qty = round(qty, 4)          # keep 4 d.p. for fractional shares

        if qty <= 0:
            add_log("WARNING", "Position size calculated as 0 – rejecting order.")
            return None

        add_log(
            "INFO",
            f"Position size: {qty} shares | Risk: ${risk_amount:.2f} | "
            f"Entry: {entry_price} | SL: {stop_loss_price}",
        )
        return qty

    def validate_order(
        self,
        symbol: str,
        qty: float,
        entry_price: float,
        stop_loss_price: Optional[float],
        take_profit_price: Optional[float],
        free_cash: float,
    ) -> Tuple[bool, str]:
        """
        Final gate-keeper before an order is sent to Alpaca.

        Returns (True, "") if the order is approved, or
        (False, reason_string) if it is rejected.
        """
        # 1. Reject if we have no credentials configured
        from backend.config import ALPACA_API_KEY
        if not ALPACA_API_KEY or ALPACA_API_KEY == "your_alpaca_key_here":
            return False, "Alpaca API key is not configured."

        # 2. Reject if the order notional value exceeds free cash
        order_value = qty * entry_price
        if order_value > free_cash:
            return False, (
                f"Insufficient cash: order needs ${order_value:.2f}, "
                f"but only ${free_cash:.2f} available."
            )

        # 3. Stop-loss must exist for every buy order (non-negotiable)
        if stop_loss_price is None:
            return False, "Stop-loss is required for every entry. Order rejected."

        # 4. Stop-loss must be a real price below entry
        if stop_loss_price >= entry_price:
            return False, f"Stop-loss ({stop_loss_price}) must be below entry ({entry_price})."

        # 5. Take-profit must be above entry
        if take_profit_price is not None and take_profit_price <= entry_price:
            return False, f"Take-profit ({take_profit_price}) must be above entry ({entry_price})."

        return True, ""

    def default_levels(self, entry_price: float) -> Tuple[float, float]:
        """
        Fallback SL and TP if the strategy does not provide dynamic values.
        Uses percentages defined in config.
        """
        stop_loss = round(entry_price * (1 - DEFAULT_STOP_LOSS_PCT), 2)
        take_profit = round(entry_price * (1 + DEFAULT_TAKE_PROFIT_PCT), 2)
        return stop_loss, take_profit

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _current_drawdown_pct(self) -> float:
        if self.high_water_mark == 0:
            return 0.0
        return (self.high_water_mark - self.current_equity) / self.high_water_mark
