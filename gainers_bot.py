"""
Flowstate Alpha — Top Gainers Discord Bot
==========================================
Standalone process. Completely independent from the P&L calendar and scanner.

Posts/edits a live Top 20 leaderboard into two dedicated Discord channels:
  - #small-cap-gainers  (market cap < $2B)
  - #mid-cap-gainers    (market cap $2B – $10B)

Each channel has its own dedicated bot token so they post independently.

Required env vars:
  POLYGON_API_KEY
  GAINERS_TOKEN_SMALL      — bot token for #small-cap-gainers
  GAINERS_TOKEN_MID        — bot token for #mid-cap-gainers
  GAINERS_CHANNEL_SMALL    — channel ID for #small-cap-gainers
  GAINERS_CHANNEL_MID      — channel ID for #mid-cap-gainers

Optional env vars (defaults shown):
  GAINERS_REFRESH_SEC=45   — how often to pull fresh data from Polygon
  GAINERS_POST_SEC=300     — how often to edit the Discord message
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from polygon_client import PolygonClient, PolygonError

# ── Config ───────────────────────────────────────────────────────────────────
POLYGON_API_KEY      = os.getenv("POLYGON_API_KEY", "").strip()
GAINERS_TOKEN_SMALL  = os.getenv("GAINERS_TOKEN_SMALL", "").strip()
GAINERS_TOKEN_MID    = os.getenv("GAINERS_TOKEN_MID", "").strip()
GAINERS_CHANNEL_SMALL = int(os.getenv("GAINERS_CHANNEL_SMALL") or "0")
GAINERS_CHANNEL_MID   = int(os.getenv("GAINERS_CHANNEL_MID") or "0")
GAINERS_REFRESH_SEC  = float(os.getenv("GAINERS_REFRESH_SEC", "45"))
GAINERS_POST_SEC     = float(os.getenv("GAINERS_POST_SEC", "300"))

_ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] gainers: %(message)s",
)
log = logging.getLogger("gainers")


# ── Market hours ─────────────────────────────────────────────────────────────
def _is_trading_session() -> bool:
    """True Mon–Fri 4:00 AM – 8:00 PM ET (premarket + regular + after-hours)."""
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 4 * 60 <= mins <= 20 * 60


# ── Formatters ───────────────────────────────────────────────────────────────
def _fmt_vol(n) -> str:
    if n is None:
        return "—"
    n = float(n)
    if n >= 1_000_000_000:
        return f"{n/1e9:.3f}B"
    if n >= 1_000_000:
        return f"{n/1e6:.3f}M"
    if n >= 1_000:
        return f"{n/1e3:.0f}K"
    return f"{n:.0f}"


def _fmt_mktcap(mc: float) -> str:
    if mc >= 1_000_000_000_000:
        return f"{mc/1e12:.3f}T"
    if mc >= 1_000_000_000:
        return f"{mc/1e9:.3f}B"
    if mc >= 1_000_000:
        return f"{mc/1e6:.3f}M"
    return f"{mc/1e3:.0f}K"


# ── Data fetching ─────────────────────────────────────────────────────────────
async def _build_gainers(polygon: PolygonClient) -> tuple[list, list]:
    """
    Returns (small_gainers, mid_gainers) — top 20 each, sorted by % gain.
    Works during premarket, regular session, and after-hours.
    """
    snapshot = await polygon.snapshot_all_us()

    # Lower volume bar during extended hours (pre/after market)
    hour_utc = datetime.now(timezone.utc).hour + datetime.now(timezone.utc).minute / 60.0
    extended = not (13.5 <= hour_utc <= 20.0)
    min_vol = 5_000 if extended else 50_000

    candidates = []
    for t in snapshot:
        day        = t.get("day") or {}
        last_trade = t.get("lastTrade") or {}
        prev_day   = t.get("prevDay") or {}
        minute     = t.get("min") or {}

        price      = last_trade.get("p") or day.get("c") or 0.0
        prev_close = prev_day.get("c") or 0.0

        # Recalculate change ourselves during extended hours (todaysChangePerc goes stale)
        chg_pct = t.get("todaysChangePerc") or 0.0
        chg_abs = t.get("todaysChange") or 0.0
        if prev_close and price and (extended or abs(chg_pct) < 0.01):
            chg_pct = (price - prev_close) / prev_close * 100.0
            chg_abs = price - prev_close

        if chg_pct < 1.0 or price < 0.50:
            continue

        vol = day.get("v") or minute.get("av") or 0
        if vol < min_vol:
            continue

        t["_chg_pct"] = chg_pct
        t["_chg_abs"] = chg_abs
        t["_price"]   = price
        t["_vol"]     = vol
        candidates.append(t)

    candidates.sort(key=lambda x: x["_chg_pct"], reverse=True)
    candidates = candidates[:250]

    sem = asyncio.Semaphore(25)

    async def _get_details(t):
        async with sem:
            try:
                return t, await polygon.ticker_details(t.get("ticker", ""))
            except Exception:
                return t, None

    results = await asyncio.gather(*[_get_details(c) for c in candidates])

    small, mid = [], []
    for t, details in results:
        if not details or not details.market_cap:
            continue
        mc = details.market_cap
        entry = {
            "symbol":     details.symbol,
            "name":       details.name,
            "price":      round(t["_price"], 2),
            "change":     round(t["_chg_abs"], 2),
            "change_pct": round(t["_chg_pct"], 2),
            "volume":     _fmt_vol(t["_vol"]),
            "avg_vol_3m": "—",
            "market_cap": _fmt_mktcap(mc),
        }
        if mc < 2_000_000_000:
            small.append(entry)
        elif mc <= 10_000_000_000:
            mid.append(entry)

    small.sort(key=lambda x: x["change_pct"], reverse=True)
    mid.sort(key=lambda x: x["change_pct"], reverse=True)
    small = small[:20]
    mid   = mid[:20]

    # 3-month avg volume for final ~40 tickers only
    async def _enrich(entry):
        async with sem:
            try:
                avg = await polygon.avg_volume_3m(entry["symbol"])
                entry["avg_vol_3m"] = _fmt_vol(avg) if avg else "—"
            except Exception:
                pass

    await asyncio.gather(*[_enrich(e) for e in small + mid])
    return small, mid


# ── Discord posting ───────────────────────────────────────────────────────────
def _build_message(cap_type: str, gainers: list) -> str:
    label = "Small Cap  <$2B" if cap_type == "small" else "Mid Cap  $2B–$10B"
    emoji = "🚀" if cap_type == "small" else "📈"
    now_str = datetime.now(_ET).strftime("%I:%M:%S %p ET")

    header = f"{'#':<3} {'SYM':<6} {'PRICE':>9} {'CHG$':>9} {'CHG%':>8}  {'VOLUME':>9}  {'AVG 3M':>9}  {'MKT CAP':>10}"
    divider = "─" * 72
    rows = [header, divider]
    for i, g in enumerate(gainers, 1):
        sign = "+" if g["change_pct"] >= 0 else ""
        rows.append(
            f"{i:<3} {g['symbol']:<6} ${g['price']:>8.2f} "
            f"{sign}${abs(g['change']):>7.2f} {sign}{g['change_pct']:>6.2f}%  "
            f"{g['volume']:>9}  {g['avg_vol_3m']:>9}  {g['market_cap']:>10}"
        )

    table = "```\n" + "\n".join(rows) + "\n```"
    return (
        f"**{emoji}  Flowstate Alpha — Top {len(gainers)} {label} Gainers**\n"
        f"Updated: `{now_str}`\n{table}"
    )


async def _post_or_edit(
    session: aiohttp.ClientSession,
    token: str,
    channel_id: int,
    content: str,
    msg_ids: dict,
    cap_type: str,
) -> None:
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    msg_id = msg_ids.get(cap_type)
    if msg_id:
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}"
        async with session.patch(url, headers=headers, json={"content": content}) as r:
            if r.status == 200:
                return
            msg_ids[cap_type] = None  # message gone, create a new one

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    async with session.post(url, headers=headers, json={"content": content}) as r:
        if r.status in (200, 201):
            data = await r.json()
            msg_ids[cap_type] = data.get("id")
            log.info("Posted new %s leaderboard (msg_id=%s)", cap_type, msg_ids[cap_type])
        else:
            body = await r.text()
            log.warning("Discord post failed [%s] %s: %s", cap_type, r.status, body[:200])


# ── Main loop ────────────────────────────────────────────────────────────────
async def _run(stop_evt: asyncio.Event) -> None:
    msg_ids: dict = {"small": None, "mid": None}
    last_post = 0.0

    async with aiohttp.ClientSession() as http:
        polygon = PolygonClient(POLYGON_API_KEY, http)
        log.info("Gainers bot started. Refresh=%.0fs  Post=%.0fs", GAINERS_REFRESH_SEC, GAINERS_POST_SEC)

        while not stop_evt.is_set():
            if not _is_trading_session():
                log.debug("Outside trading hours — sleeping 60s")
                try:
                    await asyncio.wait_for(stop_evt.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                small, mid = await _build_gainers(polygon)
                log.info("Fetched gainers: small=%d  mid=%d", len(small), len(mid))

                now = time.time()
                if now - last_post >= GAINERS_POST_SEC:
                    last_post = now
                    if GAINERS_TOKEN_SMALL and GAINERS_CHANNEL_SMALL:
                        await _post_or_edit(
                            http, GAINERS_TOKEN_SMALL, GAINERS_CHANNEL_SMALL,
                            _build_message("small", small), msg_ids, "small",
                        )
                    if GAINERS_TOKEN_MID and GAINERS_CHANNEL_MID:
                        await _post_or_edit(
                            http, GAINERS_TOKEN_MID, GAINERS_CHANNEL_MID,
                            _build_message("mid", mid), msg_ids, "mid",
                        )
            except PolygonError as e:
                log.warning("Polygon error: %s", e)
            except Exception as e:
                log.exception("Unexpected error: %s", e)

            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=GAINERS_REFRESH_SEC)
            except asyncio.TimeoutError:
                pass


async def _amain() -> int:
    if not POLYGON_API_KEY:
        log.error("POLYGON_API_KEY is required.")
        return 1
    if not (GAINERS_TOKEN_SMALL or GAINERS_TOKEN_MID):
        log.error("At least one of GAINERS_TOKEN_SMALL or GAINERS_TOKEN_MID is required.")
        return 1

    stop_evt = asyncio.Event()

    def _stop(*_):
        log.info("Shutdown signal — stopping.")
        stop_evt.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())

    await _run(stop_evt)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
