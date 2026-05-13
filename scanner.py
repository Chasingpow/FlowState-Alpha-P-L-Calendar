"""Top Gainer Scanner — orchestrator.

Two parallel feeds:
  1. WebSocket (A.*) — real-time per-second bars; fires alerts in ~200ms.
  2. REST snapshot   — runs every POLL_INTERVAL_SEC (default 30s) for cache
                       refresh, discovery of new tickers, and WS fallback.

Both feeds call the same _process_candidate() coroutine; state guards
prevent duplicate alerts for the same ticker.
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
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 4 * 60 <= mins <= 20 * 60


try:
    from dotenv import load_dotenv; load_dotenv()
except Exception:
    pass

from polygon_client import PolygonClient, PolygonError
from polygon_ws import PolygonWSClient
from scoring import ScoringConfig, score
from discord_bot import AlertPayload, ScannerBot

POLYGON_API_KEY     = os.getenv("POLYGON_API_KEY", "").strip()
DISCORD_BOT_TOKEN   = os.getenv("DISCORD_BOT_TOKEN", "").strip()
CHANNEL_SMALL       = int(os.getenv("CHANNEL_SMALL") or "0")
CHANNEL_MID         = int(os.getenv("CHANNEL_MID") or "0")
VOICE_CHANNEL_ID    = int(os.getenv("VOICE_CHANNEL_ID") or "0") or None
SPEAK_MIN_GRADE     = os.getenv("SPEAK_MIN_GRADE", "B").strip()
BOT_DISPLAY_NAME    = os.getenv("BOT_DISPLAY_NAME", "").strip() or None

# REST polling is now a background refresh; WebSocket handles real-time detection.
POLL_INTERVAL_SEC   = float(os.getenv("POLL_INTERVAL_SEC", "30.0"))
ALERT_COOLDOWN_SEC  = float(os.getenv("ALERT_COOLDOWN_SEC", "1800"))
FOLLOWUP_INTERVAL_SEC = float(os.getenv("FOLLOWUP_INTERVAL_SEC", "300"))
ALERT_MIN_GRADE     = os.getenv("ALERT_MIN_GRADE", "A").strip()
TOP_N               = int(os.getenv("TOP_N", "40"))
MIN_GAIN_PCT        = float(os.getenv("MIN_GAIN_PCT", "3.0"))
WATCHLIST_FILE      = os.getenv("WATCHLIST_FILE", "watchlist.txt")
STATE_FILE          = os.getenv("STATE_FILE", "scanner_state.json")
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO").upper()

SMALL_CAP_BAND = (0.50, 20.0)
MID_CAP_BAND   = (20.01, 300.0)

_CFG_SMALL = ScoringConfig(volume_threshold=25_000)
_CFG_MID   = ScoringConfig(volume_threshold=150_000)

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("scanner")

GRADE_ORDER = ["F", "D", "C", "B", "A", "A+"]


@dataclass
class TickerState:
    last_alert_grade: Optional[str]  = None
    last_alert_time:  float          = 0.0
    last_followup_time: float        = 0.0
    last_high_of_day: float          = 0.0
    nhod_streak: int                 = 0


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


async def _process_candidate(
    *, symbol: str, last_price: float, change_pct: float, change_abs: float,
    todays_volume: int, todays_high: float, todays_open, prev_close,
    band, is_small_cap: bool, polygon, bot, state: dict, cfg,
) -> Optional[str]:
    if not symbol or "." in symbol:
        return None
    if last_price <= 0 or not in_band(last_price, band):
        return None
    if change_pct < MIN_GAIN_PCT or change_pct > 1000:
        return None

    avg_vol, news, details = await asyncio.gather(
        polygon.avg_volume_20d(symbol),
        polygon.latest_news(symbol, hours=24),
        polygon.ticker_details(symbol),
    )

    has_news = news is not None

    # Determine which market session we're in
    now_et   = datetime.now(_ET)
    now_mins = now_et.hour * 60 + now_et.minute
    is_premarket  = (4 * 60 <= now_mins < 9 * 60 + 30) and now_et.weekday() < 5
    is_afterhours = (16 * 60 <= now_mins <= 20 * 60)   and now_et.weekday() < 5

    # Use session time (not volume) so both REST (day.v=0) and WebSocket
    # (av>0 from pre-market trades) get consistent relaxed scoring.
    pm_mode = is_premarket and change_pct > 0

    if pm_mode:
        # Polygon leaves day.o and day.h as 0 until the 9:30am open.
        # Treat 0 as "unset" and substitute last_price so gap and technical
        # checks are computed against something meaningful.
        if not todays_open:
            todays_open = last_price
        if not todays_high:
            todays_high = last_price
        score_cfg = ScoringConfig(
            volume_threshold=0,
            rvol_threshold=0.0,
            gap_threshold_pct=cfg.gap_threshold_pct,
            tech_proximity_pct=cfg.tech_proximity_pct,
        )
    elif is_afterhours and has_news:
        # After-hours catalyst play: AH volume is always thin so volume/RVOL/gap
        # gates are meaningless. change_pct > MIN_GAIN_PCT already gates entry.
        score_cfg = ScoringConfig(
            volume_threshold=0,
            rvol_threshold=0.0,
            gap_threshold_pct=0.0,
            tech_proximity_pct=5.0,
        )
    else:
        score_cfg = cfg

    s = score(
        has_news=has_news, todays_volume=todays_volume,
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
    is_first_alert = st.last_alert_grade is None
    grade_improved = (not is_first_alert and
        GRADE_ORDER.index(grade) > GRADE_ORDER.index(st.last_alert_grade))
    long_absence = (not is_first_alert and (now - st.last_alert_time) >= 7200)
    extreme = change_pct >= 100  # stock is up 100%+ on the day

    # Tiered alert rules (long_absence computed first so it can gate B/C re-alerts):
    #   A / A+  — always alert
    #   B       — alert with news, OR re-entry after 2h+, OR 100%+ extreme mover
    #   C       — alert ONLY for 100%+ extreme movers that are already on our radar
    #   Pre-market — B or better (news lags gap opens by several minutes)
    #   D / F   — never
    if pm_mode:
        qualifies = grade_at_least(grade, "B")
    else:
        qualifies = (
            grade_at_least(grade, "A") or
            (grade == "B" and (s.catalyst or long_absence or extreme)) or
            (grade == "C" and extreme and long_absence)
        )

    if qualifies and (is_first_alert or grade_improved or long_absence):
        if bot:
            payload = AlertPayload(
                symbol=symbol,
                company_name=(details.name if details else symbol),
                last_price=last_price, change_pct=change_pct,
                change_abs=change_abs, todays_volume=todays_volume,
                avg_volume_20d=avg_vol, gap_pct=s.gap_value, score=s,
                band_label=("SMALL CAP" if is_small_cap else "MID CAP")
                           + (" · AH" if is_afterhours else " · PM" if pm_mode else ""),
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


async def _warm_caches(polygon, symbols: list) -> None:
    """Pre-fetch avg_vol, details, and news for candidates so first alert is instant."""
    if not symbols:
        return
    await asyncio.gather(
        *[polygon.avg_volume_20d(s) for s in symbols],
        *[polygon.ticker_details(s) for s in symbols],
        *[polygon.latest_news(s) for s in symbols],
        return_exceptions=True,
    )


def _build_ws_handler(
    prev_close_cache: dict, day_high_cache: dict,
    trade_vol_cache: dict, day_open_cache: dict,
) -> callable:
    """A.* handler — maintains caches only. Task creation is the T.* handler's job.

    A events provide authoritative accumulated volume (av), the official open
    price (op), and per-second highs — none of which are in T events.
    """
    def on_bar(ev: dict) -> None:
        sym = ev.get("sym", "")
        if not sym or "." in sym:
            return

        # Official open price — set once the session opens; use as day_open_cache
        op = ev.get("op")
        if op:
            day_open_cache[sym] = float(op)

        # Polygon's authoritative accumulated volume beats our T-size sum
        av = int(ev.get("av") or 0)
        if av > trade_vol_cache.get(sym, 0):
            trade_vol_cache[sym] = av

        # Day high — T events only carry per-trade price, not the running day high
        bar_high = float(ev.get("h") or 0.0)
        price    = float(ev.get("c") or 0.0)
        new_high = max(bar_high, day_high_cache.get(sym, 0.0), price)
        if new_high > day_high_cache.get(sym, 0.0):
            day_high_cache[sym] = new_high

    return on_bar


def _build_trade_handler(
    polygon, bot, state: dict,
    prev_close_cache: dict, day_high_cache: dict,
    trade_vol_cache: dict, day_open_cache: dict,
) -> callable:
    """T.* handler — fires _process_candidate on the first qualifying print.

    T events arrive the moment a trade executes, giving sub-second gap detection.
    A 5-second per-ticker task debounce prevents flooding the event loop when
    an active stock prints hundreds of trades per second.
    """
    _task_times: dict[str, float] = {}  # sym → last time we created a task

    def on_trade(ev: dict) -> None:
        sym = ev.get("sym", "")
        if not sym or "." in sym:
            return

        price = float(ev.get("p") or 0.0)
        if price <= 0:
            return

        # Accumulate this trade's size into our running day-volume tally.
        # The A.* handler will correct this with Polygon's authoritative av later.
        trade_size = int(ev.get("s") or 0)
        if trade_size > 0:
            trade_vol_cache[sym] = trade_vol_cache.get(sym, 0) + trade_size

        prev_close = prev_close_cache.get(sym)
        if not prev_close or prev_close <= 0:
            return

        change_pct = (price - prev_close) / prev_close * 100.0
        if change_pct < MIN_GAIN_PCT or change_pct > 1000:
            return

        if in_band(price, SMALL_CAP_BAND):
            band, is_small_cap, cfg = SMALL_CAP_BAND, True, _CFG_SMALL
        elif in_band(price, MID_CAP_BAND):
            band, is_small_cap, cfg = MID_CAP_BAND, False, _CFG_MID
        else:
            return

        # Alert debounce: don't re-alert a ticker we alerted in the last 60s.
        # First-ever alerts (last_alert_time == 0.0) always pass immediately.
        st = state.get(sym)
        if st and st.last_alert_time and (time.time() - st.last_alert_time) < 60:
            return

        # Task debounce: active stocks print hundreds of times per second.
        # Cap task creation to once per 5s per ticker to keep the loop healthy.
        now = time.time()
        if now - _task_times.get(sym, 0) < 5:
            return
        _task_times[sym] = now

        change_abs = price - prev_close
        av         = trade_vol_cache.get(sym, 0)
        op         = day_open_cache.get(sym)  # None pre-market until A event arrives

        # Update day high with this trade price
        current_high = day_high_cache.get(sym, 0.0)
        new_high = max(current_high, price)
        if new_high > current_high:
            day_high_cache[sym] = new_high

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            _process_candidate(
                symbol=sym, last_price=price,
                change_pct=change_pct, change_abs=change_abs,
                todays_volume=av, todays_high=new_high,
                todays_open=op, prev_close=prev_close,
                band=band, is_small_cap=is_small_cap,
                polygon=polygon, bot=bot, state=state, cfg=cfg,
            )
        )

    return on_trade


async def _polling_loop(
    polygon, bot, state, stop_evt,
    prev_close_cache: dict, day_high_cache: dict,
    trade_vol_cache: dict, day_open_cache: dict,
):
    """REST snapshot loop — refreshes caches and acts as WebSocket fallback."""
    consecutive_errors = 0
    poll_count = 0
    log.info("REST polling loop started — interval=%.1fs", POLL_INTERVAL_SEC)
    _last_session_date: date | None = None

    while not stop_evt.is_set():
        if not _is_trading_session():
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            continue

        today = date.today()
        if _last_session_date != today:
            _last_session_date = today
            for st in state.values():
                st.last_high_of_day = 0.0
                st.nhod_streak = 0
                st.last_alert_grade = None
                st.last_alert_time = 0.0
                st.last_followup_time = 0.0
            day_high_cache.clear()
            trade_vol_cache.clear()
            day_open_cache.clear()
            log.info("New session %s — intraday state cleared.", today)

        loop_start = time.time()
        try:
            tickers = await polygon.snapshot_all_us()
            consecutive_errors = 0

            # Update shared caches from fresh snapshot data
            for t in tickers:
                sym = t.get("ticker") or ""
                if not sym or "." in sym:
                    continue
                day  = t.get("day") or {}
                prev = t.get("prevDay") or {}
                pc = prev.get("c")
                dh = day.get("h")
                do = day.get("o")
                dv = day.get("v")
                if pc:
                    prev_close_cache[sym] = float(pc)
                if dh and float(dh) > day_high_cache.get(sym, 0.0):
                    day_high_cache[sym] = float(dh)
                if do:
                    day_open_cache[sym] = float(do)
                if dv and int(dv) > trade_vol_cache.get(sym, 0):
                    trade_vol_cache[sym] = int(dv)

            candidates = [t for t in tickers
                          if (t.get("todaysChangePerc") or 0) >= MIN_GAIN_PCT]
            candidates.sort(key=lambda x: x.get("todaysChangePerc") or 0, reverse=True)

            # Warm ALL top-100 candidates every REST poll — not just unseen ones.
            # Tickers from yesterday sit in state with stale caches; this ensures
            # every top mover has fresh avg_vol/details/news before the WS fires.
            top_syms = [t.get("ticker", "") for t in candidates[:100]
                        if t.get("ticker") and "." not in t.get("ticker", "")]
            if top_syms:
                asyncio.create_task(_warm_caches(polygon, top_syms))

            small_top, mid_top = [], []
            ps, pm = 0, 0
            for t in candidates:
                last_trade = t.get("lastTrade") or {}
                day  = t.get("day") or {}
                prev = t.get("prevDay") or {}
                price = float(last_trade.get("p") or day.get("c") or 0.0)
                if price <= 0:
                    continue
                sym_key  = t.get("ticker") or ""
                chg_pct  = float(t.get("todaysChangePerc") or 0.0)
                chg_abs  = float(t.get("todaysChange") or 0.0)
                t_volume = int(day.get("v") or 0)
                t_high   = float(day.get("h") or 0.0)
                t_open   = day.get("o")
                p_close  = prev.get("c")

                _st = state.get(sym_key)
                already_alerted = _st is not None and _st.last_alert_time > 0

                if in_band(price, SMALL_CAP_BAND) and (ps < TOP_N or already_alerted):
                    sym = await _process_candidate(
                        symbol=sym_key, last_price=price,
                        change_pct=chg_pct, change_abs=chg_abs,
                        todays_volume=t_volume, todays_high=t_high,
                        todays_open=t_open, prev_close=p_close,
                        band=SMALL_CAP_BAND, is_small_cap=True,
                        polygon=polygon, bot=bot, state=state, cfg=_CFG_SMALL)
                    if sym:
                        small_top.append(sym)
                        if not already_alerted:
                            ps += 1

                if in_band(price, MID_CAP_BAND) and (pm < TOP_N or already_alerted):
                    sym = await _process_candidate(
                        symbol=sym_key, last_price=price,
                        change_pct=chg_pct, change_abs=chg_abs,
                        todays_volume=t_volume, todays_high=t_high,
                        todays_open=t_open, prev_close=p_close,
                        band=MID_CAP_BAND, is_small_cap=False,
                        polygon=polygon, bot=bot, state=state, cfg=_CFG_MID)
                    if sym:
                        mid_top.append(sym)
                        if not already_alerted:
                            pm += 1

                if ps >= TOP_N and pm >= TOP_N:
                    break

            _write_watchlist(small_top, mid_top, WATCHLIST_FILE)
            poll_count += 1
            if poll_count % 10 == 0:
                _save_state(state, STATE_FILE)

        except PolygonError as e:
            consecutive_errors += 1
            log.warning("Polygon error: %s", e)
        except Exception as e:
            consecutive_errors += 1
            log.exception("Unexpected polling error: %s", e)

        backoff = (min(120.0, POLL_INTERVAL_SEC * (2 ** min(consecutive_errors, 4)))
                   if consecutive_errors else POLL_INTERVAL_SEC)
        elapsed = time.time() - loop_start
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=max(0.0, backoff - elapsed))
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

    # Shared caches — seeded at startup from REST snapshot, then kept fresh by:
    #   prev_close_cache : REST poll (prevDay.c) and A.* events (op)
    #   day_high_cache   : REST poll (day.h) and A.* events (bar high)
    #   trade_vol_cache  : T.* events (size accumulation) + A.* (authoritative av)
    #   day_open_cache   : REST poll (day.o) + A.* events (op field)
    prev_close_cache: dict[str, float] = {}
    day_high_cache:   dict[str, float] = {}
    trade_vol_cache:  dict[str, int]   = {}
    day_open_cache:   dict[str, float] = {}

    async with aiohttp.ClientSession() as session:
        polygon = PolygonClient(POLYGON_API_KEY, session)

        # Seed all four caches before WebSocket connects so T.* events have
        # prev_close reference prices from the very first trade.
        try:
            log.info("Seeding caches from initial REST snapshot...")
            init_tickers = await polygon.snapshot_all_us()
            for t in init_tickers:
                sym = t.get("ticker") or ""
                if not sym or "." in sym:
                    continue
                day  = t.get("day") or {}
                prev = t.get("prevDay") or {}
                pc = prev.get("c")
                dh = day.get("h")
                do = day.get("o")
                dv = day.get("v")
                if pc:
                    prev_close_cache[sym] = float(pc)
                if dh:
                    day_high_cache[sym] = float(dh)
                if do:
                    day_open_cache[sym] = float(do)
                if dv:
                    trade_vol_cache[sym] = int(dv)
            log.info("Seeded %d tickers into caches", len(prev_close_cache))
        except Exception as e:
            log.warning("Initial snapshot seeding failed: %s — WS will warm up gradually", e)

        bar_handler   = _build_ws_handler(
            prev_close_cache, day_high_cache, trade_vol_cache, day_open_cache,
        )
        trade_handler = _build_trade_handler(
            polygon, bot, state,
            prev_close_cache, day_high_cache, trade_vol_cache, day_open_cache,
        )
        ws_client = PolygonWSClient(POLYGON_API_KEY, bar_handler, trade_handler)

        try:
            await asyncio.gather(
                _polling_loop(polygon, bot, state, stop_evt,
                              prev_close_cache, day_high_cache,
                              trade_vol_cache, day_open_cache),
                ws_client.run(stop_evt),
            )
        finally:
            _save_state(state, STATE_FILE)
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
    print("Selftest passed.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        sys.exit(_selftest())
    sys.exit(main())
