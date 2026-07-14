# 🤖 Trader Bot

A 24/7 Python **swing trading bot** with a premium web dashboard, powered by the [Alpaca API](https://alpaca.markets/).

---

## Features

- **Swing trading engine** — EMA crossover strategy with ATR-based dynamic stop-loss & take-profit
- **Risk management** — fixed-fraction position sizing, daily drawdown circuit-breaker, mandatory SL on every order
- **Crash recovery** — on restart, the bot reconciles its local database against live Alpaca positions so no trade is ever lost or double-counted
- **Premium web dashboard** — real-time equity chart, open positions table, trade history, and system log (Phase 3)
- **Alpaca Paper Trading** — full demo mode with no real money at risk while testing

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | ≥ 3.10 |
| [Poetry](https://python-poetry.org/) | ≥ 2.0 |
| Alpaca account | Free — [sign up here](https://app.alpaca.markets/signup) |

---

## Quick Start (any PC)

### 1 — Clone the repo

```bash
git clone <your-repo-url>
cd trader
```

### 2 — Install Poetry (if not already installed)

**Windows (PowerShell):**
```powershell
(Invoke-WebRequest -Uri https://install.python-poetry.org -UseBasicParsing).Content | python -
# Add Poetry to PATH (one-time):
[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path","User") + ";$env:APPDATA\Python\Scripts", "User")
```

**macOS / Linux:**
```bash
curl -sSL https://install.python-poetry.org | python3 -
```

### 3 — Create your virtual environment & install dependencies

```bash
# Creates .venv/ inside the project directory
poetry config virtualenvs.in-project true
poetry install
```

### 4 — Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

```env
# Get these from https://app.alpaca.markets → Paper Trading → API Keys
ALPACA_API_KEY=your_key_here
ALPACA_API_SECRET=your_secret_here
ALPACA_PAPER_TRADING=True   # Set to False only for live trading

# Emergency / recovery code (keep this secret!)
BOT_RECOVERY_CODE=change-me-to-a-long-random-string

# API Security
ADMIN_PASSWORD=change-me-to-a-strong-password

# Nginx Basic Auth (production only)
NGINX_USER=admin
NGINX_PASSWORD=change-me-to-a-strong-password

# Alerts (Optional)
DISCORD_WEBHOOK_URL=your_webhook_url
```

### 5 — Run the bot

```bash
poetry run python run.py
# OR use the script alias defined in pyproject.toml:
poetry run trader
```

The web dashboard will be available at **http://127.0.0.1:8000**.

---

## Production Deployment (Nginx Reverse Proxy)

In production, the bot runs behind an **Nginx reverse proxy** with **HTTP Basic Auth** so the dashboard is password-protected and not directly exposed to the internet.

### Architecture

```
Internet → [Port 80] → Nginx (Basic Auth) → [localhost:8000] → Uvicorn/FastAPI
```

- **Uvicorn** binds to `127.0.0.1:8000` (not publicly accessible)
- **Nginx** listens on port `80`, enforces Basic Auth, and proxies authenticated requests
- Scanner/bot traffic is blocked by Nginx before it reaches the app

### Setup on the server

```bash
# 1. Install Nginx and htpasswd utility
sudo apt-get install -y nginx apache2-utils

# 2. Create the password file (using values from .env)
sudo htpasswd -cb /etc/nginx/.htpasswd admin your-password

# 3. Copy the Nginx config
sudo cp nginx/trader.conf /etc/nginx/sites-available/trader
sudo ln -sf /etc/nginx/sites-available/trader /etc/nginx/sites-enabled/trader
sudo rm -f /etc/nginx/sites-enabled/default

# 4. Test and restart Nginx
sudo nginx -t
sudo systemctl restart nginx
```

> **Note:** Ensure your AWS Security Group allows inbound traffic on port **80** (HTTP). Port 8000 can be closed since Nginx handles all external traffic.

---

## Running Tests

```bash
# Run all tests with verbose output
poetry run pytest -v

# Run a specific test file
poetry run pytest tests/test_risk_manager.py -v

# Run with coverage report
poetry run pytest --cov=backend --cov-report=term-missing
```

---

## Project Structure

```
trader/
├── backend/
│   ├── __init__.py          # Package init
│   ├── config.py            # Loads .env, exposes all config constants
│   ├── database.py          # SQLite helpers (trades, equity, logs, state)
│   ├── alpaca_client.py     # Alpaca REST + WebSocket wrapper
│   ├── strategy.py          # Pluggable strategy system + EMACrossStrategy
│   ├── risk_manager.py      # Position sizing, circuit-breaker, order validation
│   └── bot.py               # Main trading state machine
├── frontend/                # Web dashboard
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── nginx/
│   └── trader.conf          # Nginx reverse proxy config
├── tests/
│   ├── __init__.py
│   ├── test_config.py
│   ├── test_database.py
│   ├── test_strategy.py
│   ├── test_risk_manager.py
│   └── test_alpaca_client.py
├── .env                     # ⚠️ Git-ignored — never commit this
├── .env.example             # Safe template to commit
├── pyproject.toml           # Poetry project & dependencies
├── poetry.lock              # Locked dependency versions (commit this!)
└── run.py                   # Single entry-point
```

---

## Safety & Risk Settings

All configurable in [`.env`](.env.example):

| Setting | Default | Description |
|---|---|---|
| `ALPACA_PAPER_TRADING` | `True` | Use paper account (demo). Set `False` only for live |
| `MAX_DRAWDOWN_PCT` | 15% | Bot halts if daily equity drops more than this |
| `MAX_RISK_PER_TRADE_PCT` | 2% | Maximum equity risked on any single trade |
| `DEFAULT_STOP_LOSS_PCT` | 2% | Fallback SL if strategy provides no dynamic value |
| `DEFAULT_TAKE_PROFIT_PCT` | 6% | Fallback TP (1:3 risk-reward ratio) |
| `BOT_RECOVERY_CODE` | — | Secret code required for emergency API actions |

> ⚠️ **Warning:** Never set `ALPACA_PAPER_TRADING=False` until you have thoroughly tested the bot on a paper account and understand the risks of automated trading.

---

## License

MIT — see [LICENSE](LICENSE)
