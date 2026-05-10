from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, date
from typing import List
import asyncio
import logging
import os
import time
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape

load_dotenv()  # Load environment variables from .env file

from database import get_db, create_tables
from models import User, PnLEntry, Earnings
from auth import get_discord_oauth_url, exchange_code_for_token, get_discord_user_info, get_or_create_user
from calendar_utils import get_5_year_trading_calendar
from polygon_client import PolygonClient, EarningsItem
import aiohttp

log = logging.getLogger("gainers")

app = FastAPI()

# Add session middleware
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "your-secret-key"))

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates
templates = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html", "xml"]),
)

# Global polygon client
polygon_api_key = os.getenv("POLYGON_API_KEY")
polygon_client = None
polygon_session = None

# Discord config for gainers board — uses its OWN bots and channels, separate from the scanner
GAINERS_TOKEN_SMALL   = os.getenv("GAINERS_TOKEN_SMALL", "").strip()
GAINERS_TOKEN_MID     = os.getenv("GAINERS_TOKEN_MID", "").strip()
GAINERS_CHANNEL_SMALL = int(os.getenv("GAINERS_CHANNEL_SMALL") or "0")
GAINERS_CHANNEL_MID   = int(os.getenv("GAINERS_CHANNEL_MID") or "0")
GAINERS_REFRESH_SEC      = float(os.getenv("GAINERS_REFRESH_SEC", "45"))
GAINERS_DISCORD_POST_SEC = float(os.getenv("GAINERS_DISCORD_POST_SEC", "300"))

# In-memory cache — populated by background task, served instantly to the page
_gainers_cache: dict = {"small": [], "mid": [], "last_updated": None}
_discord_msg_ids: dict = {"small": None, "mid": None}
_last_discord_post: float = 0.0

@app.on_event("startup")
async def startup_event():
    global polygon_client, polygon_session
    polygon_session = aiohttp.ClientSession()
    polygon_client = PolygonClient(polygon_api_key, polygon_session)
    create_tables()
    asyncio.create_task(_gainers_background_task())

@app.on_event("shutdown")
async def shutdown_event():
    global polygon_session
    if polygon_session is not None:
        await polygon_session.close()

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    content = templates.get_template("index.html").render(request=request)
    return HTMLResponse(content)

@app.get("/login")
async def login():
    return RedirectResponse(get_discord_oauth_url())

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")

@app.get("/auth/callback")
async def auth_callback(code: str, request: Request):
    try:
        token_data = exchange_code_for_token(code)
        access_token = token_data["access_token"]
        discord_user = get_discord_user_info(access_token)
        user = get_or_create_user(discord_user)
        request.session["user_id"] = user.id
        return RedirectResponse("/calendar")
    except Exception as e:
        raise HTTPException(status_code=400, detail="Authentication failed")

