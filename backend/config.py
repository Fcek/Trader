import os
from pathlib import Path
from dotenv import load_dotenv

# Resolve paths relative to this file
BASE_DIR = Path(__file__).resolve().parent.parent
env_path = BASE_DIR / ".env"

if env_path.exists():
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()  # Fallback to standard search path

# ── Alpaca Credentials ────────────────────────────────────────────────────────
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_PAPER_TRADING = os.getenv("ALPACA_PAPER_TRADING", "True").lower() in ("true", "1", "t", "yes")

# ── FastAPI Server ────────────────────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "127.0.0.1")

# ── Database ──────────────────────────────────────────────────────────────────
DB_CONN_STR = os.getenv("DATABASE_PATH", "sqlite:///trader.db")
if DB_CONN_STR.startswith("sqlite:///"):
    DB_FILE_PATH = BASE_DIR / DB_CONN_STR.replace("sqlite:///", "")
else:
    DB_FILE_PATH = BASE_DIR / "trader.db"

# ── Master Recovery Code ──────────────────────────────────────────────────────
# Required to authorize emergency operations (liquidate all, reset DB) via API.
BOT_RECOVERY_CODE = os.getenv("BOT_RECOVERY_CODE", "9f7a8b3c-4d2e-4f1a-8c3b-5d6e7f8a9b0c")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")

# ── Trading Safety Limits ─────────────────────────────────────────────────────
MAX_DRAWDOWN_PCT       = 0.15   # Halt bot if daily equity drawdown exceeds 15%
MAX_RISK_PER_TRADE_PCT = 0.02   # Risk at most 2% of equity per trade
MAX_POSITION_SIZE_PCT  = 0.20   # Cap any single position to at most 20% of account equity
DEFAULT_STOP_LOSS_PCT  = 0.02   # Fallback stop-loss: 2% below entry
DEFAULT_TAKE_PROFIT_PCT = 0.06  # Fallback take-profit: 6% above entry (1:3 RRR)

# ── Watchlist ─────────────────────────────────────────────────────────────────
_watchlist_env = os.getenv("WATCHLIST", "AMD,ASML,NVDA,MSFT,PLTR,FSLR,ENPH,ICLN,GLD,NEM,INTC")
WATCHLIST = [sym.strip().upper() for sym in _watchlist_env.split(",") if sym.strip()]
