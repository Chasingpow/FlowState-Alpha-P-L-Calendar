"""
Quiver Quantitative client — congressional trades and government contracts.

Adds high-conviction catalyst signals independent of news:
  - Congress buys:  politicians purchased stock within last 30 days
  - Gov contracts:  government contract awarded within last 30 days

API key required: https://api.quiverquant.com (~$30/month)
Set QUIVER_API_KEY in your .env file.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Optional

import aiohttp

log = logging.getLogger("quiver")

BASE         = "https://api.quiverquant.com/beta"
LOOKBACK_DAYS = 30


@dataclass
class QuiverSignal:
    congress_buy:    bool = False
    gov_contract:    bool = False
    congress_detail: str  = ""
    contract_detail: str  = ""

    @property
    def has_signal(self) -> bool:
        return self.congress_buy or self.gov_contract

    def summary(self) -> str:
        parts = []
        if self.congress_buy:
            parts.append(f"🏛️ **Congress:** {self.congress_detail}")
        if self.gov_contract:
            parts.append(f"📋 **Gov Contract:** {self.contract_detail}")
        return "\n".join(parts)


class QuiverClient:
    def __init__(self, api_key: str, session: aiohttp.ClientSession):
        self.api_key = api_key
        self.session = session
        self._cache: dict[str, tuple[float, QuiverSignal]] = {}
        self._ttl = 3600.0  # data is updated daily; 1h cache is plenty

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json"}

    async def _get(self, path: str) -> Optional[list]:
        url = f"{BASE}{path}"
        try:
            async with self.session.get(
                url, headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=6),
            ) as r:
                if r.status == 200:
                    return await r.json()
                if r.status == 401:
                    log.warning("Quiver: 401 Unauthorized — check QUIVER_API_KEY")
                if r.status == 404:
                    return []
                log.debug("Quiver %s → HTTP %s", path, r.status)
                return None
        except asyncio.TimeoutError:
            log.debug("Quiver timeout: %s", path)
            return None
        except Exception as e:
            log.debug("Quiver error %s: %s", path, e)
            return None

    async def check_signals(self, symbol: str) -> QuiverSignal:
        cached = self._cache.get(symbol)
        if cached and (time.time() - cached[0]) < self._ttl:
            return cached[1]

        cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)

        congress, contracts = await asyncio.gather(
            self._get(f"/historical/congresstrading/{symbol}"),
            self._get(f"/historical/govcontractsall/{symbol}"),
            return_exceptions=True,
        )

        sig = QuiverSignal()

        # Congressional trades — look for any Purchase in the last 30 days
        if isinstance(congress, list):
            for trade in congress:
                raw_date = (trade.get("TransactionDate")
                            or trade.get("ReportDate")
                            or trade.get("Date") or "")
                try:
                    trade_date = date.fromisoformat(str(raw_date)[:10])
                except (ValueError, TypeError):
                    continue
                if trade_date < cutoff:
                    continue
                txn = (trade.get("Transaction") or "").lower()
                if "purchase" in txn or "buy" in txn:
                    rep    = trade.get("Representative") or trade.get("Politician") or "Congress"
                    amount = trade.get("Range") or trade.get("Amount") or ""
                    sig.congress_buy    = True
                    sig.congress_detail = f"{rep} · {amount}".strip(" ·")
                    break

        # Government contracts — look for any contract ≥ $100K in last 30 days
        if isinstance(contracts, list):
            for c in contracts:
                raw_date = (c.get("Date") or c.get("TransactionDate") or "")
                try:
                    contract_date = date.fromisoformat(str(raw_date)[:10])
                except (ValueError, TypeError):
                    continue
                if contract_date < cutoff:
                    continue
                try:
                    amt = float(str(c.get("Amount") or c.get("Value") or 0)
                                .replace(",", "").replace("$", ""))
                except (ValueError, TypeError):
                    amt = 0.0
                if amt >= 100_000:
                    agency = c.get("Agency") or c.get("Dept") or "Gov"
                    sig.gov_contract    = True
                    sig.contract_detail = f"${amt / 1_000_000:.1f}M · {agency}"
                    break

        self._cache[symbol] = (time.time(), sig)
        if sig.has_signal:
            log.info("Quiver signal for %s: congress=%s contract=%s",
                     symbol, sig.congress_buy, sig.gov_contract)
        return sig
