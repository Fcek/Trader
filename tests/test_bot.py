import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from backend.bot import TradingBot
from backend.database import init_db, get_open_trades, add_trade


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

    bot.client.get_historical_bars_multi.assert_called_once()
    args, kwargs = bot.client.get_historical_bars_multi.call_args
    assert "SPY" in args[0]

    # Ensure SPY was passed as market_bars but with the forming bar dropped
    bot._evaluate_symbol.assert_called_once_with(
        "AAPL",
        [{"c": 150}], # AAPL with forming bar dropped
        [{"c": 300}], # SPY with forming bar dropped
        10000.0,
        10000.0,
        set(),
        False
    )

@pytest.mark.asyncio
async def test_open_position(bot):
    """Ensure _open_position submits native market buy with SL bracket and persists trade."""
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
        time_in_force="day",
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
    """Ensure a BUY fill logs correctly and doesn't submit a trailing stop immediately."""
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
