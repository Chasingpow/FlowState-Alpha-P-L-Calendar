# Top Gainer Scanner — v2

A Polygon-fed real-time scanner that posts FlowState-style rich embeds to Discord and **speaks the ticker out loud in your voice channel** when an A+ setup hits, so you can keep working without staring at a screen.

Two price bands run in parallel:

| Band | Price | Channel |
|---|---|---|
| Small Cap | $1 – $20  | `CHANNEL_SMALL` |
| Mid Cap   | $5 – $300 | `CHANNEL_MID`   |

Plus a TradingView Pine Script indicator that mirrors the same logic on-chart.

## P&L Calendar Web App

An interactive web application for Discord members to log daily P&L, view equity curves, and see top traders.

Features:
- 5-year trading calendar with earnings dates
- Log wins, losses, and profit per day
- Equity curve visualization
- Top 10 weekly and top 3 monthly traders
- Discord OAuth authentication

### Running the P&L Calendar

1. Set up Discord OAuth app at https://discord.com/developers/applications
2. Add the new environment variables to `.env`
3. Run `python run_pnl_calendar.py`
4. Access at http://localhost:8000

## Files

| File | Purpose |
|---|---|
| `scanner.py` | Main entry point. Async polling + scoring + dispatch. |
| `polygon_client.py` | Async Polygon.io client (snapshot, ticker details, news, RVOL, earnings). |
| `scoring.py` | Computes the 5+1 setup score and letter grade. |
| `discord_bot.py` | discord.py bot: rich embeds + voice + follow-up messages. |
| `tts.py` | gTTS wrapper, spells tickers letter-by-letter. |
| `top_gainers_scanner.pine` | TradingView Pine Script indicator. |
| `pnl_calendar.py` | FastAPI web app for P&L calendar. |
| `models.py` | SQLAlchemy models for P&L data. |
| `database.py` | Database setup. |
| `auth.py` | Discord OAuth handling. |
| `calendar_utils.py` | Trading calendar utilities. |
| `run_pnl_calendar.py` | Script to run the web app. |
| `templates/` | HTML templates. |
| `static/` | Static files (CSS, JS). |
| `requirements.txt` | Python deps. |
| `.env.example` | Config template — copy to `.env` and fill in. |
| `scanner.service` | systemd unit for VPS deployment. |

## How the scoring works

Each gainer gets a score out of 5, plus a +1 bonus for printing a new high of day right now:

| # | Criterion | Default trigger |
|---|---|---|
| 1 | **Catalyst** | Ticker has news in the last 24 hours (Polygon news endpoint) |
| 2 | **RVOL**     | Today's volume / 20-day avg ≥ **5x** |
| 3 | **Gap**      | Today's open vs prev close ≥ **+5%** |
| 4 | **Volume**   | Today's volume ≥ **1,000,000** shares |
| 5 | **Technical**| Current price within **1%** of day's high |
| ★ | **Bonus**    | Currently printing a new high of day → +1 |

Total → grade:

| Score | Grade | Color | What happens |
|---|---|---|---|
| 6/5 | **A+** | green | Posts embed **+ voice alert** |
| 5/5 | **A**  | purple | Posts embed |
| 4/5 | **B**  | blue   | Posts embed |
| 3/5 | **C**  | gray   | Silent (configurable via `ALERT_MIN_GRADE`) |
| ≤ 2 | D / F  | —      | Silent |

You can change all the thresholds in `scoring.py` (`ScoringConfig`) and the alert/voice cutoffs in `.env`.

---

## Setup

### 1. Polygon API key

