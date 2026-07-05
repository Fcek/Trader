"""
alpaca_client.py – Thin async wrapper around the Alpaca REST + WebSocket APIs.

Uses raw `requests` (via asyncio.to_thread) for REST calls to keep the dependency
footprint minimal, and `websockets` for the real-time trade-update stream.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

import httpx
import websockets

from backend.config import ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_PAPER_TRADING
from backend.database import add_log

logger = logging.getLogger("alpaca_client")


class AlpacaClient:
    def __init__(self) -> None:
        self.api_key = ALPACA_API_KEY
        self.api_secret = ALPACA_API_SECRET
        self.paper = ALPACA_PAPER_TRADING

        if self.paper:
            self.base_url = "https://paper-api.alpaca.markets"
            self.trade_ws_url = "wss://paper-api.alpaca.markets/stream"
        else:
            self.base_url = "https://api.alpaca.markets"
            self.trade_ws_url = "wss://api.alpaca.markets/stream"

        self.data_url = "https://data.alpaca.markets/v2"
        self.headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.api_secret,
            "Content-Type": "application/json",
        }

    # ── REST helpers ──────────────────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, payload: Optional[Dict] = None
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if method == "GET":
                    resp = await client.get(url, headers=self.headers, params=payload)
                elif method == "POST":
                    resp = await client.post(url, headers=self.headers, json=payload)
                elif method == "DELETE":
                    resp = await client.delete(url, headers=self.headers)
                else:
                    raise ValueError(f"Unsupported method: {method}")

            if resp.status_code not in (200, 201, 204):
                msg = f"Alpaca API {resp.status_code}: {resp.text}"
                add_log("ERROR", msg)
                raise RuntimeError(msg)

            return {} if resp.status_code == 204 else resp.json()
        except Exception as exc:
            add_log("ERROR", f"HTTP {method} {path} failed: {exc}")
            raise

    async def get_account(self) -> Dict[str, Any]:
        return await self._request("GET", "/v2/account")

    async def get_positions(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/v2/positions")

    async def get_orders(self, status: str = "open") -> List[Dict[str, Any]]:
        return await self._request("GET", "/v2/orders", {"status": status})

    async def cancel_order(self, order_id: str) -> None:
        await self._request("DELETE", f"/v2/orders/{order_id}")
        add_log("INFO", f"Cancelled order {order_id}")

    async def submit_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "gtc",
        limit_price: Optional[float] = None,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        trail_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side.lower(),
            "type": order_type,
            "time_in_force": time_in_force,
        }

        if order_type == "limit" and limit_price:
            payload["limit_price"] = str(limit_price)
            
        if order_type == "trailing_stop" and trail_price:
            payload["trail_price"] = str(trail_price)

        if stop_loss_price or take_profit_price:
            payload["order_class"] = "bracket"
            if take_profit_price:
                payload["take_profit"] = {"limit_price": str(take_profit_price)}
            if stop_loss_price:
                payload["stop_loss"] = {"stop_price": str(stop_loss_price)}

        msg = (
            f"Submitting {side.upper()} {qty}×{symbol} (type={order_type})"
            + (f" SL={stop_loss_price}" if stop_loss_price else "")
            + (f" TP={take_profit_price}" if take_profit_price else "")
        )
        add_log("INFO", msg)
        return await self._request("POST", "/v2/orders", payload)

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_historical_bars(
        self, symbol: str, timeframe: str = "1Day", limit: int = 250
    ) -> List[Dict[str, Any]]:
        bars_dict = await self.get_historical_bars_multi([symbol], timeframe, limit)
        return bars_dict.get(symbol, [])

    async def get_historical_bars_multi(
        self, symbols: List[str], timeframe: str = "1Day", limit: int = 250
    ) -> Dict[str, List[Dict[str, Any]]]:
        # For 1Hour timeframe, 250 bars requires ~39 trading days. We use limit // 4 (~62 calendar days) to be safe.
        days_back = limit * 2 if timeframe == "1Day" else max(limit // 4, 30)
        start = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")

        symbols_str = ",".join(symbols)
        params = {
            "symbols": symbols_str,
            "timeframe": timeframe,
            "start": start,
            "limit": 10000,
            "adjustment": "all",
            "feed": "iex",   # IEX is available on paper; use "sip" on live
        }

        all_bars = {}
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                while True:
                    resp = await client.get(
                        f"{self.data_url}/stocks/bars",
                        headers=self.headers,
                        params=params,
                    )
                    if resp.status_code != 200:
                        add_log("ERROR", f"Market data error for {symbols_str}: {resp.text}")
                        break
                    
                    data = resp.json()
                    chunk = data.get("bars", {})
                    for sym, bars in chunk.items():
                        all_bars.setdefault(sym, []).extend(bars)
                        
                    page_token = data.get("next_page_token")
                    if not page_token:
                        break
                    params["page_token"] = page_token

            # Slice to 'limit' bars per symbol to match expected behavior
            for sym in all_bars:
                all_bars[sym] = all_bars[sym][-limit:]
            return all_bars
        except Exception as exc:
            add_log("ERROR", f"get_historical_bars_multi({symbols_str}): {exc}")
            return all_bars

    # ── WebSocket trade updates ───────────────────────────────────────────────

    async def listen_trade_updates(self, callback: Callable[[Dict], Any]) -> None:
        """
        Persistent WebSocket loop that delivers real-time trade-update events
        (fills, cancellations, partial fills) to `callback`.
        Automatically reconnects on any connection error.
        """
        auth_msg = {"action": "auth", "key": self.api_key, "secret": self.api_secret}
        sub_msg  = {"action": "listen", "data": {"streams": ["trade_updates"]}}

        while True:
            try:
                add_log("INFO", f"Connecting to trade-update stream: {self.trade_ws_url}")
                async with websockets.connect(self.trade_ws_url) as ws:
                    await ws.send(json.dumps(auth_msg))
                    auth_resp = json.loads(await ws.recv())

                    if auth_resp.get("data", {}).get("status") != "authorized":
                        add_log("ERROR", f"WS auth failed: {auth_resp}")
                        await asyncio.sleep(10)
                        continue

                    await ws.send(json.dumps(sub_msg))
                    add_log("INFO", "Subscribed to trade_updates stream.")

                    while True:
                        msg = json.loads(await ws.recv())
                        if msg.get("stream") == "trade_updates":
                            await callback(msg.get("data", {}))

            except websockets.exceptions.ConnectionClosed:
                add_log("WARNING", "Trade-update WS closed – reconnecting in 5 s…")
                await asyncio.sleep(5)
            except Exception as exc:
                add_log("ERROR", f"Trade-update WS error: {exc} – retrying in 10 s…")
                await asyncio.sleep(10)
