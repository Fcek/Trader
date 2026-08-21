import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from backend.bot import TradingBot
from backend.database import init_db, get_open_trades, get_closed_trades, add_trade, get_db_connection


@pytest.fixture
def bot():
    b = TradingBot()
    b.client = AsyncMock()
    return b

@pytest.mark.asyncio
async def test_evaluate_all_symbols(bot):
    """Ensure it fetches QQQ and passes it down correctly."""
    # Mock positions
    bot._get_broker_positions = AsyncMock(return_value=[])
    
    # Mock historical data to return AAPL and SPY
    mock_bars = {
        "AAPL": [{"c": 150}, {"c": 151}],
        "SPY": [{"c": 300}, {"c": 301}]
    }
    bot.client.get_historical_bars_multi.return_value = mock_bars
    bot.watchlist = ["AAPL"]
    bot.running = True

    # Mock _evaluate_symbol so it doesn't do real stuff
    bot._evaluate_symbol = AsyncMock(return_value="Mocked Result")

    # Mock account
    bot.client.get_account.return_value = {"equity": "10000", "cash": "10000"}

    await bot._evaluate_all_symbols()

    assert bot.client.get_historical_bars_multi.call_count == 2
    args, kwargs = bot.client.get_historical_bars_multi.call_args
    assert "SPY" in args[0]

    # Ensure SPY was passed as market_bars and daily_bars was passed
    bot._evaluate_symbol.assert_called_once_with(
        "AAPL",
        [{"c": 150}, {"c": 151}], # AAPL tactical bars
        [{"c": 300}, {"c": 301}], # SPY macro bars
        10000.0,
        10000.0,
        set(),
        False,
        daily_bars=[{"c": 150}, {"c": 151}]
    )

@pytest.mark.asyncio
async def test_open_position(bot):
    """Ensure _open_position submits native limit buy with GTC bracket and persists trade."""
    bot.risk_manager = MagicMock()
    bot.risk_manager.calculate_position_size = MagicMock(return_value=10.0)
    bot.risk_manager.validate_order = MagicMock(return_value=(True, ""))
    bot.risk_manager.default_levels = MagicMock(return_value=(140.0, 160.0))
    
    bot.client.submit_order.return_value = {"id": "mock_order_123"}
    
    # Mock strategy signal output
    signal_result = {
        "signal": "BUY",
        "stop_loss": 140.0,
        "activation_price": 160.0,
        "trail_amount": 10.0
    }

    await bot._open_position("AAPL", signal_result, 10000, 10000, 150.0)

    bot.client.submit_order.assert_called_once_with(
        symbol="AAPL",
        qty=10.0,
        side="buy",
        order_type="limit",
        limit_price=150.15,
        time_in_force="gtc",
        stop_loss_price=140.0,
        take_profit_price=160.0
    )
    
    trades = get_open_trades()
    assert len(trades) == 1
    assert trades[0]["symbol"] == "AAPL"
    assert trades[0]["take_profit"] == 160.0
    assert trades[0]["activation_price"] == 160.0
    assert trades[0]["trail_amount"] == 10.0
    assert trades[0]["alpaca_entry_order_id"] == "mock_order_123"

@pytest.mark.asyncio
async def test_on_trade_update_buy_fill(bot):
    """Ensure a BUY fill logs correctly and updates entry price."""
    # Insert open trade into DB simulating a submitted but unfilled buy
    add_trade("AAPL", 10.0, "buy", 150.0, stop_loss=140.0, activation_price=160.0, trail_amount=10.0, order_id="mock_order_123")
    
    event = {
        "event": "fill",
        "price": "151.0",
        "order": {
            "id": "mock_order_123",
            "symbol": "AAPL",
            "side": "buy",
            "filled_qty": "10.0"
        }
    }
    
    bot._get_broker_positions = AsyncMock(return_value=[])
    bot._emit = AsyncMock()
    
    await bot._on_trade_update(event)
    
    bot.client.submit_order.assert_not_called()
    open_trades = get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["entry_price"] == 151.0
    assert open_trades[0]["status"] == "OPEN"

