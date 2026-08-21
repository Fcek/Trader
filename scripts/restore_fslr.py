"""
restore_fslr.py – Restores the open short position record for FSLR in SQLite database.
"""
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import config

def restore_fslr():
    db_path = str(config.DB_FILE_PATH)
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM trades WHERE symbol = 'FSLR' ORDER BY id DESC LIMIT 1")
    trade = cur.fetchone()

    if trade:
        print(f"Found existing FSLR trade id={trade['id']} status={trade['status']}. Restoring to OPEN...")
        cur.execute("""
            UPDATE trades
            SET status = 'OPEN',
                exit_price = NULL,
                exit_time = NULL,
                pnl = 0.0,
                alpaca_exit_order_id = NULL,
                entry_price = 220.0,
                stop_loss = 242.88,
                take_profit = 206.93,
                activation_price = 173.91,
                trail_amount = 22.99
            WHERE id = ?
        """, (trade["id"],))
    else:
        print("No FSLR trade found. Inserting new OPEN short trade...")
        cur.execute("""
            INSERT INTO trades (
                symbol, qty, side, entry_price, entry_time, status,
                stop_loss, take_profit, activation_price, trail_amount,
                alpaca_entry_order_id
            ) VALUES (
                'FSLR', 84.0, 'sell', 220.0, '2026-08-19T14:02:14.608458', 'OPEN',
                242.88, 206.93, 173.91, 22.99,
                '9294894a-49f3-41c6-a489-d72c60187c9b'
            )
        """)

    conn.commit()

    print("\n--- CURRENT OPEN TRADES ---")
    cur.execute("SELECT id, symbol, side, qty, entry_price, stop_loss, take_profit, activation_price, trail_amount, status FROM trades WHERE status = 'OPEN'")
    for r in cur.fetchall():
        print(dict(r))

    conn.close()
    print("\nFSLR restore complete.")

if __name__ == "__main__":
    restore_fslr()
