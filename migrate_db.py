import sqlite3

def migrate():
    conn = sqlite3.connect('trader.db')
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN activation_price REAL")
        print("Added activation_price column.")
    except Exception as e:
        print("Migration error (activation_price):", e)
        
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN trail_amount REAL")
        print("Added trail_amount column.")
        conn.commit()
    except Exception as e:
        print("Migration error (trail_amount):", e)

if __name__ == '__main__':
    migrate()
