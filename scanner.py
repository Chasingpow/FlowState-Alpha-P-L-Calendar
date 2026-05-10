"""Top Gainer Scanner - orchestrator.

Polls Polygon ~once/sec, scores each candidate gainer in two share-price
bands, dispatches to Discord (rich embed + voice) and writes a TradingView
watchlist file.
"""
from __future__ import annotations
import asyncio, json, logging, os, signal, sys, time
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Optional
import aiohttp

_ET = ZoneInfo("America/New_York")

def _is_trading_session() -> bool:
    """True Mon–Fri 4:00 AM – 8:00 PM ET (covers premarket, regular, after-hours)."""
    now = datetime.now(_ET)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    mins = now.hour * 60 + now.minute
    return 4 * 60 <= mins <= 20 * 60   # 4:00am–8:00pm

try:
    from dotenv import load_dotenv; load_dotenv()
except Exception:
    pass

from polygon_client import PolygonClient, PolygonError
from scoring import ScoringConfig, score
from discord_bot import AlertPayload, ScannerBot

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "").strip()
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_SMALL = int(os.getenv("CHANNEL_SMALL") or "0")
CHANNEL_MID = int(os.getenv("CHANNEL_MID") or "0")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID") or "0") or None
SPEAK_MIN_GRADE = os.getenv("SPEAK_MIN_GRADE", "A+").strip()
BOT_DISPLAY_NAME = os.getenv("BOT_DISPLAY_NAME", "").strip() or None

POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "1.0"))
ALERT_COOLDOWN_SEC = float(os.getenv("ALERT_COOLDOWN_SEC", "1800"))
FOLLOWUP_INTERVAL_SEC = float(os.getenv("FOLLOWUP_INTERVAL_SEC", "300"))
ALERT_MIN_GRADE = os.getenv("ALERT_MIN_GRADE", "B").strip()
TOP_N = int(os.getenv("TOP_N", "15"))
MIN_GAIN_PCT = float(os.getenv("MIN_GAIN_PCT", "3.0"))
WATCHLIST_FILE = os.getenv("WATCHLIST_FILE", "watchlist.txt")
STATE_FILE = os.getenv("STATE_FILE", "scanner_state.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

SMALL_CAP_BAND = (1.0, 20.0)
MID_CAP_BAND = (20.01, 300.0)

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("scanner")

GRADE_ORDER = ["F", "D", "C", "B", "A", "A+"]


@dataclass
class TickerState:
    last_alert_grade: Optional[str] = None
    last_alert_time: float = 0.0
    last_followup_time: float = 0.0
    last_high_of_day: float = 0.0
    nhod_streak: int = 0


def grade_at_least(actual, minimum):
    try:
        return GRADE_ORDER.index(actual) >= GRADE_ORDER.index(minimum)
    except ValueError:
        return False


def in_band(price, band):
    return band[0] <= price <= band[1]


def _write_watchlist(small, mid, path):
    try:
        lines = []
        if small:
            lines.append("###Small Cap Gainers," + ",".join(small))
        if mid:
            lines.append("###Mid Cap Gainers," + ",".join(mid))
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as e:
        log.warning("Could not write watchlist: %s", e)


def _save_state(state: dict, path: str) -> None:
    try:
        data = {sym: {
            "last_alert_grade": st.last_alert_grade,
            "last_alert_time": st.last_alert_time,
            "last_followup_time": st.last_followup_time,
            "last_high_of_day": st.last_high_of_day,
            "nhod_streak": st.nhod_streak,
        } for sym, st in state.items()}
        with open(path, "w") as f:
            json.dump(data, f)
    except OSError as e:
        log.warning("Could not save state: %s", e)


def _load_state(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        today = date.today()
        state = {}
        for sym, d in data.items():
            st = TickerState(
                last_alert_grade=d.get("last_alert_grade"),
                last_alert_time=d.get("last_alert_time", 0.0),
                last_followup_time=d.get("last_followup_time", 0.0),
                last_high_of_day=d.get("last_high_of_day", 0.0),
                nhod_streak=d.get("nhod_streak", 0),
            )
            # Reset intraday fields if persisted state is from a prior day
            if st.last_alert_time:
                alert_date = date.fromtimestamp(st.last_alert_time)
                if alert_date < today:
                    st.last_high_of_day = 0.0
                    st.nhod_streak = 0
            state[sym] = st
        log.info("Loaded state for %d tickers from %s", len(state), path)
        return state
    except (OSError, json.JSONDecodeError, KeyError):
        return {}


async def _process_candidate(*, t, band, is_small_cap, polygon, bot, state, cfg):
    symbol = t.get("ticker")
    if not symbol or "." in symbol:
        return None
    last_trade = t.get("lastTrade") or {}
    day = t.get("day") or {}
    prev = t.get("prevDay") or {}
    last_price = float(last_trade.get("p") or day.get("c") or 0.0)
    if last_price <= 0 or not in_band(last_price, band):
        return None
    change_pct = float(t.get("todaysChangePerc") or 0.0)
    change_abs = float(t.get("todaysChange") or 0.0)
    if change_pct < MIN_GAIN_PCT or change_pct > 1000:
        return None

    todays_volume = int(day.get("v") or 0)
    todays_high = float(day.get("h") or 0.0)
    todays_open = day.get("o")
    prev_close = prev.get("c")

    avg_vol, news, details = await asyncio.gather(
        polygon.avg_volume_20d(symbol),
        polygon.latest_news(symbol, hours=24),
        polygon.ticker_details(symbol),
    )

    # Pre-market: day.v=0 means the regular session hasn't opened yet.
    # Derive gap from last_price vs prev_close; bypass volume/RVOL (unavailable).
    pm_mode = todays_volume == 0 and change_pct > 0
    if pm_mode:
        todays_open = todays_open if todays_open is not None else last_price
        score_cfg = ScoringConfig(
            volume_threshold=0,
            rvol_threshold=0.0,
            gap_threshold_pct=cfg.gap_threshold_pct,
            tech_proximity_pct=cfg.tech_proximity_pct,
        )
    else:
        score_cfg = cfg

    s = score(
        has_news=news is not None, todays_volume=todays_volume,
        avg_volume_20d=avg_vol,
        todays_open=float(todays_open) if todays_open is not None else None,
        prev_close=float(prev_close) if prev_close is not None else None,
        todays_high=todays_high, last_price=last_price, cfg=score_cfg,
    )

    st = state.setdefault(symbol, TickerState())
    is_new_hod = todays_high > st.last_high_of_day + 1e-6
    if is_new_hod:
        st.last_high_of_day = todays_high
        st.nhod_streak += 1

    now = time.time()
    grade = s.grade
    qualifies = grade_at_least(grade, ALERT_MIN_GRADE)
    cooldown_passed = (now - st.last_alert_time) >= ALERT_COOLDOWN_SEC
    grade_improved = (st.last_alert_grade is None or
        GRADE_ORDER.index(grade) > GRADE_ORDER.index(st.last_alert_grade))

    if qualifies and (cooldown_passed or grade_improved):
        if bot:
            payload = AlertPayload(
                symbol=symbol,
                company_name=(details.name if details else symbol),
                last_price=last_price, change_pct=change_pct,
                change_abs=change_abs, todays_volume=todays_volume,
                avg_volume_20d=avg_vol, gap_pct=s.gap_value, score=s,
                band_label="SMALL CAP" if is_small_cap else "MID CAP",
                band_range=f"${band[0]:g} - ${band[1]:g}",
                is_small_cap=is_small_cap, details=details, news=news,
            )
            try:
                await bot.send_alert(payload)
            except Exception as e:
                log.warning("Bot alert failed for %s: %s", symbol, e)
        st.last_alert_time = now
        st.last_alert_grade = grade
        log.info("ALERT %s grade=%s price=%.2f chg=%+.2f%% rvol=%s news=%s",
                 symbol, grade, last_price, change_pct,
                 f"{s.rvol_value:.1f}x" if s.rvol_value else "-",
                 "Y" if news else "N")

    if (st.last_alert_grade is not None and
            is_new_hod and
            (now - st.last_followup_time) >= FOLLOWUP_INTERVAL_SEC and
            (now - st.last_alert_time) >= FOLLOWUP_INTERVAL_SEC):
        if bot:
            try:
                await bot.send_followup(symbol=symbol, change_pct=change_pct,
                                        streak=st.nhod_streak,
                                        is_nhod=is_new_hod,
                                        is_small_cap=is_small_cap)
            except Exception as e:
                log.warning("Bot followup failed for %s: %s", symbol, e)
        st.last_followup_time = now
    return symbol


async def _polling_loop(polygon, bot, state, stop_evt):
    cfg_small = ScoringConfig(volume_threshold=75_000)
    cfg_mid = ScoringConfig(volume_threshold=150_000)
    consecutive_errors = 0
    poll_count = 0
    log.info("Polling loop started - interval=%.1fs", POLL_INTERVAL_SEC)
    _last_session_date: date | None = None
    while not stop_evt.is_set():
        if not _is_trading_session():
            # Market closed — sleep quietly and check again in 60s
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            continue

        # First poll of a new trading day — wipe stale intraday state
        today = date.today()
        if _last_session_date != today:
            _last_session_date = today
            for st in state.values():
                st.last_high_of_day = 0.0
                st.nhod_streak = 0
                st.last_alert_grade = None
                st.last_alert_time = 0.0
                st.last_followup_time = 0.0
            log.info("New session %s — intraday state cleared.", today)

        loop_start = time.time()
        try:
            tickers = await polygon.snapshot_all_us()
            consecutive_errors = 0
            candidates = [t for t in tickers
                          if (t.get("todaysChangePerc") or 0) >= MIN_GAIN_PCT]
            candidates.sort(key=lambda x: x.get("todaysChangePerc") or 0,
                            reverse=True)
            small_top, mid_top = [], []
            ps, pm = 0, 0
            for t in candidates:
                last_trade = t.get("lastTrade") or {}
                day = t.get("day") or {}
                price = float(last_trade.get("p") or day.get("c") or 0.0)
                if price <= 0:
                    continue
                if ps < TOP_N and in_band(price, SMALL_CAP_BAND):
                    sym = await _process_candidate(
                        t=t, band=SMALL_CAP_BAND, is_small_cap=True,
                        polygon=polygon, bot=bot, state=state, cfg=cfg_small)
                    if sym:
                        small_top.append(sym); ps += 1
                if pm < TOP_N and in_band(price, MID_CAP_BAND):
                    sym = await _process_candidate(
                        t=t, band=MID_CAP_BAND, is_small_cap=False,
                        polygon=polygon, bot=bot, state=state, cfg=cfg_mid)
                    if sym:
                        mid_top.append(sym); pm += 1
                if ps >= TOP_N and pm >= TOP_N:
                    break
            _write_watchlist(small_top, mid_top, WATCHLIST_FILE)
            poll_count += 1
            if poll_count % 60 == 0:
                _save_state(state, STATE_FILE)
        except PolygonError as e:
            consecutive_errors += 1
            log.warning("Polygon error: %s", e)
        except Exception as e:
            consecutive_errors += 1
            log.exception("Unexpected polling error: %s", e)

        backoff = (min(30.0, POLL_INTERVAL_SEC * (2 ** min(consecutive_errors, 5)))
                   if consecutive_errors else POLL_INTERVAL_SEC)
        elapsed = time.time() - loop_start
        try:
            await asyncio.wait_for(stop_evt.wait(),
                                   timeout=max(0.0, backoff - elapsed))
        except asyncio.TimeoutError:
            pass
    log.info("Polling loop exiting cleanly.")


async def _amain():
    if not POLYGON_API_KEY:
        log.error("POLYGON_API_KEY env var is required.")
        return 2
    stop_evt = asyncio.Event()
    def _stop(*_a):
        log.info("Shutdown signal received.")
        stop_evt.set()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())
    state = _load_state(STATE_FILE)
    bot = None
    bot_task = None
    if DISCORD_BOT_TOKEN and CHANNEL_SMALL and CHANNEL_MID:
        bot = ScannerBot(token=DISCORD_BOT_TOKEN, channel_small=CHANNEL_SMALL,
                         channel_mid=CHANNEL_MID,
                         voice_channel_id=VOICE_CHANNEL_ID,
                         speak_min_grade=SPEAK_MIN_GRADE,
                         bot_display_name=BOT_DISPLAY_NAME)
        bot_task = await bot.start_in_background()
        log.info("Discord bot online.")
    else:
        log.warning("Discord disabled: set DISCORD_BOT_TOKEN, CHANNEL_SMALL, "
                    "CHANNEL_MID to enable.")
    async with aiohttp.ClientSession() as session:
        polygon = PolygonClient(POLYGON_API_KEY, session)
        try:
            await _polling_loop(polygon, bot, state, stop_evt)
        finally:
            if bot:
                await bot.stop()
            if bot_task:
                try:
                    await asyncio.wait_for(bot_task, timeout=5)
                except (asyncio.TimeoutError, Exception):
                    pass
    return 0


def main():
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


def _selftest():
    from scoring import score, ScoringConfig
    s = score(has_news=True, todays_volume=2_310_000, avg_volume_20d=82_000,
              todays_open=15.20, prev_close=13.05, todays_high=17.85,
              last_price=17.85, cfg=ScoringConfig())
    assert s.grade == "A+", s
    assert grade_at_least("A+", "B")
    assert not grade_at_least("C", "A+")
    print(f"Scoring + grade gating: OK (sample grade={s.grade})")
    _write_watchlist(["AAA", "BBB"], ["CCC"], "/tmp/_wl.txt")
    contents = open("/tmp/_wl.txt").read()
    assert "###Small Cap Gainers,AAA,BBB" in contents
    assert "###Mid Cap Gainers,CCC" in contents
    print("Watchlist writer: OK")
    print("Selftest passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(_selftest())
    sys.exit(main())
