import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.bot import TradingBot
from backend.database import get_open_trades, get_logs, get_equity_history, register_log_callback

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
    allow_origins=["*"],
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
async def close_position_endpoint(symbol: str):
    await bot._close_position(symbol)
    return {"status": "success", "message": f"Close request sent for {symbol}"}

@app.get("/api/logs")
async def get_logs_api(limit: int = 50):
    return get_logs(limit)

@app.get("/api/equity")
async def get_equity_api(limit: int = 100):
    return get_equity_history(limit)

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
    return FileResponse("frontend/index.html")
