import logging
from typing import Optional, Tuple, Dict, Any
from decimal import Decimal
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

    def __init__(self, starting_balance: float | Decimal | str):
        self.starting_balance = Decimal(str(starting_balance))
        self.high_water_mark = self.starting_balance   # best equity we've seen today
        self.current_equity = self.starting_balance

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def update_equity(self, equity: float | Decimal | str) -> bool:
        """
        Called periodically with the latest account equity.

        Returns:
            True  – equity is within acceptable drawdown.
            False – daily drawdown limit breached; bot must stop.
        """
        eq = Decimal(str(equity))
        self.current_equity = eq
        if eq > self.high_water_mark:
            self.high_water_mark = eq

        drawdown = self._current_drawdown_pct()
        if drawdown >= Decimal(str(MAX_DRAWDOWN_PCT)):
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
        entry_price: float | Decimal | str,
        stop_loss_price: float | Decimal | str,
        account_equity: float | Decimal | str,
    ) -> Optional[Decimal]:
        """
        Fixed-fractional position sizing.

        Risk per trade = equity × MAX_RISK_PER_TRADE_PCT
        Risk per share = entry_price − stop_loss_price
        Qty            = risk_per_trade / risk_per_share

        Returns the number of shares (float, Alpaca supports fractional
        shares for many symbols) or None if the order would be rejected.
        """
        ep = Decimal(str(entry_price))
        sl = Decimal(str(stop_loss_price))
        eq = Decimal(str(account_equity))

        if sl == ep:
            add_log("WARNING", "Invalid stop-loss: SL must not equal entry price.")
            return None

        risk_amount = eq * Decimal(str(MAX_RISK_PER_TRADE_PCT))
        risk_per_share = abs(ep - sl)

        qty_by_risk = risk_amount / risk_per_share

        # Cap qty based on max position allocation to prevent going all-in
        from backend.config import MAX_POSITION_SIZE_PCT
        max_notional_value = eq * Decimal(str(MAX_POSITION_SIZE_PCT))
        max_qty_by_allocation = max_notional_value / ep

        qty = min(qty_by_risk, max_qty_by_allocation)
        
        # Alpaca does not support fractional shares for short selling
        if sl > ep:
            qty = float(int(qty))
        else:
            qty = round(float(qty), 4)

        if qty <= 0:
            add_log("WARNING", "Position size calculated as 0 – rejecting order.")
            return None

        capped_msg = " (Capped by max allocation limit)" if qty == round(max_qty_by_allocation, 4) else ""
        add_log(
            "INFO",
            f"Position size: {qty} shares{capped_msg} | Risk: ${risk_amount:.2f} | "
            f"Entry: {entry_price} | SL: {stop_loss_price}",
        )
        return qty

    def validate_order(
        self,
        symbol: str,
        qty: float | Decimal | str,
        entry_price: float | Decimal | str,
        stop_loss_price: Optional[float | Decimal | str],
        take_profit_price: Optional[float | Decimal | str],
        free_cash: float | Decimal | str,
        side: Optional[str] = None,
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
        q = Decimal(str(qty))
        ep = Decimal(str(entry_price))
        fc = Decimal(str(free_cash))
        
        order_value = q * ep
        if order_value > fc:
            return False, (
                f"Insufficient cash: order needs ${order_value:.2f}, "
                f"but only ${free_cash:.2f} available."
            )

        # 3. Stop-loss must exist for every buy order (non-negotiable)
        if stop_loss_price is None:
            return False, "Stop-loss is required for every entry. Order rejected."

        # 4. Stop-loss logic
        sl = Decimal(str(stop_loss_price))
        if sl == ep:
            return False, f"Stop-loss ({sl}) cannot equal entry ({ep})."

        tp = Decimal(str(take_profit_price)) if take_profit_price is not None else None

        if side:
            is_short = side.lower() in ("sell", "short")
        elif tp is not None and tp > ep:
            is_short = False
        elif tp is not None and tp < ep:
            is_short = True
        else:
            is_short = sl > ep

        if not is_short and sl > ep:
            return False, f"Stop-loss ({sl}) must be below entry ({ep}) for longs."
        if is_short and sl < ep:
            return False, f"Stop-loss ({sl}) must be above entry ({ep}) for shorts."

        # Check max stop-loss distance (hard cap)
        from backend.config import MAX_STOP_LOSS_DISTANCE_PCT
        if MAX_STOP_LOSS_DISTANCE_PCT is not None:
            max_dist_pct = Decimal(str(MAX_STOP_LOSS_DISTANCE_PCT)) + Decimal('0.005')
            actual_dist_pct = abs(ep - sl) / ep
            if actual_dist_pct > max_dist_pct:
                return False, f"Stop-loss distance ({actual_dist_pct:.1%}) exceeds maximum limit of {MAX_STOP_LOSS_DISTANCE_PCT:.1%}."

        # 5. Take-profit must be logical
        if tp is not None:
            if not is_short and tp <= ep:
                return False, f"Take-profit ({tp}) must be above entry ({ep}) for longs."
            if is_short and tp >= ep:
                return False, f"Take-profit ({tp}) must be below entry ({ep}) for shorts."

        # 6. Sector exposure limits
        from backend.config import MAX_SECTOR_EXPOSURE_PCT, SYMBOL_METADATA
        from backend.database import get_open_trades
        target_sector = SYMBOL_METADATA.get(symbol, {}).get("sector", "Unknown")
        open_trades = get_open_trades()
        sector_exposure = 0.0
        for t in open_trades:
            t_sym = t["symbol"]
            t_sec = SYMBOL_METADATA.get(t_sym, {}).get("sector", "Unknown")
            if t_sec == target_sector:
                # Estimate current value with entry price
                sector_exposure += float(t["qty"]) * float(t["entry_price"])
        
        max_sector_notional = float(self.current_equity) * float(MAX_SECTOR_EXPOSURE_PCT)
        if (sector_exposure + float(order_value)) > max_sector_notional:
            return False, f"Sector exposure limit breached. Max: ${max_sector_notional:.2f}, Current: ${sector_exposure:.2f}, Order: ${order_value:.2f}"

        return True, ""

    def default_levels(self, entry_price: float | Decimal | str, side: str = "buy") -> Tuple[Decimal, Decimal]:
        """
        Fallback SL and TP if the strategy does not provide dynamic values.
        Uses percentages defined in config.
        """
        ep = Decimal(str(entry_price))
        sl_pct = Decimal(str(DEFAULT_STOP_LOSS_PCT))
        tp_pct = Decimal(str(DEFAULT_TAKE_PROFIT_PCT))
        
        if side.lower() == "buy":
            stop_loss = round(ep * (Decimal('1') - sl_pct), 2)
            take_profit = round(ep * (Decimal('1') + tp_pct), 2)
        else:
            stop_loss = round(ep * (Decimal('1') + sl_pct), 2)
            take_profit = round(ep * (Decimal('1') - tp_pct), 2)
            
        return stop_loss, take_profit

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _current_drawdown_pct(self) -> Decimal:
        if self.high_water_mark == Decimal('0'):
            return Decimal('0')
        return (self.high_water_mark - self.current_equity) / self.high_water_mark
