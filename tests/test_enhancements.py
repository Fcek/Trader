import pytest
from decimal import Decimal
from backend.risk_manager import RiskManager
from backend.strategy import EMACrossStrategy
from backend.config import ALLOW_SHORT_SELLING, MAX_SECTOR_EXPOSURE_PCT, MAX_RISK_PER_TRADE_PCT

def test_risk_manager_short_math():
    rm = RiskManager(starting_balance=10000)
    
    # Short entry at 100, SL at 104 (4% risk). Risk is $4 per share.
    # Total risk allowed = 10000 * 0.02 = 200 -> raw qty = 50 shares.
    # Capped by MAX_POSITION_SIZE_PCT (20% of 10000 = $2000 -> 20 shares max).
    qty = rm.calculate_position_size(entry_price=100, stop_loss_price=104, account_equity=10000)
    assert qty == 20.0
    
    # Validate a short order
    # Entry 100, SL 104, TP 90
    approved, msg = rm.validate_order(
        symbol="AAPL",
        qty=20,
        entry_price=100,
        stop_loss_price=104,
        take_profit_price=90,
        free_cash=10000
    )
    assert approved == True

    # Invalid short SL (below entry)
    approved, msg = rm.validate_order(
        symbol="AAPL",
        qty=20,
        entry_price=100,
        stop_loss_price=90,
        take_profit_price=80,
        free_cash=10000
    )
    assert approved == False

from unittest.mock import patch

def test_risk_manager_sector_exposure():
    rm = RiskManager(starting_balance=10000)
    
    # Mock config and DB
    with patch("backend.config.MAX_SECTOR_EXPOSURE_PCT", 0.40), \
         patch("backend.config.SYMBOL_METADATA", {
             "NVDA": {"sector": "Tech"},
             "AMD": {"sector": "Tech"}
         }), \
         patch("backend.database.get_open_trades", return_value=[
             {"symbol": "NVDA", "qty": 10, "entry_price": 300}
         ]):
        
        # Try to buy $2,000 more of AMD (Total $5,000 Tech exposure > $4,000 limit)
        approved, msg = rm.validate_order(
            symbol="AMD",
            qty=20,
            entry_price=100,
            stop_loss_price=96,
            take_profit_price=None,
            free_cash=10000
        )
        assert approved == False
        assert "Sector exposure limit breached" in msg
        
        # Try to buy $500 more of AMD (Total $3,500 Tech exposure <= $4,000 limit)
        approved, msg = rm.validate_order(
            symbol="AMD",
            qty=5,
            entry_price=100,
            stop_loss_price=96,
            take_profit_price=None,
            free_cash=10000
        )
        assert approved == True