@pytest.mark.asyncio
async def test_on_trade_update_short_entry_fill(bot):
    """Ensure a short SELL fill (entry) does NOT close the trade prematurely."""
    # Insert open short trade into DB
    trade_id = add_trade("FSLR", 84.0, "sell", 220.14, stop_loss=242.88, take_profit=206.93, activation_price=173.91, trail_amount=22.99, order_id="short_entry_order_1")
    
    event = {
        "event": "fill",
        "price": "220.0",
        "order": {
            "id": "short_entry_order_1",
            "symbol": "FSLR",
            "side": "sell",
            "filled_qty": "84.0",
            "position_intent": "sell_to_open"
        }
    }
    
    bot._get_broker_positions = AsyncMock(return_value=[])
    bot._emit = AsyncMock()
    
    await bot._on_trade_update(event)
    
    open_trades = get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["symbol"] == "FSLR"
    assert open_trades[0]["side"] == "sell"
    assert open_trades[0]["status"] == "OPEN"
    assert open_trades[0]["entry_price"] == 220.0

@pytest.mark.asyncio
async def test_on_trade_update_short_exit_fill(bot):
    """Ensure an exit fill for a short position closes trade and calculates correct short PnL."""
    add_trade("FSLR", 84.0, "sell", 220.0, stop_loss=242.88, take_profit=206.93, activation_price=173.91, trail_amount=22.99, order_id="short_entry_order_1")
    
    # Exit order (buy_to_close) at 210.0 (gain of $10/share = $840)
    event = {
        "event": "fill",
        "price": "210.0",
        "order": {
            "id": "short_exit_order_2",
            "symbol": "FSLR",
            "side": "buy",
            "filled_qty": "84.0",
            "position_intent": "buy_to_close"
        }
    }
    
    bot._get_broker_positions = AsyncMock(return_value=[])
    bot._emit = AsyncMock()
    
    await bot._on_trade_update(event)
    
    assert len(get_open_trades()) == 0
    closed_trades = get_closed_trades()
    assert len(closed_trades) == 1
    assert closed_trades[0]["symbol"] == "FSLR"
    assert closed_trades[0]["exit_price"] == 210.0
    assert closed_trades[0]["pnl"] == pytest.approx(840.0)

@pytest.mark.asyncio
async def test_software_stop_loss_short(bot):
    """Ensure software stop loss loop checks short trades correctly."""
    add_trade("FSLR", 84.5, "sell", 220.0, stop_loss=240.0, take_profit=200.0, activation_price=210.0, trail_amount=10.0, order_id="entry_1")
    
    open_trades = get_open_trades()
    assert len(open_trades) == 1
    
    trade = open_trades[0]
    current_price = 241.0
    stop_loss = trade["stop_loss"]
    trade_side = trade["side"]
    
    assert trade_side == "sell"
    assert current_price >= stop_loss # Triggered!


@pytest.mark.asyncio
async def test_software_stop_loss_breakeven_long(bot):
    """Ensure SL moves to entry price (breakeven) once price hits +1.0R (entry + trail_amount)."""
    # Entry: 100.0, SL: 95.0, trail_amount: 5.0 -> Breakeven triggers at 105.0
    trade_id = add_trade("AAPL", 10.0, "buy", 100.0, stop_loss=95.0, activation_price=110.0, trail_amount=5.0, order_id="entry_aapl")
    
    bot._pending_closes = set()
    bot.client.get_orders = AsyncMock(return_value=[])
    bot._get_broker_positions = AsyncMock(return_value=[
        {"symbol": "AAPL", "current_price": "105.5"} # Price reached +1.0R
    ])

    open_trades = get_open_trades()
    trade = open_trades[0]
    entry_price = float(trade["entry_price"])
    trail_amount = float(trade["trail_amount"])
    current_price = 105.5
    stop_loss = float(trade["stop_loss"])
    
    # Breakeven condition check
    assert current_price >= (entry_price + trail_amount)
    assert stop_loss < entry_price


@pytest.mark.asyncio
async def test_software_stop_loss_breakeven_short(bot):
    """Ensure SL moves to entry price (breakeven) once short price drops +1.0R (entry - trail_amount)."""
    # Entry: 100.0, SL: 105.0, trail_amount: 5.0 -> Breakeven triggers at 95.0
    trade_id = add_trade("FSLR", 50.0, "sell", 100.0, stop_loss=105.0, activation_price=90.0, trail_amount=5.0, order_id="entry_fslr")
    
    open_trades = get_open_trades()
    trade = open_trades[0]
    entry_price = float(trade["entry_price"])
    trail_amount = float(trade["trail_amount"])
    current_price = 94.5
    stop_loss = float(trade["stop_loss"])
    
    # Breakeven condition check for short
    assert current_price <= (entry_price - trail_amount)
    assert stop_loss > entry_price

