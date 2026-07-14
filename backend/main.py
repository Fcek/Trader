import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.config import ADMIN_PASSWORD
from backend.bot import TradingBot
from backend.database import get_open_trades, get_logs, get_equity_history, register_log_callback, get_logs_by_days
from fastapi.responses import Response

logger = logging.getLogger("api")

bot = TradingBot()

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception as e:
                logger.warning(f"Error sending message to client: {e}")

manager = ConnectionManager()

async def bot_event_listener(event: dict):
    """Callback for the TradingBot to send events to clients."""
    await manager.broadcast(json.dumps(event))

async def db_log_listener(log_data: dict):
    """Callback for database logs to send to clients."""
    await manager.broadcast(json.dumps({"type": "log", "data": log_data}))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting up FastAPI and Trading Bot...")
    bot.add_listener(bot_event_listener)
    register_log_callback(db_log_listener)
    asyncio.create_task(bot.start())
    yield
    # Shutdown
    logger.info("Shutting down FastAPI and Trading Bot...")
    await bot.stop()

app = FastAPI(title="Trading Bot API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://13.60.253.54",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Endpoints
@app.get("/api/status")
async def get_status():
    return {"running": bot.running}

@app.get("/api/positions")
async def get_positions():
    return get_open_trades()

@app.post("/api/positions/{symbol}/close")
async def close_position_endpoint(symbol: str, password: str = ""):
    if password != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Invalid password")
    await bot._close_position(symbol)
    return {"status": "success", "message": f"Close request sent for {symbol}"}

@app.get("/api/logs")
async def get_logs_api(limit: int = 50):
    return get_logs(limit)

@app.get("/api/logs/download")
async def download_logs_api(days: int = 7):
    from datetime import datetime, timedelta, timezone
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    
    combined = []
    
    # 1. System logs
    logs = get_logs_by_days(days)
    for log in logs:
        ts_str = log['timestamp']
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            ts = datetime.min.replace(tzinfo=timezone.utc)
        combined.append({
            "ts": ts,
            "text": f"[{ts_str}] {log['level']}: {log['message']}"
        })

    # 2. Metrics logs
    try:
        with open("decision_metrics.log", "r") as f:
            for line in f:
                if line.startswith("["):
                    ts_str = line[1:27]
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= cutoff_date:
                            combined.append({
                                "ts": ts,
                                "text": line.strip()
                            })
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass

    # Sort descending
    combined.sort(key=lambda x: x["ts"], reverse=True)
    text_lines = ["=== COMBINED SYSTEM & METRICS LOGS ==="]
    text_lines.extend([item["text"] for item in combined])
    
    content = "\n".join(text_lines)
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filename = f"trader_logs_{days}days_{current_date}.txt"
    return Response(
        content=content,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )

@app.get("/api/equity")
async def get_equity_api(limit: int = 5000, timeframe: str = "ALL"):
    return get_equity_history(limit, timeframe)

# WebSocket Endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # We don't expect messages from the client right now, but we need to keep the connection open
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# Mount Frontend
app.mount("/static", StaticFiles(directory="frontend"), name="static")

@app.get("/")
async def serve_frontend():
    return FileResponse(
        "frontend/index.html",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }
    )
