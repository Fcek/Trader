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
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from backend.alpaca_client import AlpacaClient
from backend.config import WATCHLIST, BASE_DIR
from backend.database import (
    add_log,
    add_trade,
    get_bot_state,
    get_open_trades,
    save_equity_snapshot,
    set_bot_state,
    update_trade_entry_price,
    update_trade_exit,
)
from backend.risk_manager import RiskManager
from backend.strategy import EMACrossStrategy

logger = logging.getLogger("bot")

# How often to run the strategy scan (seconds).
# 600 = 10 minutes — enough to catch new 1-hour bar closes promptly.
STRATEGY_INTERVAL_SECONDS: int = 600

# How often to refresh account equity for the drawdown monitor (seconds).
EQUITY_POLL_INTERVAL_SECONDS: int = 60

# Cooldown period (hours) before re-entering a symbol after exit.
# Prevents whipsaw re-entries on EMA crossover strategies.
COOLDOWN_HOURS: int = 24


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
        self.watchlist = watchlist or WATCHLIST
        self.running = False
        self._listeners: List[Callable[[Dict[str, Any]], Any]] = []
        self._tasks: List[asyncio.Task] = []
        self._pending_closes: set[str] = set()
        # Counter for consecutive Alpaca paper-trading equity glitches.
        # Suppresses repeated log spam: logs on first hit then every 10th.
        self._equity_glitch_count: int = 0

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
            asyncio.create_task(self._software_stop_loss_loop()),
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

    async def _software_stop_loss_loop(self) -> None:
        """Monitors and triggers soft stop-loss for fractional positions."""
        from backend.database import get_open_trades, update_trade_stop_loss
        while self.running:
            try:
                positions = await self._get_broker_positions()
                open_trades = get_open_trades()
                
                broker_lookup = {p["symbol"]: p for p in positions}
                
                # Cleanup pending closes if the position actually closed
                self._pending_closes = {sym for sym in self._pending_closes if sym in broker_lookup}
                
                updated_sl = False
                for trade in open_trades:
                    symbol = trade["symbol"]
                    if symbol not in broker_lookup:
                        continue
                        
                    if symbol in self._pending_closes:
                        continue
                        
                    qty = float(trade["qty"])
                    # Skip whole-number positions since they have an Alpaca SL attached
                    if qty.is_integer():
                        continue
                        
                    current_price = float(broker_lookup[symbol].get("current_price", 0))
                    stop_loss = trade.get("stop_loss")
                    activation_price = trade.get("activation_price")
                    trail_amount = trade.get("trail_amount")
                    take_profit = trade.get("take_profit") # for legacy trades
                    
                    if stop_loss is None:
                        continue
                        
                    # Check if we hit the hard stop loss
                    if current_price <= stop_loss:
                        add_log("WARNING", f"📉 Soft SL triggered for {symbol} at {current_price} (SL: {stop_loss}).")
                        await self._close_position(symbol)
                        continue
                        
                    if take_profit and current_price >= take_profit:
                        add_log("INFO", f"🎯 Soft TP triggered for {symbol} at {current_price} (TP: {take_profit}).")
                        await self._close_position(symbol)
                        continue
                        
                    # Check trailing activation
                    if activation_price and current_price >= activation_price and trail_amount:
                        new_sl = round(current_price - trail_amount, 2)
                        if new_sl > stop_loss:
                            update_trade_stop_loss(trade["id"], new_sl)
                            trade["stop_loss"] = new_sl
                            add_log("INFO", f"📈 Trailing stop raised for {symbol}: {stop_loss} -> {new_sl}")
                            updated_sl = True
                            
                            # Attempt to update native broker order
                            try:
                                orders = await self.client.get_orders(status="open")
                                sl_order = next((o for o in orders if o["symbol"] == symbol and o["type"] == "stop"), None)
                                if sl_order:
                                    await self.client.replace_order(sl_order["id"], new_sl)
                            except Exception as e:
                                add_log("ERROR", f"Failed to replace native SL order for {symbol}: {e}")
                            
                if updated_sl:
                    await self._get_broker_positions()
                            
            except Exception as e:
                add_log("ERROR", f"Software stop loss loop error: {e}")

            await asyncio.sleep(15)

    async def _equity_monitor_loop(self) -> None:
        """Polls account equity every minute and triggers circuit-breaker.
        Skips recording snapshots when the market is closed to avoid
        flat lines on the dashboard chart.
        """
        while self.running:
            try:
                # Check if market is open — skip snapshot recording when closed
                market_open = True
                try:
                    clock = await self.client.get_clock()
                    market_open = clock.get("is_open", False)
                except Exception as e:
                    add_log("WARNING", f"Could not check market clock in equity monitor: {e}. Recording anyway.")

                account = await self.client.get_account()
                equity = float(account["equity"])
                balance = float(account["cash"])
                unrealized = float(account.get("unrealized_pl", 0))

                if market_open:
                    from backend.database import get_open_trades
                    open_trades = get_open_trades()
                    # Alpaca paper trading glitch: positions disappear, equity drops to cash balance
                    if len(open_trades) > 0 and equity == balance:
                        self._equity_glitch_count += 1
                        # Log only the first occurrence and every 10th thereafter to avoid spam
                        if self._equity_glitch_count == 1:
                            add_log("WARNING", "Ignoring invalid Alpaca equity snapshot (paper trading glitch). Subsequent occurrences will be suppressed.")
                        elif self._equity_glitch_count % 10 == 0:
                            add_log("WARNING", f"Alpaca equity glitch still occurring (x{self._equity_glitch_count} times). Continuing to ignore.")
                    else:
                        if self._equity_glitch_count > 0:
                            add_log("INFO", f"Alpaca equity glitch resolved after {self._equity_glitch_count} occurrence(s). Resuming normal snapshots.")
                            self._equity_glitch_count = 0
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
                add_log("ERROR", f"Equity monitor error: {type(e).__name__}: {e}")

            await asyncio.sleep(EQUITY_POLL_INTERVAL_SECONDS)

    async def _strategy_loop(self) -> None:
        """
        Evaluates the trading strategy for each symbol in the watchlist.
        Runs once immediately on startup, then every STRATEGY_INTERVAL_SECONDS.
        Skips evaluation when the market is closed to save API calls.
        """
        while self.running:
            try:
                # Check if market is open before scanning
                try:
                    clock = await self.client.get_clock()
                    if not clock.get("is_open", False):
                        next_open = clock.get("next_open", "unknown")
                        logger.info(f"⏸ Market closed. Next open: {next_open}. Skipping scan.")
                        await asyncio.sleep(STRATEGY_INTERVAL_SECONDS)
                        continue
                    else:
                        next_close_str = clock.get("next_close")
                        if next_close_str:
                            next_close = datetime.fromisoformat(next_close_str)
                            now = datetime.now(timezone.utc)
                            time_to_close = (next_close - now).total_seconds()
                            if time_to_close > 15 * 60:
                                logger.info(f"⏳ Market open but not near close ({(time_to_close/3600):.1f}h remaining). Skipping scan.")
                                await asyncio.sleep(STRATEGY_INTERVAL_SECONDS)
                                continue
                except Exception as e:
                    add_log("WARNING", f"Could not check market clock: {e}. Scanning anyway.")

                await self._evaluate_all_symbols()
            except Exception as e:
                add_log("ERROR", f"Strategy loop error: {e}")

            await asyncio.sleep(STRATEGY_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Strategy evaluation
    # ------------------------------------------------------------------

    async def _evaluate_all_symbols(self) -> None:
        """Run the strategy on every symbol in the watchlist."""
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

        from backend.config import SYMBOL_METADATA, MAX_VIX_LEVEL

        # VIX check
        skip_new_entries = False
        try:
            import yfinance as yf
            vix = yf.Ticker("^VIX").history(period="1d")
            if not vix.empty:
                current_vix = vix["Close"].iloc[-1]
                if current_vix > MAX_VIX_LEVEL:
                    add_log("WARNING", f"VIX is {current_vix:.2f} > {MAX_VIX_LEVEL}. Halting new entries.")
                    skip_new_entries = True
        except Exception as e:
            add_log("WARNING", f"Could not fetch VIX: {e}")

        # Fetch unique macro symbols plus watchlist
        macro_symbols = {SYMBOL_METADATA.get(sym, {}).get("macro", "SPY") for sym in self.watchlist}
        fetch_list = list(set(self.watchlist) | macro_symbols)

        bars_dict = await self.client.get_historical_bars_multi(fetch_list, timeframe="1Day", limit=251)
        
        # Note: forming day is dropped in _evaluate_symbol

        scan_results = []
        for symbol in self.watchlist:
            if not self.running:
                break
            try:
                bars = bars_dict.get(symbol, [])
                macro_sym = SYMBOL_METADATA.get(symbol, {}).get("macro", "SPY")
                market_bars = bars_dict.get(macro_sym, [])
                res = await self._evaluate_symbol(symbol, bars, market_bars, equity, free_cash, open_positions, skip_new_entries)
                if res:
                    scan_results.append(res)
            except Exception as e:
                add_log("ERROR", f"Error evaluating {symbol}: {e}")
                
        if scan_results:
            combined = ", ".join(scan_results)
            add_log("INFO", f"📊 Scan complete: {combined}")

    async def _evaluate_symbol(
        self,
        symbol: str,
        bars: List[Dict[str, Any]],
        market_bars: List[Dict[str, Any]],
        equity: float,
        free_cash: float,
        open_positions: set,
        skip_new_entries: bool = False,
    ) -> str:
        """Evaluate one symbol and submit/skip as appropriate."""
        if not bars or len(bars) < 2:
            return f"{symbol}: No Data"
            
        # Drop the current day's forming bar to prevent intraday repainting
        closed_bars = bars[:-1]

        result = self.strategy.generate_signal(closed_bars, market_bars)
        signal = result["signal"]
        reason = result["reason"]
        metrics = result.get("metrics")

        log_msg = f"{symbol}: {signal}"

        if metrics:
            metrics_str = ", ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}" for k, v in metrics.items())
            try:
                metrics_path = os.path.join(BASE_DIR, "decision_metrics.log")
                with open(metrics_path, "a") as f:
                    f.write(f"[{datetime.now(timezone.utc).isoformat()}] {symbol} | Signal: {signal} | Metrics: {metrics_str}\n")
            except Exception as e:
                logger.error(f"Failed to write metrics: {e}")

        if signal in ["BUY", "SELL_SHORT"] and symbol not in open_positions:
            if skip_new_entries:
                return f"{symbol}: {signal} ignored (VIX too high)"

            # Earnings check
            try:
                import yfinance as yf
                ticker = yf.Ticker(symbol)
                cal = ticker.calendar
                import pandas as pd
                if isinstance(cal, pd.DataFrame) and not cal.empty and "Earnings Date" in cal.index:
                    earliest = cal.loc["Earnings Date"].iloc[0]
                    days_to_earnings = (earliest.date() - datetime.now(timezone.utc).date()).days
                    if 0 <= days_to_earnings <= 3:
                        add_log("INFO", f"Skipping {symbol} due to upcoming earnings in {days_to_earnings} days.")
                        return f"{symbol}: {signal} ignored (Earnings in {days_to_earnings}d)"
            except Exception as e:
                pass # Ignore if we can't fetch earnings

            # Check re-entry cooldown to prevent whipsaw
            last_exit_str = get_bot_state(f"cooldown_{symbol}")
            if last_exit_str:
                last_exit = datetime.fromisoformat(last_exit_str)
                cooldown_until = last_exit + timedelta(hours=COOLDOWN_HOURS)
                if datetime.now(timezone.utc) < cooldown_until:
                    remaining = (cooldown_until - datetime.now(timezone.utc)).total_seconds() / 3600
                    log_msg += f" (cooldown {remaining:.1f}h remaining)"
                    return log_msg

            entry_price = float(bars[-1]["c"])
            await self._open_position(symbol, result, equity, free_cash, entry_price)

        elif signal in ["SELL", "COVER"] and symbol in open_positions:
            await self._close_position(symbol)
            
        return log_msg

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    async def _open_position(
        self,
        symbol: str,
        signal_result: Dict[str, Any],
        equity: float,
        free_cash: float,
        entry_price: float,
    ) -> None:
        """Submit a bracket BUY order for the given symbol."""
        side = "buy" if signal_result.get("signal") == "BUY" else "sell"

        if side == "sell":
            try:
                asset_info = await self.client.get_asset(symbol)
                if not asset_info.get("shortable", False):
                    add_log("WARNING", f"Cannot short sell {symbol} as it is not shortable on Alpaca.")
                    return
            except Exception as e:
                add_log("WARNING", f"Failed to check if {symbol} is shortable: {e}")
                return

        # Resolve SL / TP (strategy may provide dynamic values, fallback to config)
        stop_loss = signal_result.get("stop_loss")
        activation_price = signal_result.get("activation_price")
        trail_amount = signal_result.get("trail_amount")
        if stop_loss is None or activation_price is None or trail_amount is None:
            stop_loss, tp = self.risk_manager.default_levels(entry_price, side=side)
            activation_price = entry_price + (entry_price - stop_loss) * 2 if side == "buy" else entry_price - (stop_loss - entry_price) * 2
            trail_amount = abs(entry_price - stop_loss)
        else:
            tp = signal_result.get("take_profit")
            if not tp:
                _, default_tp = self.risk_manager.default_levels(entry_price, side=side)
                tp = default_tp

        # Calculate position size
        qty = self.risk_manager.calculate_position_size(entry_price, stop_loss, equity)
        if qty is None:
            return

        # Final validation gate
        approved, rejection_reason = self.risk_manager.validate_order(
            symbol, qty, entry_price, stop_loss, activation_price, free_cash
        )
        if not approved:
            add_log("WARNING", f"Order rejected for {symbol}: {rejection_reason}")
            return

        # Submit to Alpaca
        limit_price = round(entry_price * 1.001, 2) if side == "buy" else round(entry_price * 0.999, 2)
        
        try:
            if qty.is_integer():
                order = await self.client.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    order_type="limit",
                    limit_price=limit_price,
                    time_in_force="day",
                    stop_loss_price=stop_loss,
                    take_profit_price=tp,
                )
            else:
                order = await self.client.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side=side,
                    order_type="limit",
                    limit_price=limit_price,
                    time_in_force="day",
                )
            order_id = order.get("id")

            # Persist trade in local DB
            trade_id = add_trade(
                symbol=symbol,
                qty=qty,
                side=side,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=tp,
                activation_price=activation_price,
                trail_amount=trail_amount,
                order_id=order_id,
            )

            msg = f"✅ {side.upper()} {qty} × {symbol} @ ~{entry_price} (Limit {limit_price}) | SL: {stop_loss} | Activate: {activation_price} | Trail: {trail_amount}"
            add_log("INFO", msg)
            await self._emit("trade_opened", {
                "trade_id": trade_id,
                "symbol": symbol,
                "qty": qty,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "activation_price": activation_price,
                "trail_amount": trail_amount,
            })

        except Exception as e:
            add_log("ERROR", f"Failed to submit {side.upper()} order for {symbol}: {e}")

    async def _close_position(self, symbol: str) -> None:
        """Submit a request to close an existing position entirely."""
        try:
            order = await self.client.close_position(symbol)
            self._pending_closes.add(symbol)
            set_bot_state(f"cooldown_{symbol}", datetime.now(timezone.utc).isoformat())
            add_log("INFO", f"📤 Sent SELL signal for {symbol} (position closed, {COOLDOWN_HOURS}h cooldown started).")
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

            if side.lower() == "buy":
                # Update DB with actual fill price (replaces the estimate)
                from backend.database import get_db_connection
                conn = get_db_connection()
                trade = conn.execute(
                    "SELECT * FROM trades WHERE symbol = ? AND status = 'OPEN' ORDER BY id DESC LIMIT 1",
                    (symbol,)
                ).fetchone()
                if trade:
                    update_trade_entry_price(trade["id"], filled_price)
                    add_log("INFO", f"Updated {symbol} entry price to actual fill: {filled_price}")

                if filled_qty.is_integer():
                    add_log("INFO", f"Integer order for {symbol}; SL bracket order attached.")
                else:
                    add_log("INFO", f"Fractional order for {symbol}; tracking SL via software loop.")
            
            elif side.lower() == "sell":
                from backend.database import get_db_connection, update_trade_exit
                conn = get_db_connection()
                # We need to find the OPEN trade for this symbol to mark it closed
                trade = conn.execute("SELECT * FROM trades WHERE symbol = ? AND status = 'OPEN'", (symbol,)).fetchone()
                if trade:
                    entry_price = float(trade["entry_price"])
                    qty = float(trade["qty"])
                    pnl = (filled_price - entry_price) * qty
                    pnl_str = f"Gain: ${pnl:.2f}" if pnl >= 0 else f"Loss: ${abs(pnl):.2f}"
                    update_trade_exit(
                        trade_id=trade["id"],
                        exit_price=filled_price,
                        pnl=pnl,
                        order_id=order_id
                    )
                    add_log("INFO", f"Trade for {symbol} closed in DB. {pnl_str}")

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
                    adopted_side = "buy" if pos.get("side") == "long" else "sell"

                    # Try to recover SL/TP from open bracket-leg orders on Alpaca
                    stop_loss = None
                    take_profit = None
                    try:
                        open_orders = await self.client.get_orders(status="open")
                        for o in open_orders:
                            if o.get("symbol") != symbol:
                                continue
                            order_type = o.get("type", "")
                            order_side = o.get("side", "")
                            # For a long position, SL is a sell stop; TP is a sell limit
                            # For a short position, SL is a buy stop; TP is a buy limit
                            if order_type == "stop" and o.get("stop_price"):
                                stop_loss = float(o["stop_price"])
                            elif order_type == "limit" and o.get("limit_price"):
                                take_profit = float(o["limit_price"])
                        if stop_loss or take_profit:
                            add_log("INFO", f"Recovered orders for {symbol}: SL={stop_loss}, TP={take_profit}")
                    except Exception as e:
                        add_log("WARNING", f"Could not recover SL/TP orders for {symbol}: {e}")

                    add_trade(
                        symbol=symbol,
                        qty=float(pos["qty"]),
                        side=adopted_side,
                        entry_price=float(pos.get("avg_entry_price", 0)),
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        order_id="ADOPTED_ON_RECOVERY",
                    )

            # 2. DB trades that no longer have a matching broker position
            for symbol, trade in db_symbols.items():
                if symbol not in broker_symbols:
                    # Try to recover actual exit price from Alpaca order history
                    exit_price = 0.0
                    pnl = 0.0
                    try:
                        orders = await self.client.get_closed_orders(symbol, limit=5)
                        sell_order = next(
                            (o for o in orders if o.get("side") == "sell" and o.get("status") == "filled"),
                            None,
                        )
                        if sell_order:
                            exit_price = float(sell_order.get("filled_avg_price", 0))
                            entry_price = float(trade["entry_price"])
                            qty = float(trade["qty"])
                            pnl = (exit_price - entry_price) * qty
                            pnl_str = f"Gain: ${pnl:.2f}" if pnl >= 0 else f"Loss: ${abs(pnl):.2f}"
                            add_log("INFO", f"Recovered exit price for {symbol}: ${exit_price:.2f}, {pnl_str}")
                    except Exception as e:
                        add_log("WARNING", f"Could not recover exit price for {symbol}: {e}")

                    add_log(
                        "INFO",
                        f"Position for {symbol} no longer on broker – marking as CLOSED (SL/TP triggered).",
                    )
                    update_trade_exit(
                        trade_id=trade["id"],
                        exit_price=exit_price,
                        pnl=pnl,
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
            
            from backend.database import get_open_trades
            db_trades = {t["symbol"]: t for t in get_open_trades()}
            
            enriched_positions = []
            for p in positions:
                enriched = dict(p)
                db_t = db_trades.get(p["symbol"])
                if db_t:
                    enriched["stop_loss"] = db_t.get("stop_loss")
                    enriched["take_profit"] = db_t.get("take_profit")
                    enriched["activation_price"] = db_t.get("activation_price")
                    enriched["trail_amount"] = db_t.get("trail_amount")
                enriched_positions.append(enriched)
                
            await self._emit("position_update", enriched_positions)
            return enriched_positions
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
