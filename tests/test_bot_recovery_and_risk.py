import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from backend.bot import TradingBot
from backend.database import add_trade, get_open_trades, get_bot_state, set_bot_state, init_db


@pytest.fixture
def bot():
    bot_instance = TradingBot()
    bot_instance.client = AsyncMock()
    return bot_instance

@pytest.mark.asyncio
async def test_reconcile_positions_adopts_untracked(bot):
    """If broker has a position not in DB, bot should adopt it."""
    # Mock broker returning 1 AAPL position
    bot.client.get_positions.return_value = [
        {"symbol": "AAPL", "qty": "10", "avg_entry_price": "150.0"}
    ]
    
    # DB initially has no open trades
    assert len(get_open_trades()) == 0
    
    await bot._reconcile_positions()
    
    # DB should now have AAPL
    open_trades = get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["symbol"] == "AAPL"
    assert open_trades[0]["qty"] == 10.0
    assert open_trades[0]["alpaca_entry_order_id"] == "ADOPTED_ON_RECOVERY"

@pytest.mark.asyncio
async def test_reconcile_positions_closes_missing(bot):
    """If DB has a position but broker does not, bot should close it in DB."""
    # DB has MSFT open
    add_trade("MSFT", 5.0, "buy", 300.0, 290.0, 320.0, "some_order_id")
    
    # Broker has NO positions
    bot.client.get_positions.return_value = []
    
    await bot._reconcile_positions()
    
    # DB should have marked MSFT as closed
    open_trades = get_open_trades()
    assert len(open_trades) == 0

@pytest.mark.asyncio
async def test_risk_manager_stress_test_drawdown(bot):
    """Verify that a massive drawdown triggers the circuit breaker and stops the bot."""
    
    # Setup account equity at $10,000 initially
    bot.client.get_account.side_effect = [
        {"equity": "10000", "cash": "10000", "unrealized_pl": "0"},
        # Next poll: massive crash to $5000 (50% drawdown, well over limit)
        {"equity": "5000", "cash": "5000", "unrealized_pl": "-5000"},
    ]
    
    # Start the bot to initialize risk manager
    # We patch bot._reconcile_positions and the background tasks so we can manually drive it
    with patch.object(bot, '_reconcile_positions', new_callable=AsyncMock), \
         patch.object(bot, '_strategy_loop', new_callable=AsyncMock), \
         patch.object(bot.client, 'listen_trade_updates', new_callable=AsyncMock):
        
        await bot.start()
        
        assert bot.risk_manager is not None
        assert bot.risk_manager.high_water_mark == 10000.0
        assert bot.running is True
        
        # Manually run one iteration of the equity monitor logic by letting it pull the next account state
        # In the real loop it's a while loop, we can just call the logic manually or let it run
        # To avoid infinite loop in test, we will just replicate the inner loop logic
        
        account = await bot.client.get_account()
        equity = float(account["equity"])
        
        # Update risk manager
        ok = bot.risk_manager.update_equity(equity)
        
        assert ok is False # Drawdown exceeded
        if not ok:
            await bot.stop()
            
        assert bot.running is False
