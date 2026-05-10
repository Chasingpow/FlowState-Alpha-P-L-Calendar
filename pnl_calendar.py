from fastapi import FastAPI, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from sqlalchemy.orm import Session
from datetime import datetime, date
from typing import List
import os
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader, select_autoescape

load_dotenv()

from database import get_db, create_tables
from models import User, PnLEntry, Earnings
from auth import get_discord_oauth_url, exchange_code_for_token, get_discord_user_info, get_or_create_user
from calendar_utils import get_5_year_trading_calendar
from polygon_client import PolygonClient, EarningsItem
import aiohttp

app = FastAPI()

app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "your-secret-key"))
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html", "xml"]),
)

polygon_api_key = os.getenv("POLYGON_API_KEY")
polygon_client = None
polygon_session = None

@app.on_event("startup")
async def startup_event():
    global polygon_client, polygon_session
    polygon_session = aiohttp.ClientSession()
    polygon_client = PolygonClient(polygon_api_key, polygon_session)
    create_tables()

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

    trading_days = get_5_year_trading_calendar()

    start_date = datetime.now().date()
    end_date = start_date.replace(year=start_date.year + 5)
    earnings = await polygon_client.earnings_calendar(start_date.isoformat(), end_date.isoformat())

    earnings_by_date = {}
    for e in earnings:
        d = date.fromisoformat(e.report_date).isoformat()
        if d not in earnings_by_date:
            earnings_by_date[d] = []
        earnings_by_date[d].append(e.symbol)

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

    all_dates = sorted([d[0] for d in db.query(PnLEntry.date).distinct().all()])

    if not all_dates:
        return {"dates": [], "user": [], "collective": []}

    user_cumulative = 0
    user_equity = []
    user_entries = db.query(PnLEntry).filter(PnLEntry.user_id == user_id).order_by(PnLEntry.date).all()
    user_entry_map = {e.date: e.profit for e in user_entries}

    for date in all_dates:
        if date in user_entry_map:
            user_cumulative += user_entry_map[date]
        user_equity.append(user_cumulative)

    collective_equity = []
    for date in all_dates:
        all_entries_to_date = db.query(PnLEntry).filter(PnLEntry.date <= date).all()
        user_profits = {}
        for entry in all_entries_to_date:
            if entry.user_id not in user_profits:
                user_profits[entry.user_id] = 0
            user_profits[entry.user_id] += entry.profit
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

    days_since_monday = now.weekday()
    last_monday = now - timedelta(days=days_since_monday)
    week_start = last_monday

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
