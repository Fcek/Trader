"""
run.py – Single entry-point to boot the trading bot.

Usage:
    poetry run python run.py          # normal start
    poetry run trader                 # via the pyproject.toml script alias
"""

import asyncio
import logging
import sys

import uvicorn
from backend.database import init_db

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("runner")


def main() -> None:
    logger.info("Initialising database…")
    init_db()

    logger.info("Starting FastAPI server + trading bot…")
    # main.py is defined in Phase 3; uvicorn will auto-reload the bot via lifespan events.
    uvicorn.run(
        "backend.main:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
