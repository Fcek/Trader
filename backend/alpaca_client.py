"""
alpaca_client.py – Thin async wrapper around the Alpaca REST + WebSocket APIs.

Uses raw `requests` (via asyncio.to_thread) for REST calls to keep the dependency
footprint minimal, and `websockets` for the real-time trade-update stream.
"""

import asyncio
import json
import logging
import random
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
        self._session: Optional[httpx.AsyncClient] = None

    # ── REST helpers ──────────────────────────────────────────────────────────

    async def _request(
        self, method: str, path: str, payload: Optional[Dict] = None
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        
        for attempt in range(3):
            if self._session is None or self._session.is_closed:
                self._session = httpx.AsyncClient(timeout=20.0)
                
            try:
                if method == "GET":
                    resp = await self._session.get(url, headers=self.headers, params=payload)
                elif method == "POST":
                    resp = await self._session.post(url, headers=self.headers, json=payload)
                elif method == "DELETE":
                    resp = await self._session.delete(url, headers=self.headers, params=payload)
                elif method == "PATCH":
                    resp = await self._session.patch(url, headers=self.headers, json=payload)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                if resp.status_code not in (200, 201, 204):
                    if resp.status_code == 404 and method == "DELETE" and path.startswith("/v2/positions/"):
                        add_log("INFO", f"Position already closed or not found on broker: {path}")
                        return {}
                    
                    # Retry on 5xx server errors
                    if resp.status_code >= 500 and attempt < 2:
                        jitter = random.uniform(0.5, 1.5)
                        await asyncio.sleep((2 ** attempt) * jitter)
                        continue

                    msg = f"Alpaca API {resp.status_code}: {resp.text}"
                    add_log("ERROR", msg)
                    raise RuntimeError(msg)

                return {} if resp.status_code == 204 else resp.json()
                
            except httpx.RequestError as exc:
                if attempt < 2:
                    jitter = random.uniform(0.5, 1.5)
                    await asyncio.sleep((2 ** attempt) * jitter)
                    continue
                add_log("ERROR", f"HTTP {method} {path} failed after retries: {exc.__class__.__name__} - {exc}")
                raise
            except RuntimeError:
                raise
            except Exception as exc:
                add_log("ERROR", f"HTTP {method} {path} failed: {exc.__class__.__name__} - {exc}")
                raise

    async def get_account(self) -> Dict[str, Any]:
        return await self._request("GET", "/v2/account")

    async def get_asset(self, symbol: str) -> Dict[str, Any]:
        return await self._request("GET", f"/v2/assets/{symbol}")

    async def get_positions(self) -> List[Dict[str, Any]]:
        return await self._request("GET", "/v2/positions")

    async def get_orders(self, status: str = "open") -> List[Dict[str, Any]]:
        return await self._request("GET", "/v2/orders", {"status": status})

    async def get_clock(self) -> Dict[str, Any]:
        """Get market clock (is_open, next_open, next_close)."""
        return await self._request("GET", "/v2/clock")

    async def get_closed_orders(self, symbol: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Get recent closed/filled orders for a symbol (for reconciliation)."""
        return await self._request("GET", "/v2/orders", {
            "status": "closed",
            "symbols": symbol,
            "limit": str(limit),
            "direction": "desc",
        })

    async def cancel_order(self, order_id: str) -> None:
        await self._request("DELETE", f"/v2/orders/{order_id}")
        add_log("INFO", f"Cancelled order {order_id}")

    async def replace_order(self, order_id: str, stop_price: float) -> Dict[str, Any]:
        payload = {"stop_price": str(stop_price)}
        add_log("INFO", f"Replacing order {order_id} with new SL {stop_price}")
        return await self._request("PATCH", f"/v2/orders/{order_id}", payload=payload)

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

        if stop_loss_price and take_profit_price:
            payload["order_class"] = "bracket"
            payload["take_profit"] = {"limit_price": str(take_profit_price)}
            payload["stop_loss"] = {"stop_price": str(stop_loss_price)}
        elif stop_loss_price:
            payload["order_class"] = "oto"
            payload["stop_loss"] = {"stop_price": str(stop_loss_price)}
        elif take_profit_price:
            payload["order_class"] = "oto"
            payload["take_profit"] = {"limit_price": str(take_profit_price)}

        msg = (
            f"Submitting {side.upper()} {qty}×{symbol} (type={order_type})"
            + (f" SL={stop_loss_price}" if stop_loss_price else "")
            + (f" TP={take_profit_price}" if take_profit_price else "")
        )
        add_log("INFO", msg)
        return await self._request("POST", "/v2/orders", payload)

    async def close_position(self, symbol: str) -> Dict[str, Any]:
        """Close an entire position via Alpaca API."""
        add_log("INFO", f"Closing entire position for {symbol}")
        return await self._request("DELETE", f"/v2/positions/{symbol}", payload={"cancel_orders": "true"})

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_historical_bars(
        self, symbol: str, timeframe: str = "1Day", limit: int = 250
    ) -> List[Dict[str, Any]]:
        bars_dict = await self.get_historical_bars_multi([symbol], timeframe, limit)
        return bars_dict.get(symbol, [])

    async def get_historical_bars_multi(
        self, symbols: List[str], timeframe: str = "1Day", limit: int = 250
    ) -> Dict[str, List[Dict[str, Any]]]:
        # Calculate how far back to look based on timeframe and limit.
        # Trading day ≈ 6.5 regular-session hours, 26 × 15-min bars.
        # We multiply by 1.5 for safety (weekends, holidays, gaps).
        if timeframe == "1Day":
            days_back = limit * 2
        elif timeframe in ("1Hour", "1H"):
            days_back = max(int(limit / 6.5 * 1.5), 60)
        else:  # 15Min, 5Min, etc.
            days_back = max(limit // 4, 30)
        from datetime import timezone
        start = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%SZ")

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
