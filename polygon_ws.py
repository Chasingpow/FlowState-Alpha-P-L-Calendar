"""
Polygon.io WebSocket client — real-time per-second stock aggregate bars.

Connects to wss://socket.polygon.io/stocks, authenticates, subscribes to
A.* (all tickers, per-second bars), and calls on_bar(event_dict) for each A
event. Reconnects automatically with exponential backoff.

A event fields used by the scanner:
  sym  — ticker symbol
  c    — close price this second (current price)
  h    — high price this second  (NOT the day's high)
  av   — accumulated day volume
  op   — official open price for the day
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable

import aiohttp

log = logging.getLogger("polygon_ws")

WS_URL = "wss://socket.polygon.io/stocks"


class PolygonWSClient:
    def __init__(self, api_key: str, on_bar: Callable[[dict], Any]):
        self.api_key = api_key
        self.on_bar = on_bar  # sync callback — called per A event

    async def run(self, stop_evt: asyncio.Event) -> None:
        """Connect, authenticate, subscribe, and stream events. Reconnects on error."""
        backoff = 1.0
        while not stop_evt.is_set():
            try:
                await self._connect_and_listen(stop_evt)
                backoff = 1.0  # clean disconnect — reset backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("WebSocket error: %s — reconnecting in %.0fs", e, backoff)
            if stop_evt.is_set():
                break
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60.0)
        log.info("WebSocket loop exiting.")

    async def _connect_and_listen(self, stop_evt: asyncio.Event) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(WS_URL, heartbeat=20) as ws:
                log.info("WebSocket connected — authenticating...")
                await ws.send_str(json.dumps({"action": "auth", "params": self.api_key}))
                await ws.send_str(json.dumps({"action": "subscribe", "params": "A.*"}))

                async for msg in ws:
                    if stop_evt.is_set():
                        await ws.close()
                        return
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            events = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        for ev in events:
                            ev_type = ev.get("ev")
                            if ev_type == "A":
                                self.on_bar(ev)
                            elif ev_type in ("connected", "auth_success", "auth_failed",
                                             "success", "error"):
                                log.info("WS status: %s", ev)
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        log.info("WebSocket closed by server")
                        return
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        log.warning("WebSocket protocol error: %s", msg.data)
                        return
