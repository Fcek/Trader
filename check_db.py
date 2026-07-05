import sqlite3

conn = sqlite3.connect("trader/trader.db")
rows = conn.execute("SELECT timestamp, message FROM system_logs ORDER BY timestamp DESC LIMIT 25").fetchall()
for r in rows:
    print(r[0], r[1])
