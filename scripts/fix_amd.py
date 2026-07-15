"""One-shot script: delete the stale ADOPTED AMD trade so reconciliation re-adopts it with SL/TP."""
import sqlite3, sys, os

db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader.db")
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT id, symbol, side, stop_loss, take_profit FROM trades "
    "WHERE status='OPEN' AND alpaca_entry_order_id='ADOPTED_ON_RECOVERY' AND stop_loss IS NULL"
).fetchall()

if not rows:
    print("No stale adopted trades found.")
    sys.exit(0)

for r in rows:
    print(f"Deleting stale adopted trade id={r['id']} symbol={r['symbol']} (SL={r['stop_loss']}, TP={r['take_profit']})")
    conn.execute("DELETE FROM trades WHERE id=?", (r['id'],))

conn.commit()
print("Done. Restart the bot so reconciliation re-adopts these positions with SL/TP.")
