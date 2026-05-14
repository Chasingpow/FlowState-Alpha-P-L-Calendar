"""
Discord bot: posts FlowState-style rich embeds and speaks A+ alerts in voice.

Public API
----------
    bot = ScannerBot(...)
    await bot.start_in_background()       # connects + joins voice
    await bot.send_alert(payload)         # post embed + maybe speak
    await bot.send_followup(symbol, ...)  # short NHOD/momentum line

Both `send_alert` and `send_followup` are coroutine-safe and can be called
from the scanner task.

Discord setup the user has to do once:
    1. Create an application + bot at https://discord.com/developers/applications
    2. Enable "Server Members" + "Message Content" intents (under Bot)
    3. Copy the bot token -> DISCORD_BOT_TOKEN in .env
    4. OAuth2 -> URL Generator: scopes=bot, permissions=Send Messages,
       Embed Links, Connect, Speak, Use Voice Activity. Open the URL,
       invite to the server.
    5. Copy the channel IDs (right-click channel -> Copy Channel ID; needs
       Developer Mode on in Discord settings) for:
         - small-cap text channel    -> CHANNEL_SMALL
         - mid-cap text channel      -> CHANNEL_MID
         - voice channel for alerts  -> VOICE_CHANNEL_ID
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import discord

import tts as tts_mod
from polygon_client import NewsItem, TickerDetails
from quiver_client import QuiverSignal
from scoring import ScoreResult

log = logging.getLogger("bot")


# Color palette, matching the inspiration screenshot
GRADE_COLORS = {
    "A+": 0x2ECC71,   # green
    "A":  0x9B59B6,   # purple
    "B":  0x3498DB,   # blue
    "C":  0x95A5A6,   # gray
    "D":  0x7F8C8D,   # dark gray
}


@dataclass
class AlertPayload:
    symbol: str
    company_name: str
    last_price: float
    change_pct: float
    change_abs: float
    todays_volume: int
    avg_volume_20d: Optional[float]
    gap_pct: Optional[float]
    score: ScoreResult
    band_label: str         # e.g. "SMALL CAP" or "MID CAP"
    band_range: str         # e.g. "$1 - $20"
    is_small_cap: bool
    details: Optional[TickerDetails]
    news: Optional[NewsItem]
    quiver_signal: Optional[QuiverSignal] = None


# --- Formatting helpers -----------------------------------------------------
def _fmt_compact(n: Optional[float]) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return f"{n:.0f}"


def _fmt_volume(n: Optional[float]) -> str:
    if n is None:
        return "—"
    return _fmt_compact(n)


def _check(ok: bool) -> str:
    return "✅" if ok else "❌"


def _country_flag(locale: Optional[str]) -> str:
    return "🇺🇸 USA" if (locale or "").lower() == "us" else (locale or "—")


def build_embed(p: AlertPayload) -> discord.Embed:
    color = GRADE_COLORS.get(p.score.grade, 0x95A5A6)
    fire = "🔥 " if p.score.grade in ("A+", "A") else ""
    band_emoji = "🟢" if p.is_small_cap else "🟣"

    title = (
        f"{fire}{p.score.grade} ALERT  |  {p.symbol}  |  {p.score.display()}  |  "
        f"{band_emoji} {p.band_label}  |  {p.band_range}"
    )

    arrow = "▲" if p.change_pct >= 0 else "▼"
    desc_lines = [
        f"**{p.company_name}**",
        f"**${p.last_price:.2f}**  {arrow} **{p.change_pct:+.2f}%**  "
        f"(${p.change_abs:+.2f})",
    ]
    if p.news:
        desc_lines.append(
            f"\n📰 **News:** {p.news.title}\n"
            f"_{p.news.publisher} · {p.news.published_utc[:16].replace('T', ' ')} UTC_"
        )
    if p.quiver_signal and p.quiver_signal.has_signal:
        desc_lines.append(f"\n{p.quiver_signal.summary()}")
    embed = discord.Embed(
        title=title,
        description="\n".join(desc_lines),
        color=color,
        timestamp=datetime.now(timezone.utc),
    )

    # Stats column 1
    rvol_str = f"{p.score.rvol_value:.1f}x" if p.score.rvol_value else "—"
    stats1 = (
        f"RVOL: **{rvol_str}**\n"
        f"Volume: **{_fmt_volume(p.todays_volume)}**\n"
        f"Avg Vol: **{_fmt_volume(p.avg_volume_20d)}**\n"
        f"Gap: **{('%+.1f%%' % p.gap_pct) if p.gap_pct is not None else '—'}**"
    )
    embed.add_field(name="​", value=stats1, inline=True)

    # Stats column 2
    d = p.details
    shares = d.shares_outstanding if d else None
    float_str = _fmt_compact(shares) if shares else "—"
    low_float_tag = " 🔸LOW FLOAT" if shares and shares < 20_000_000 else ""
    mktcap_str = f"${_fmt_compact(d.market_cap)}" if d and d.market_cap else "—"
    exch_str = (d.primary_exchange or "—") if d else "—"
    country_str = _country_flag(d.locale if d else None)
    stats2 = (
        f"Float: **{float_str}**{low_float_tag}\n"
        f"Mkt Cap: **{mktcap_str}**\n"
        f"Exchange: **{exch_str}**\n"
        f"Country: **{country_str}**"
    )
    embed.add_field(name="​", value=stats2, inline=True)

    # Score breakdown row (full width)
    s = p.score
    breakdown = (
        f"**Score Breakdown ({s.display()}{' Bonus' if s.nhod_bonus else ''}):**\n"
        f"1. Catalyst {_check(s.catalyst)}  "
        f"2. RVOL {_check(s.rvol_ok)}  "
        f"3. Gap {_check(s.gap_ok)}  "
        f"4. Volume {_check(s.volume_ok)}  "
        f"5. Technical {_check(s.technical_ok)}"
        + (f"  ·  **Bonus: NHOD +1**" if s.nhod_bonus else "")
    )
    embed.add_field(name="​", value=breakdown, inline=False)

    embed.set_footer(text="Not financial advice. Trade at your own risk. · Data may be delayed.")
    return embed


def build_compact_embed(p: AlertPayload) -> discord.Embed:
    """Minimal one-field embed for C-grade alerts — keeps the channel clean."""
    arrow = "▲" if p.change_pct >= 0 else "▼"
    band_emoji = "🟢" if p.is_small_cap else "🟣"
    rvol_str = f"{p.score.rvol_value:.1f}x" if p.score.rvol_value else "—"
    gap_str = f"{p.gap_pct:+.1f}%" if p.gap_pct is not None else "—"
    news_tag = "  📰" if p.news else ""
    title = (
        f"C  |  {p.symbol}  |  {arrow} {p.change_pct:+.2f}%  |  "
        f"${p.last_price:.2f}  |  {band_emoji} {p.band_label}{news_tag}"
    )
    stats = (
        f"Vol: **{_fmt_volume(p.todays_volume)}**  ·  "
        f"RVOL: **{rvol_str}**  ·  "
        f"Gap: **{gap_str}**"
    )
    embed = discord.Embed(title=title, color=GRADE_COLORS.get("C", 0x95A5A6),
                          timestamp=datetime.now(timezone.utc))
    embed.add_field(name="​", value=stats, inline=False)
    return embed


def build_followup_line(symbol: str, change_pct: float, streak: int,
                        is_nhod: bool) -> str:
    fire = "🔥" * min(streak, 5) if streak else ""
    nhod = " · **NHOD**" if is_nhod else ""
    return (f"`{symbol}`  **{change_pct:+.2f}%**  {fire} x{streak}{nhod}").strip()


# --- Bot --------------------------------------------------------------------
class ScannerBot:
    def __init__(
        self,
        *,
        token: str,
        channel_small: int,
        channel_mid: int,
        voice_channel_id: Optional[int] = None,
        speak_min_grade: str = "A+",
        bot_display_name: Optional[str] = None,
    ):
        if not token:
            raise RuntimeError("DISCORD_BOT_TOKEN is required")
        intents = discord.Intents.default()
        intents.message_content = False
        intents.members = False
        intents.voice_states = True
        self.client = discord.Client(intents=intents)
        self.token = token
        self.channel_small = channel_small
        self.channel_mid = channel_mid
        self.voice_channel_id = voice_channel_id
        self.speak_min_grade = speak_min_grade
        self.bot_display_name = bot_display_name
        self._voice: Optional[discord.VoiceClient] = None
        self._ready_evt = asyncio.Event()
        self._tts_lock = asyncio.Lock()
        # Priority slot: holds the single best-grade clip waiting to play next.
        # A higher-grade clip always bumps a lower-grade one that's waiting.
        self._tts_pending: Optional[str] = None
        self._tts_pending_rank: int = -1

        @self.client.event
        async def on_ready():
            log.info("Bot logged in as %s (id=%s)", self.client.user, self.client.user.id)
            if self.bot_display_name:
                try:
                    await self.client.user.edit(username=self.bot_display_name)
                except Exception as e:  # noqa: BLE001
                    log.debug("Could not set bot display name: %s", e)
            await self._ensure_voice()
            self._ready_evt.set()

    # -- voice -----------------------------------------------------------
    async def _ensure_voice(self) -> None:
        if not self.voice_channel_id:
            return
        ch = self.client.get_channel(self.voice_channel_id)
        if not isinstance(ch, discord.VoiceChannel):
            log.warning("Voice channel %s not found or not a VoiceChannel",
                        self.voice_channel_id)
            return
        try:
            if self._voice and self._voice.is_connected():
                return
            self._voice = await ch.connect(reconnect=True, self_deaf=True)
            log.info("Connected to voice channel #%s", ch.name)
        except Exception as e:  # noqa: BLE001
            log.warning("Voice connect failed: %s", e)

    _GRADE_RANK = {"C": 0, "B": 1, "A": 2, "A+": 3}

    async def _play_phrase(self, phrase: str) -> None:
        """Synthesise and play one TTS clip. Must be called with _tts_lock held."""
        await self._ensure_voice()
        if not (self._voice and self._voice.is_connected()):
            return
        mp3 = await asyncio.to_thread(tts_mod.synthesize, phrase)
        if not mp3:
            return
        try:
            done = asyncio.Event()

            def after(err):
                if err:
                    log.warning("Voice playback error: %s", err)
                self.client.loop.call_soon_threadsafe(done.set)

            source = discord.FFmpegPCMAudio(mp3, options="-loglevel quiet")
            self._voice.play(source, after=after)
            await asyncio.wait_for(done.wait(), timeout=30)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to play voice alert: %s", e)
        finally:
            try:
                os.remove(mp3)
            except OSError:
                pass

    async def _speak(self, phrase: str, grade: str = "C") -> None:
        if not self.voice_channel_id:
            return
        rank = self._GRADE_RANK.get(grade, 0)
        if self._tts_lock.locked():
            # Keep only the highest-grade clip waiting — A+ always beats B in the queue
            if rank > self._tts_pending_rank:
                self._tts_pending = phrase
                self._tts_pending_rank = rank
                log.debug("Voice busy — queued grade=%s clip (bumped lower)", grade)
            else:
                log.debug("Voice busy — dropping grade=%s (lower than pending)", grade)
            return
        async with self._tts_lock:
            await self._play_phrase(phrase)
            # After current clip, drain the priority slot once
            if self._tts_pending is not None:
                pending, self._tts_pending = self._tts_pending, None
                self._tts_pending_rank = -1
                await self._play_phrase(pending)

    async def _speak_repeat(self, phrase: str, times: int, grade: str = "C") -> None:
        for _ in range(times):
            await self._speak(phrase, grade)

    # -- public API ------------------------------------------------------
    async def start_in_background(self) -> asyncio.Task:
        """Kick off the discord client login. Returns the task."""
        task = asyncio.create_task(self.client.start(self.token))
        # wait until on_ready fires
        await self._ready_evt.wait()
        return task

    async def stop(self) -> None:
        try:
            if self._voice and self._voice.is_connected():
                await self._voice.disconnect(force=True)
        except Exception:
            pass
        await self.client.close()

    async def _channel(self, channel_id: int) -> Optional[discord.TextChannel]:
        ch = self.client.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.client.fetch_channel(channel_id)
            except Exception as e:  # noqa: BLE001
                log.warning("Channel %s fetch failed: %s", channel_id, e)
                return None
        return ch if isinstance(ch, discord.TextChannel) else None

    async def send_alert(self, payload: AlertPayload) -> Optional[int]:
        """Post the rich embed; return message ID for follow-up threading."""
        target_id = self.channel_small if payload.is_small_cap else self.channel_mid
        ch = await self._channel(target_id)
        if not ch:
            return None
        # Full embed for: B+, 30%+ moves, or any signal-tagged alert (NHOD, GAPGO, POP, CONT)
        _signal_tags = ("NHOD", "GAPGO", "POP", "CONT", "LOW FLOAT",
                        "MEGA", "TOP20", "CONGRESS", "CONTRACT")
        has_signal = (any(tag in payload.band_label for tag in _signal_tags)
                      or (payload.quiver_signal and payload.quiver_signal.has_signal))
        use_full = payload.score.grade != "C" or payload.change_pct >= 30 or has_signal
        embed = build_embed(payload) if use_full else build_compact_embed(payload)
        try:
            msg = await ch.send(embed=embed)
        except discord.HTTPException as e:
            log.warning("Failed to send embed: %s", e)
            return None

        # Voice-alert if grade is high enough
        if self._grade_meets(payload.score.grade, self.speak_min_grade):
            phrase = tts_mod.build_phrase(
                grade=payload.score.grade,
                symbol=payload.symbol,
                change_pct=payload.change_pct,
                price=payload.last_price,
                is_small_cap=payload.is_small_cap,
            )
            repeats = 3 if payload.score.grade == "A+" else 1
            asyncio.create_task(
                self._speak_repeat(phrase, repeats, grade=payload.score.grade)
            )

        return msg.id

    async def send_followup(
        self,
        *,
        symbol: str,
        change_pct: float,
        streak: int,
        is_nhod: bool,
        is_small_cap: bool,
    ) -> None:
        target_id = self.channel_small if is_small_cap else self.channel_mid
        ch = await self._channel(target_id)
        if not ch:
            return
        try:
            await ch.send(build_followup_line(symbol, change_pct, streak, is_nhod))
        except discord.HTTPException as e:
            log.warning("Failed to send followup: %s", e)

    @staticmethod
    def _grade_meets(actual: str, minimum: str) -> bool:
        order = ["F", "D", "C", "B", "A", "A+"]
        try:
            return order.index(actual) >= order.index(minimum)
        except ValueError:
            return False
