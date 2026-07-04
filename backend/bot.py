"""
bot.py – The core trading state machine.

Responsibilities:
  1. Polls Alpaca for account state and current positions on startup.
  2. Reconciles the local SQLite database with the broker's ground truth
     (crash-recovery: if a position exists on Alpaca but not in the DB it
     is adopted; if an order in the DB has been filled it is marked closed).
  3. Runs a periodic strategy evaluation loop (default: once per market day).
  4. Emits structured events that the FastAPI server can broadcast to the
     web dashboard via WebSocket.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from backend.alpaca_client import AlpacaClient
from backend.database import (
    add_log,
    add_trade,
    get_bot_state,
    get_open_trades,
    save_equity_snapshot,
    set_bot_state,
    update_trade_exit,
)
from backend.risk_manager import RiskManager
from backend.strategy import EMACrossStrategy

logger = logging.getLogger("bot")

# Watchlist – symbols the bot is allowed to trade
DEFAULT_WATCHLIST: List[str] = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOG"]

# How often to run the strategy scan (seconds).
# 3 600 = 1 hour.  Swing trading → daily bar evaluation is enough.
STRATEGY_INTERVAL_SECONDS: int = 3_600

# How often to refresh account equity for the drawdown monitor (seconds).
EQUITY_POLL_INTERVAL_SECONDS: int = 60


class TradingBot:
    """
    A long-only, swing-trading bot driven by the EMACrossStrategy.

    Event broadcasting
    ------------------
    Any coroutine can register itself as an event listener via
    ``bot.add_listener(coro)``.  The listener is called with a dict:

        {"type": "equity_update",   "data": {...}}
        {"type": "position_update", "data": [...]}
        {"type": "trade_opened",    "data": {...}}
        {"type": "trade_closed",    "data": {...}}
        {"type": "log",             "data": {"level": ..., "message": ...}}
    """

    def __init__(self, watchlist: Optional[List[str]] = None):
        self.client = AlpacaClient()
        self.strategy = EMACrossStrategy()
        self.risk_manager: Optional[RiskManager] = None
        self.watchlist = watchlist or DEFAULT_WATCHLIST
        self.running = False
        self._listeners: List[Callable[[Dict[str, Any]], Any]] = []
        self._tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the bot: reconcile state, then kick off background loops."""
        if self.running:
            add_log("WARNING", "Bot is already running.")
            return

        add_log("INFO", "🚀 Bot starting…")
        self.running = True
        set_bot_state("bot_running", "1")

        # 1. Fetch account and initialise the risk manager
        try:
            account = await self.client.get_account()
            equity = float(account["equity"])
            self.risk_manager = RiskManager(starting_balance=equity)
            add_log("INFO", f"Account equity: ${equity:,.2f}")
        except Exception as e:
            add_log("ERROR", f"Failed to fetch account on startup: {e}")
            self.running = False
            set_bot_state("bot_running", "0")
            return

        # 2. Reconcile open positions with the local DB
        await self._reconcile_positions()

        # 3. Start background tasks
        self._tasks = [
            asyncio.create_task(self._equity_monitor_loop()),
            asyncio.create_task(self._strategy_loop()),
            asyncio.create_task(self.client.listen_trade_updates(self._on_trade_update)),
        ]

        add_log("INFO", "✅ Bot is live and all loops are running.")
        await self._emit("log", {"level": "INFO", "message": "Bot started successfully."})

    async def stop(self) -> None:
        """Gracefully stop all background loops."""
        if not self.running:
            return

        add_log("INFO", "⏹ Bot stopping…")
        self.running = False
        set_bot_state("bot_running", "0")

        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        add_log("INFO", "Bot stopped.")
        await self._emit("log", {"level": "INFO", "message": "Bot stopped."})

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _equity_monitor_loop(self) -> None:
        """Polls account equity every minute and triggers circuit-breaker."""
        while self.running:
            try:
                account = await self.client.get_account()
                equity = float(account["equity"])
                balance = float(account["cash"])
                unrealized = float(account.get("unrealized_pl", 0))

                save_equity_snapshot(balance, equity, unrealized)

                await self._emit("equity_update", {
                    "equity": equity,
                    "balance": balance,
                    "unrealized_pnl": unrealized,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

                # Circuit-breaker check
                if self.risk_manager and not self.risk_manager.update_equity(equity):
                    await self._emit("log", {
                        "level": "CRITICAL",
                        "message": "⛔ Daily drawdown limit hit – bot shutting down!",
                    })
                    await self.stop()
                    return

            except Exception as e:
                add_log("ERROR", f"Equity monitor error: {e}")

            await asyncio.sleep(EQUITY_POLL_INTERVAL_SECONDS)

    async def _strategy_loop(self) -> None:
        """
        Evaluates the trading strategy for each symbol in the watchlist.
        Runs once immediately on startup, then every STRATEGY_INTERVAL_SECONDS.
        """
        while self.running:
            try:
                await self._evaluate_all_symbols()
            except Exception as e:
                add_log("ERROR", f"Strategy loop error: {e}")

            await asyncio.sleep(STRATEGY_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Strategy evaluation
    # ------------------------------------------------------------------

    async def _evaluate_all_symbols(self) -> None:
        """Run the strategy on every symbol in the watchlist."""
        add_log("INFO", f"📊 Running strategy scan on {len(self.watchlist)} symbols…")

        # Fetch current account state once
        try:
            account = await self.client.get_account()
            equity = float(account["equity"])
            free_cash = float(account["cash"])
        except Exception as e:
            add_log("ERROR", f"Cannot evaluate strategy – account fetch failed: {e}")
            return

        # Which symbols do we already hold a position in?
        open_positions = {p["symbol"] for p in await self._get_broker_positions()}

        for symbol in self.watchlist:
            if not self.running:
                break
            try:
                await self._evaluate_symbol(symbol, equity, free_cash, open_positions)
            except Exception as e:
                add_log("ERROR", f"Error evaluating {symbol}: {e}")

    async def _evaluate_symbol(
        self,
        symbol: str,
        equity: float,
        free_cash: float,
        open_positions: set,
    ) -> None:
        """Evaluate one symbol and submit/skip as appropriate."""
        # Fetch 250 daily bars (enough for EMA-200 calculation) + 1 for the current forming day
        bars = await self.client.get_historical_bars(symbol, timeframe="1Day", limit=251)
        if not bars or len(bars) < 2:
            add_log("WARNING", f"No market data for {symbol}, skipping.")
            return
            
        # Drop the current day's forming bar to prevent intraday repainting
        closed_bars = bars[:-1]

        result = self.strategy.generate_signal(closed_bars)
        signal = result["signal"]
        reason = result["reason"]

        add_log("INFO", f"[{symbol}] Signal: {signal} – {reason}")

        if signal == "BUY" and symbol not in open_positions:
            await self._open_position(symbol, result, equity, free_cash)

        elif signal == "SELL" and symbol in open_positions:
            await self._close_position(symbol)

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def _open_position(
        self,
        symbol: str,
        signal_result: Dict[str, Any],
        equity: float,
        free_cash: float,
    ) -> None:
        """Submit a bracket BUY order for the given symbol."""
        # Get the latest price
        bars = await self.client.get_historical_bars(symbol, timeframe="1Day", limit=2)
        if not bars:
            return
        entry_price = float(bars[-1]["c"])

        # Resolve SL / TP (strategy may provide dynamic values, fallback to config)
        stop_loss = signal_result.get("stop_loss")
        take_profit = signal_result.get("take_profit")
        if stop_loss is None or take_profit is None:
            stop_loss, take_profit = self.risk_manager.default_levels(entry_price)

        # Calculate position size
        qty = self.risk_manager.calculate_position_size(entry_price, stop_loss, equity)
        if qty is None:
            return

        # Final validation gate
        approved, rejection_reason = self.risk_manager.validate_order(
            symbol, qty, entry_price, stop_loss, take_profit, free_cash
        )
        if not approved:
            add_log("WARNING", f"Order rejected for {symbol}: {rejection_reason}")
            return

        # Submit to Alpaca
        try:
            order = await self.client.submit_order(
                symbol=symbol,
                qty=qty,
                side="buy",
                order_type="market",
                time_in_force="gtc",
                stop_loss_price=stop_loss,
                take_profit_price=take_profit,
            )
            order_id = order.get("id")

            # Persist trade in local DB
            trade_id = add_trade(
                symbol=symbol,
                qty=qty,
                side="buy",
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                order_id=order_id,
            )

            msg = f"✅ BUY {qty} × {symbol} @ ~{entry_price} | SL: {stop_loss} | TP: {take_profit}"
            add_log("INFO", msg)
            await self._emit("trade_opened", {
                "trade_id": trade_id,
                "symbol": symbol,
                "qty": qty,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
            })

        except Exception as e:
            add_log("ERROR", f"Failed to submit BUY order for {symbol}: {e}")

    async def _close_position(self, symbol: str) -> None:
        """Submit a SELL market order to close an existing position."""
        try:
            order = await self.client.submit_order(
                symbol=symbol,
                qty=1,               # Alpaca will close full position if qty > held
                side="sell",
                order_type="market",
                time_in_force="gtc",
            )
            add_log("INFO", f"📤 Sent SELL signal for {symbol} (strategy exit).")
        except Exception as e:
            add_log("ERROR", f"Failed to close position for {symbol}: {e}")

    # ------------------------------------------------------------------
    # Trade-update WebSocket handler
    # ------------------------------------------------------------------

    async def _on_trade_update(self, event: Dict[str, Any]) -> None:
        """
        Handles real-time trade updates from Alpaca.
        Called by AlpacaClient.listen_trade_updates().
        """
        ev_type = event.get("event")
        order = event.get("order", {})
        symbol = order.get("symbol", "?")
        order_id = order.get("id")

        if ev_type == "fill":
            filled_price = float(event.get("price", 0))
            filled_qty = float(order.get("filled_qty", 0))
            side = order.get("side", "?")
            add_log("INFO", f"🔔 Order FILLED: {side.upper()} {filled_qty} × {symbol} @ {filled_price}")
            await self._emit("position_update", await self._get_broker_positions())

        elif ev_type in ("canceled", "expired"):
            add_log("WARNING", f"Order {order_id} for {symbol} was {ev_type}.")

        elif ev_type == "partial_fill":
            add_log("INFO", f"Partial fill for {symbol}: {order.get('filled_qty')} of {order.get('qty')} shares.")

    # ------------------------------------------------------------------
    # Reconciliation (crash recovery)
    # ------------------------------------------------------------------

    async def _reconcile_positions(self) -> None:
        """
        Compare broker positions to local DB on startup.

        - If the broker shows a position not tracked in the DB, adopt it.
        - If the DB shows an open trade but the broker no longer has the
          position, mark it as closed (SL or TP was hit while bot was down).
        """
        add_log("INFO", "🔍 Reconciling positions with broker…")
        try:
            broker_positions = await self._get_broker_positions()
            broker_symbols = {p["symbol"]: p for p in broker_positions}

            db_open_trades = get_open_trades()
            db_symbols = {t["symbol"]: t for t in db_open_trades}

            # 1. Positions on broker not tracked locally
            for symbol, pos in broker_symbols.items():
                if symbol not in db_symbols:
                    add_log(
                        "WARNING",
                        f"Adopting untracked broker position: {symbol} ({pos['qty']} shares).",
                    )
                    add_trade(
                        symbol=symbol,
                        qty=float(pos["qty"]),
                        side="buy",
                        entry_price=float(pos.get("avg_entry_price", 0)),
                        order_id="ADOPTED_ON_RECOVERY",
                    )

            # 2. DB trades that no longer have a matching broker position
            for symbol, trade in db_symbols.items():
                if symbol not in broker_symbols:
                    add_log(
                        "INFO",
                        f"Position for {symbol} no longer on broker – marking as CLOSED (SL/TP triggered).",
                    )
                    update_trade_exit(
                        trade_id=trade["id"],
                        exit_price=0.0,
                        pnl=0.0,
                        order_id="CLOSED_WHILE_OFFLINE",
                    )

        except Exception as e:
            add_log("ERROR", f"Reconciliation error: {e}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_broker_positions(self) -> List[Dict[str, Any]]:
        """Wrapper around AlpacaClient.get_positions with error handling."""
        try:
            positions = await self.client.get_positions()
            await self._emit("position_update", positions)
            return positions
        except Exception as e:
            add_log("ERROR", f"Failed to fetch broker positions: {e}")
            return []

    async def _emit(self, event_type: str, data: Any) -> None:
        """Broadcast an event to all registered listeners."""
        payload = {"type": event_type, "data": data}
        for listener in self._listeners:
            try:
                await listener(payload)
            except Exception as e:
                logger.warning(f"Listener error on event '{event_type}': {e}")

    def add_listener(self, coro: Callable[[Dict[str, Any]], Any]) -> None:
        """Register a coroutine to receive bot events."""
        self._listeners.append(coro)