Sign up at [polygon.io](https://polygon.io/). To poll once per second you need the **Stocks Developer plan ($79/mo)** or higher for unlimited REST calls. Lower plans work but you'll need to bump `POLL_INTERVAL_SEC` to ~12 seconds.

### 2. Create the Discord bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**.
2. Sidebar **Bot** → **Reset Token** → copy the token into `DISCORD_BOT_TOKEN`.
3. Same page, scroll to **Privileged Gateway Intents** — leave Server Members and Message Content **off** (we don't need them).
4. Sidebar **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Permissions: `Send Messages`, `Embed Links`, `Read Message History`, **`Connect`**, **`Speak`**, `Use Voice Activity`
5. Open the generated URL → invite to your server.
6. In Discord settings → **Advanced** → enable **Developer Mode**.
7. Right-click each channel → **Copy Channel ID**:
   - Small-cap text channel → `CHANNEL_SMALL`
   - Mid-cap text channel → `CHANNEL_MID`
   - Voice channel for alerts → `VOICE_CHANNEL_ID`

### 3. Provision a small VPS

Any of: DigitalOcean ($4/mo), Hetzner CX11 (~$4/mo), Vultr ($5/mo), Linode ($5/mo). Pick a US-East region. Use Ubuntu 22.04 or 24.04.

### 4. Install system deps (voice needs ffmpeg)

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip ffmpeg libffi-dev libnacl-dev
```

### 5. Install + run

```bash
mkdir -p ~/top-gainer-scanner && cd ~/top-gainer-scanner
# copy all the files in this folder here

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env   # fill in keys + channel IDs

# Smoke test (no network/Discord)
.venv/bin/python scanner.py --selftest
.venv/bin/python scoring.py
.venv/bin/python tts.py

# Live run
.venv/bin/python scanner.py
```

### 6. Run as a service

```bash
sudo cp scanner.service /etc/systemd/system/top-gainer-scanner.service
# Edit User=, paths, and EnvironmentFile= inside the unit if needed
sudo systemctl daemon-reload
sudo systemctl enable --now top-gainer-scanner
sudo journalctl -fu top-gainer-scanner    # live logs
```

Auto-restarts on failure, starts on boot.

---

## Voice alerts

When a ticker scores **A+** (6/5), the bot:

1. Posts the rich embed in the small-cap or mid-cap text channel.
2. Generates a TTS clip via gTTS (free, no key) saying e.g.:
   > "A plus alert. Ticker P. L. T. R. Up 9.2 percent. Price 24.85."
3. Plays that clip in your `VOICE_CHANNEL_ID`. You stay in that voice channel; it speaks to you whenever an A+ hits.

To get alerts but not voice, set `VOICE_CHANNEL_ID=` (blank) or `SPEAK_MIN_GRADE=Z`.

To make even **A** trigger voice: `SPEAK_MIN_GRADE=A`.

> Tickers are spelled letter-by-letter so gTTS doesn't pronounce "PLTR" as "puh-litter". This works well for 3–5 letter tickers; 1-2 letter tickers (`F`, `GM`) are spoken naturally.

---

## Follow-up cadence

Once a ticker has triggered a fresh alert, the bot drops a compact follow-up line every `FOLLOWUP_INTERVAL_SEC` seconds (default 5 minutes) for as long as the ticker is still on the gainer board. Format mirrors the screenshot:

```
PLTR  +12.88%  🔥🔥🔥🔥 x4 · NHOD
```

The 🔥 count grows with each new high of day printed since the original alert. The `NHOD` tag appears only on the line where the new high actually printed.

---

## Tuning

| Var | Default | Effect |
|---|---|---|
| `POLL_INTERVAL_SEC` | `1.0` | Polygon snapshot polling interval |
| `ALERT_COOLDOWN_SEC` | `1800` | Seconds before re-alerting same ticker (overridden if grade improves) |
| `FOLLOWUP_INTERVAL_SEC` | `300` | NHOD/momentum follow-up cadence |
| `ALERT_MIN_GRADE` | `B` | Lowest grade that posts an embed (`A+`, `A`, `B`, `C`, `D`) |
| `SPEAK_MIN_GRADE` | `A+` | Lowest grade that triggers voice |
| `TOP_N` | `15` | How many tickers tracked per band |
| `MIN_GAIN_PCT` | `3.0` | Floor on % change before considering a ticker |

Edit `scoring.py` `ScoringConfig` to tweak the criterion thresholds (RVOL ≥ 5x, gap ≥ 5%, volume ≥ 1M, etc).

Edit `scanner.py` `SMALL_CAP_BAND` / `MID_CAP_BAND` constants to widen or narrow the price bands.

---

## TradingView integration

The Python scanner writes `watchlist.txt` once per second:

```
###Small Cap Gainers,AAPL,NVDA,XYZ
###Mid Cap Gainers,TSLA,META,GOOG
```

Paste into TradingView via Watchlist → `...` → **Import list from text**.

For an in-chart top-N table + threshold alerts, load `top_gainers_scanner.pine` in the Pine Editor and enter up to 40 tickers from the watchlist into the indicator's settings. Pick the band, set the alert threshold, save, then create an alert: chart → **Add alert** → this indicator → **Any alert() function call**.

---

## What's *not* in v2

- **CTB / Cost to Borrow** — Polygon doesn't expose borrow data. To add it, you'd subscribe to Ortex (~$60/mo) or Fintel and bolt on a borrow-data fetcher in `polygon_client.py`. The embed has space for it — drop a new field in `discord_bot.build_embed`.
- **Pre-market / after-hours** — Polygon's snapshot does include extended-hours data, but `MIN_VOLUME` defaults filter most of it out. Lower the threshold if you want to scan PM/AH.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `POLYGON_API_KEY env var is required` | `.env` not found in working dir, or `python-dotenv` missing |
| `Discord disabled: set DISCORD_BOT_TOKEN ...` warning | Bot env vars not set; scanner runs but won't post |
| `Voice connect failed` | Bot lacks Connect/Speak perms, or `VOICE_CHANNEL_ID` is wrong/text channel |
| `gTTS not installed` | `pip install gTTS` (already in requirements.txt) |
| `discord.errors.PrivilegedIntentsRequired` | We don't request privileged intents; ensure no other code adds them |
| Voice plays then cuts out | ffmpeg missing; `apt install ffmpeg` on the VPS |
| HTTP 429 from Polygon | Over rate limit — increase `POLL_INTERVAL_SEC` or upgrade plan |
| No alerts firing | Grades are F or below `ALERT_MIN_GRADE` — try `ALERT_MIN_GRADE=C` to see more |

---

## Disclaimer

This is a market scanner, not advice. False positives and missed signals are guaranteed. Verify every alert against your own analysis before trading. Polygon data can have ticks, missing fields, and delays — don't trust a single number blindly.
