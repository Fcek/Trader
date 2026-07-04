import os
import sys
import random
import time
from datetime import datetime, timedelta

# Ensure backend can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.database import init_db, save_equity_snapshot, add_log, add_trade, get_db_connection
from backend.config import DB_FILE_PATH

def generate_mock_data():
    print(f"Generating mock data in {DB_FILE_PATH}...")
    
    # 1. Generate a nice equity curve for the last 50 points
    base_equity = 10000.0
    now = datetime.utcnow()
    
    for i in range(50, 0, -1):
        timestamp = (now - timedelta(minutes=i*15)).isoformat()
        # Random walk for equity
        change = random.uniform(-50, 60)
        base_equity += change
        save_equity_snapshot(
            balance=base_equity - 2000, 
            equity=base_equity, 
            unrealized_pnl=random.uniform(-100, 300)
        )
        # Hack timestamp in DB since save_equity_snapshot uses now()
        conn = get_db_connection()
        conn.execute("UPDATE equity_history SET timestamp = ? WHERE equity = ?", (timestamp, base_equity))
        conn.commit()
        conn.close()

    # 2. Add some mock open trades
    add_trade("AAPL", 15.5, "buy", 150.25, 145.0, 160.0, "mock_order_1")
    add_trade("TSLA", 5.0, "sell", 200.00, 210.0, 180.0, "mock_order_2")

    # 3. Add some logs
    add_log("INFO", "Mock data generation complete.")
    add_log("INFO", "Booting demo environment...")

if __name__ == "__main__":
    init_db()
    
    # Clear existing mock data to start fresh
    conn = get_db_connection()
    conn.execute("DELETE FROM equity_history")
    conn.execute("DELETE FROM trades")
    conn.execute("DELETE FROM system_logs")
    conn.commit()
    conn.close()

    generate_mock_data()
    
    print("Starting trading bot server in DEMO mode (mocked Alpaca API)...")
    
    from unittest.mock import patch, AsyncMock
    from run import main
    
    with patch('backend.bot.AlpacaClient') as MockClient:
        mock_instance = MockClient.return_value
        
        mock_instance.get_account = AsyncMock(return_value={
            "equity": "10000.0",
            "cash": "8000.0",
            "unrealized_pl": "150.0"
        })
        
        mock_instance.get_positions = AsyncMock(return_value=[
            {"symbol": "AAPL", "qty": "15.5", "avg_entry_price": "150.25", "side": "long"},
            {"symbol": "TSLA", "qty": "5.0", "avg_entry_price": "200.00", "side": "short"}
        ])
        
        mock_instance.listen_trade_updates = AsyncMock()
        
        # Call the actual main entrypoint
        main()
