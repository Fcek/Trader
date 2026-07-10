import sqlite3
import sys

def main():
    conn = sqlite3.connect('trader.db')
    conn.row_factory = sqlite3.Row
    trades = conn.execute("SELECT * FROM trades WHERE status='OPEN'").fetchall()
    
    if len(sys.argv) > 1 and sys.argv[1] == '--update':
        for t in trades:
            tid = t['id']
            entry_price = float(t['entry_price'])
            trail_amount = float(t['take_profit'])
            stop_loss = float(t['stop_loss'])
            
            risk = entry_price - stop_loss
            if risk <= 0:
                risk = trail_amount
                
            new_tp = round(entry_price + (risk * 3), 2)
            
            print(f"Updating trade {tid} ({t['symbol']}): TP {trail_amount} -> {new_tp}")
            conn.execute("UPDATE trades SET take_profit = ? WHERE id = ?", (new_tp, tid))
        conn.commit()
        print("Updated.")
    else:
        for t in trades:
            print(dict(t))

if __name__ == '__main__':
    main()
