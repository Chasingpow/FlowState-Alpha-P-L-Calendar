"""
Async Polygon.io client.

Provides:
- snapshot_all_us(): full universe snapshot, called every poll
- ticker_details(symbol): per-ticker reference data, cached for 24h
- avg_volume_20d(symbol): 20-day avg volume for RVOL calc, cached for 24h
- latest_news(symbol): most recent news item for the symbol, cached for 5m

All methods raise PolygonError on persistent failure. Transient errors are
retried with backoff.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

log = logging.getLogger("polygon")

BASE = "https://api.polygon.io"


class PolygonError(RuntimeError):
    pass


@dataclass
class TickerDetails:
    symbol: str
    name: str
    market_cap: Optional[float]
    shares_outstanding: Optional[float]
    primary_exchange: Optional[str]
    locale: Optional[str]   # 'us', 'global', etc.


@dataclass
class NewsItem:
    title: str
    publisher: str
    published_utc: str  # ISO timestamp
    url: str


@dataclass
class EarningsItem:
    symbol: str
    report_date: str  # YYYY-MM-DD
    fiscal_year: Optional[int]
    fiscal_period: Optional[str]


class _TtlCache:
    """Tiny in-memory TTL cache."""
    def __init__(self, ttl_seconds: float):
        self.ttl = ttl_seconds
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        now = time.time()
        item = self._data.get(key)
        if item is None:
            return None
        ts, val = item
        if now - ts > self.ttl:
            self._data.pop(key, None)
            return None
        return val

    def set(self, key: str, val: Any) -> None:
        self._data[key] = (time.time(), val)


class PolygonClient:
    def __init__(self, api_key: str, session: aiohttp.ClientSession):
        if not api_key:
            raise PolygonError("POLYGON_API_KEY required")
        self.api_key = api_key
        self.session = session
        self._details_cache = _TtlCache(60 * 60 * 24)   # 24h
        self._avgvol_cache = _TtlCache(60 * 60 * 24)    # 24h
        self._news_cache = _TtlCache(60 * 5)            # 5m

    async def _get(self, path: str, params: dict | None = None,
                   retries: int = 3) -> dict:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        url = f"{BASE}{path}"
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                async with self.session.get(url, params=params,
                                             timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 429:
                        wait = 2 ** attempt
                        log.warning("Polygon 429 on %s — backing off %ss", path, wait)
                        await asyncio.sleep(wait)
                        continue
                    r.raise_for_status()
                    return await r.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_exc = e
                await asyncio.sleep(0.5 * (attempt + 1))
        raise PolygonError(f"Failed GET {path}: {last_exc}")

    # ------------------------------------------------------------------
    async def snapshot_all_us(self) -> list[dict]:
        """Full US stocks snapshot — the firehose. Returns the `tickers` list."""
        data = await self._get("/v2/snapshot/locale/us/markets/stocks/tickers")
        return data.get("tickers", []) or []

    async def ticker_details(self, symbol: str) -> Optional[TickerDetails]:
        cached = self._details_cache.get(symbol)
        if cached is not None:
            return cached
        try:
            data = await self._get(f"/v3/reference/tickers/{symbol}")
        except PolygonError:
            return None
        results = (data or {}).get("results") or {}
        if not results:
            return None
        td = TickerDetails(
            symbol=symbol,
            name=results.get("name") or symbol,
            market_cap=results.get("market_cap"),
            shares_outstanding=results.get("weighted_shares_outstanding")
                                or results.get("share_class_shares_outstanding"),
            primary_exchange=results.get("primary_exchange"),
            locale=results.get("locale"),
        )
        self._details_cache.set(symbol, td)
        return td

    async def avg_volume_20d(self, symbol: str) -> Optional[float]:
        """Average daily volume over the last 20 trading days, cached daily."""
        cached = self._avgvol_cache.get(symbol)
        if cached is not None:
            return cached
        # Pull 30 calendar days of daily aggs to make sure we have ≥ 20 trading days
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=40)
        path = f"/v2/aggs/ticker/{symbol}/range/1/day/{start.isoformat()}/{end.isoformat()}"
        try:
            data = await self._get(path, {"adjusted": "true", "sort": "desc", "limit": 25})
        except PolygonError:
            return None
        results = (data or {}).get("results") or []
        if not results:
            return None
        # Skip today's bar if present (we want 20 *prior* days)
        vols = [r.get("v") for r in results if r.get("v")][:20]
        if not vols:
            return None
        avg = sum(vols) / len(vols)
        self._avgvol_cache.set(symbol, avg)
        return avg

    async def latest_news(self, symbol: str, hours: int = 24) -> Optional[NewsItem]:
        """Most recent news item for the symbol within `hours`. Cached 5m."""
        cached = self._news_cache.get(symbol)
        if cached is not None:
            return cached or None  # cached can be `False` to mean "no news"
        try:
            data = await self._get("/v2/reference/news", {
                "ticker": symbol,
                "limit": 1,
                "order": "desc",
                "sort": "published_utc",
            })
        except PolygonError:
            return None
        results = (data or {}).get("results") or []
        if not results:
            self._news_cache.set(symbol, False)  # negative cache
            return None
        item = results[0]
        from datetime import datetime, timezone, timedelta
        try:
            pub = datetime.fromisoformat(item["published_utc"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - pub > timedelta(hours=hours):
                self._news_cache.set(symbol, False)
                return None
        except (KeyError, ValueError):
            return None
        ni = NewsItem(
            title=item.get("title", ""),
            publisher=(item.get("publisher") or {}).get("name", "Unknown"),
            published_utc=item.get("published_utc", ""),
            url=item.get("article_url", ""),
        )
        self._news_cache.set(symbol, ni)
        return ni

    async def earnings_calendar(self, start_date: str, end_date: str) -> list[EarningsItem]:
        """Fetch earnings calendar between start_date and end_date (YYYY-MM-DD)."""
        path = "/v3/reference/earnings"
        params = {
            "date.gte": start_date,
            "date.lte": end_date,
            "limit": 1000,  # Adjust as needed
        }
        try:
            data = await self._get(path, params)
        except PolygonError:
            return []
        results = (data or {}).get("results") or []
        earnings = []
        for item in results:
            earnings.append(EarningsItem(
                symbol=item.get("ticker", ""),
                report_date=item.get("report_date", ""),
                fiscal_year=item.get("fiscal_year"),
                fiscal_period=item.get("fiscal_period"),
            ))
        return earnings
