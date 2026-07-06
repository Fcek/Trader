import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from backend.bot import TradingBot
from backend.database import init_db, get_open_trades, add_trade

@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_bot.db"
    monkeypatch.setattr("backend.config.DB_FILE_PATH", db_file)
    monkeypatch.setattr("backend.database.DB_FILE_PATH", db_file)
    init_db()
    yield db_file

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
    
    # Mock historical data to return AAPL and QQQ
    mock_bars = {
        "AAPL": [{"c": 150}, {"c": 151}],
        "QQQ": [{"c": 300}, {"c": 301}]
    }
    bot.client.get_historical_bars_multi.return_value = mock_bars
    bot.watchlist = ["AAPL"]
    bot.running = True
    
    # Mock _evaluate_symbol so it doesn't do real stuff
    bot._evaluate_symbol = AsyncMock()
    
    # Mock account
    bot.client.get_account.return_value = {"equity": "10000", "cash": "10000"}
    
    await bot._evaluate_all_symbols()
    
    bot.client.get_historical_bars_multi.assert_called_once()
    args, kwargs = bot.client.get_historical_bars_multi.call_args
    assert "QQQ" in args[0]
    
    # Ensure QQQ was passed as market_bars but with the forming bar dropped
    bot._evaluate_symbol.assert_called_once_with(
        "AAPL",
        mock_bars["AAPL"],
        [{"c": 300}], # QQQ with forming bar dropped
        10000,
        10000,
        set()
    )

@pytest.mark.asyncio
async def test_open_position(bot):
    """Ensure _open_position submits native market buy and persists trade."""
    bot.risk_manager = MagicMock()
    bot.risk_manager.calculate_position_size = MagicMock(return_value=10.0)
    bot.risk_manager.validate_order = MagicMock(return_value=(True, ""))
    
    bot.client.submit_order.return_value = {"id": "mock_order_123"}
    
    # Mock strategy signal output
    signal_result = {
        "stop_loss": 140.0,
        "trail_amount": 2.5
    }
    
    await bot._open_position("AAPL", signal_result, 10000, 10000, 150.0)
    
    bot.client.submit_order.assert_called_once_with(
        symbol="AAPL",
        qty=10.0,
        side="buy",
        order_type="market",
        time_in_force="day"
    )
    
    trades = get_open_trades()
    assert len(trades) == 1
    assert trades[0]["symbol"] == "AAPL"
    assert trades[0]["take_profit"] == 2.5  # stored trail_amount here
    assert trades[0]["alpaca_entry_order_id"] == "mock_order_123"

@pytest.mark.asyncio
async def test_on_trade_update_trailing_stop(bot):
    """Ensure a BUY fill triggers the Trailing Stop sell order."""
    # Insert open trade into DB simulating a submitted but unfilled buy
    add_trade("AAPL", 10.0, "buy", 150.0, 140.0, 2.5, "mock_order_123")
    
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
    
    bot.client.submit_order.return_value = {"id": "mock_ts_124"}
    
    await bot._on_trade_update(event)
    
    bot.client.submit_order.assert_called_once_with(
        symbol="AAPL",
        qty=10.0,
        side="sell",
        order_type="trailing_stop",
        trail_price=2.5,
        time_in_force="gtc"
    )