@app.get("/calendar", response_class=HTMLResponse)
async def calendar(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse("/login")
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/login")
    
    # Get trading days
    trading_days = get_5_year_trading_calendar()
    
    # Get earnings for the period
    start_date = datetime.now().date()
    end_date = start_date.replace(year=start_date.year + 5)
    earnings = await polygon_client.earnings_calendar(start_date.isoformat(), end_date.isoformat())
    
    # Group earnings by date in JSON-friendly format
    earnings_by_date = {}
    for e in earnings:
        d = date.fromisoformat(e.report_date).isoformat()
        if d not in earnings_by_date:
            earnings_by_date[d] = []
        earnings_by_date[d].append(e.symbol)
    
    # Get user's P&L entries
    pnl_entries = db.query(PnLEntry).filter(PnLEntry.user_id == user_id).all()
    pnl_by_date = {}
    for entry in pnl_entries:
        key = entry.date.isoformat()
        pnl_by_date[key] = {
            "wins": entry.wins,
            "losses": entry.losses,
            "profit": entry.profit,
        }
    
    content = templates.get_template("calendar.html").render({
        "request": request,
        "user": user,
        "trading_days": trading_days,
        "earnings_by_date": earnings_by_date,
        "pnl_by_date": pnl_by_date
    })
    return HTMLResponse(content)

@app.post("/log_pnl")
async def log_pnl(
    date: str = Form(...),
    wins: int = Form(...),
    losses: int = Form(...),
    profit: float = Form(...),
    request: Request = None,
    db: Session = Depends(get_db)
):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    entry_date = datetime.strptime(date, "%Y-%m-%d").date()
    existing = db.query(PnLEntry).filter(PnLEntry.user_id == user_id, PnLEntry.date == entry_date).first()
    if existing:
        existing.wins = wins
        existing.losses = losses
        existing.profit = profit
    else:
        entry = PnLEntry(user_id=user_id, date=entry_date, wins=wins, losses=losses, profit=profit)
        db.add(entry)
    db.commit()
    return {"status": "success"}

@app.post("/clear_pnl")
async def clear_pnl(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    data = await request.json()
    entry_date = datetime.strptime(data.get("date", ""), "%Y-%m-%d").date()
    existing = db.query(PnLEntry).filter(PnLEntry.user_id == user_id, PnLEntry.date == entry_date).first()
    if existing:
        db.delete(existing)
        db.commit()
    return {"status": "success"}

@app.get("/equity_curve")
async def equity_curve(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    from sqlalchemy import func
    
    # Get all unique dates from all users
    all_dates = sorted([d[0] for d in db.query(PnLEntry.date).distinct().all()])
    
    if not all_dates:
        return {"dates": [], "user": [], "collective": []}
    
    # Build user equity curve
    user_cumulative = 0
    user_equity = []
    user_entries = db.query(PnLEntry).filter(PnLEntry.user_id == user_id).order_by(PnLEntry.date).all()
    user_entry_map = {e.date: e.profit for e in user_entries}
    
    for date in all_dates:
        if date in user_entry_map:
            user_cumulative += user_entry_map[date]
        user_equity.append(user_cumulative)
    
    # Build collective (community average) equity curve
    collective_equity = []
    for date in all_dates:
        # Get all entries up to and including this date from all users
        all_entries_to_date = db.query(PnLEntry).filter(PnLEntry.date <= date).all()
        
        # Group by user and sum their profits
        user_profits = {}
        for entry in all_entries_to_date:
            if entry.user_id not in user_profits:
                user_profits[entry.user_id] = 0
            user_profits[entry.user_id] += entry.profit
        
        # Calculate average across all users
        if user_profits:
            avg = sum(user_profits.values()) / len(user_profits)
            collective_equity.append(avg)
        else:
            collective_equity.append(0)
    
    return {
        "dates": [d.isoformat() for d in all_dates],
        "user": user_equity,
        "collective": collective_equity
    }

@app.get("/top_traders")
async def top_traders(request: Request, db: Session = Depends(get_db)):
    from sqlalchemy import func
    from datetime import timedelta
    now = datetime.now().date()

    # Calculate last Monday for weekly start
    days_since_monday = now.weekday()  # 0=Monday
    last_monday = now - timedelta(days=days_since_monday)
    week_start = last_monday

    # Weekly top 10
    weekly_query = db.query(
        User.username,
        func.sum(PnLEntry.profit).label("total_profit"),
        func.sum(PnLEntry.wins).label("total_wins"),
        func.sum(PnLEntry.losses).label("total_losses")
    ).join(PnLEntry).filter(
        PnLEntry.date >= week_start
    ).group_by(User.id).order_by(func.sum(PnLEntry.profit).desc()).limit(10).all()

    weekly = []
    for w in weekly_query:
        total_trades = w.total_wins + w.total_losses
        win_rate = (w.total_wins / total_trades * 100) if total_trades > 0 else 0
        weekly.append({
            "username": w.username,
            "total_profit": w.total_profit,
            "win_rate": round(win_rate, 2)
        })

    # Monthly top 3 (last 30 days)
    month_start = now - timedelta(days=30)
    monthly_query = db.query(
        User.username,
        func.sum(PnLEntry.profit).label("total_profit"),
        func.sum(PnLEntry.wins).label("total_wins"),
        func.sum(PnLEntry.losses).label("total_losses")
    ).join(PnLEntry).filter(
        PnLEntry.date >= month_start
    ).group_by(User.id).order_by(func.sum(PnLEntry.profit).desc()).limit(3).all()

    monthly = []
    for m in monthly_query:
        total_trades = m.total_wins + m.total_losses
        win_rate = (m.total_wins / total_trades * 100) if total_trades > 0 else 0
        monthly.append({
            "username": m.username,
            "total_profit": m.total_profit,
            "win_rate": round(win_rate, 2)
        })

    return {"weekly": weekly, "monthly": monthly}

@app.get("/top_gainers")
async def top_gainers_page(request: Request):
    content = templates.get_template("top_gainers.html").render(request=request)
    return HTMLResponse(content)

@app.get("/api/top_gainers")
async def get_top_gainers(cap_type: str = "small"):
    """Serve cached gainers — populated by background task every ~45s."""
    gainers = _gainers_cache.get(cap_type, [])
    return {
        "gainers": gainers,
        "last_updated": _gainers_cache.get("last_updated"),
        "timestamp": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Gainers background task
# ---------------------------------------------------------------------------

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


async def _build_gainers_data():
    """
    1. Pull full US snapshot
    2. Pre-filter: gain ≥ 1%, price ≥ $0.50, day vol ≥ 50K
    3. Take top 250 by % gain
    4. Concurrently fetch ticker_details (24h cached) to get market cap
    5. Split into small (<$2B) and mid ($2B–$10B), top 20 each
    6. Fetch 3-month avg volume only for the final ~40 tickers
    """
    from datetime import datetime, timezone as _tz

    snapshot = await polygon_client.snapshot_all_us()

    # Detect session: regular hours = 9:30am–4:00pm ET (13:30–20:00 UTC)
    # Lower the volume bar for premarket / after-hours so we still surface movers
    hour_utc = datetime.now(_tz.utc).hour + datetime.now(_tz.utc).minute / 60.0
    extended_hours = not (13.5 <= hour_utc <= 20.0)
    min_vol = 5_000 if extended_hours else 50_000

    candidates = []
    for t in snapshot:
        day       = t.get("day") or {}
        last_trade = t.get("lastTrade") or {}
        prev_day  = t.get("prevDay") or {}
        minute    = t.get("min") or {}

        price      = last_trade.get("p") or day.get("c") or 0.0
        prev_close = prev_day.get("c") or 0.0

        # todaysChangePerc is stale pre-market — recalculate from last trade vs prev close
        chg_pct = t.get("todaysChangePerc") or 0.0
        chg_abs = t.get("todaysChange") or 0.0
        if prev_close and price and (extended_hours or abs(chg_pct) < 0.01):
            chg_pct = (price - prev_close) / prev_close * 100.0
            chg_abs = price - prev_close

        if chg_pct < 1.0:
            continue
        if price < 0.50:
            continue

        # day.v is 0 pre-market — fall back to accumulated minute volume
        vol = day.get("v") or minute.get("av") or 0
        if vol < min_vol:
            continue

        # stash computed values so we don't recalculate below
        t["_chg_pct"] = chg_pct
        t["_chg_abs"] = chg_abs
        t["_price"]   = price
        t["_vol"]     = vol
        candidates.append(t)

    candidates.sort(key=lambda x: x["_chg_pct"], reverse=True)
    candidates = candidates[:250]

    sem = asyncio.Semaphore(25)

    async def _fetch_details(t):
        sym = t.get("ticker", "")
        async with sem:
            try:
                return t, await polygon_client.ticker_details(sym)
            except Exception:
                return t, None

    results = await asyncio.gather(*[_fetch_details(c) for c in candidates])

    small_gainers, mid_gainers = [], []

    for t, details in results:
        if not details or not details.market_cap:
            continue
        mc      = details.market_cap
        price   = t["_price"]
        chg_pct = t["_chg_pct"]
        chg_abs = t["_chg_abs"]
        vol     = t["_vol"]

        entry = {
            "symbol":     details.symbol,
            "name":       details.name,
            "price":      round(price, 2),
            "change":     round(chg_abs, 2),
            "change_pct": round(chg_pct, 2),
            "volume":     _fmt_vol(vol),
            "avg_vol_3m": "—",
            "market_cap": _fmt_mktcap(mc),
        }

        if mc < 2_000_000_000:
            small_gainers.append(entry)
        elif mc <= 10_000_000_000:
            mid_gainers.append(entry)

    small_gainers.sort(key=lambda x: x["change_pct"], reverse=True)
    mid_gainers.sort(key=lambda x: x["change_pct"], reverse=True)
    small_gainers = small_gainers[:20]
    mid_gainers   = mid_gainers[:20]

    # Fetch 3-month avg volume only for the final ~40 tickers
    async def _enrich_avgvol(entry):
        async with sem:
            try:
                avg = await polygon_client.avg_volume_3m(entry["symbol"])
                entry["avg_vol_3m"] = _fmt_vol(avg) if avg else "—"
            except Exception:
                pass

    await asyncio.gather(*[_enrich_avgvol(e) for e in small_gainers + mid_gainers])

    return small_gainers, mid_gainers


async def _discord_post_gainers(cap_type: str, gainers: list) -> None:
    """Post or edit the leaderboard embed in the appropriate Discord channel."""
    global _discord_msg_ids
    token      = GAINERS_TOKEN_SMALL if cap_type == "small" else GAINERS_TOKEN_MID
    channel_id = GAINERS_CHANNEL_SMALL if cap_type == "small" else GAINERS_CHANNEL_MID
    if not token or not channel_id:  # not configured — skip silently
        return

    label = "Small Cap  <$2B" if cap_type == "small" else "Mid Cap  $2B–$10B"
    emoji = "🚀" if cap_type == "small" else "📈"
    now_str = datetime.now().strftime("%I:%M:%S %p ET")

    rows = ["```", f"{'#':<3} {'SYM':<6} {'PRICE':>9} {'CHG$':>9} {'CHG%':>8}  {'VOLUME':>9}  {'AVG 3M':>9}  {'MKT CAP':>10}", "─" * 72]
    for i, g in enumerate(gainers, 1):
        sign = "+" if g["change_pct"] >= 0 else ""
        rows.append(
            f"{i:<3} {g['symbol']:<6} ${g['price']:>8.2f} "
            f"{sign}${abs(g['change']):>7.2f} {sign}{g['change_pct']:>6.2f}%  "
            f"{g['volume']:>9}  {g['avg_vol_3m']:>9}  {g['market_cap']:>10}"
        )
    rows.append("```")

    content = (
        f"**{emoji}  Flowstate Alpha — Top {len(gainers)} {label} Gainers**\n"
        f"Updated: `{now_str}`\n" + "\n".join(rows)
    )

    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        msg_id = _discord_msg_ids.get(cap_type)
        if msg_id:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{msg_id}"
            async with session.patch(url, headers=headers, json={"content": content}) as r:
                if r.status == 200:
                    return
                # Message gone — fall through to create a new one
                _discord_msg_ids[cap_type] = None

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        async with session.post(url, headers=headers, json={"content": content}) as r:
            if r.status in (200, 201):
                data = await r.json()
                _discord_msg_ids[cap_type] = data.get("id")
            else:
                log.warning("Discord post failed for %s: %s", cap_type, r.status)


async def _gainers_background_task() -> None:
    """Refresh gainers cache every GAINERS_REFRESH_SEC; post to Discord every GAINERS_DISCORD_POST_SEC."""
    global _last_discord_post
    while True:
        try:
            small, mid = await _build_gainers_data()
            _gainers_cache["small"] = small
            _gainers_cache["mid"] = mid
            _gainers_cache["last_updated"] = datetime.now().isoformat()

            now = time.time()
            if (GAINERS_TOKEN_SMALL or GAINERS_TOKEN_MID) and (now - _last_discord_post) >= GAINERS_DISCORD_POST_SEC:
                _last_discord_post = now
                asyncio.create_task(_discord_post_gainers("small", small))
                asyncio.create_task(_discord_post_gainers("mid", mid))
        except Exception as e:
            log.error("Gainers refresh failed: %s", e)

        await asyncio.sleep(GAINERS_REFRESH_SEC)