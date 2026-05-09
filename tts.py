"""
TTS helper for spoken alerts.

Produces an MP3 file using gTTS (free, no API key) saying e.g.:

    "A plus alert. Ticker P L T R. Up 9.18 percent. Price 24.85."

Tickers are spelled letter-by-letter so they don't get mispronounced
(otherwise gTTS turns "PLTR" into something like "puh-litter").

The MP3 path is returned and the *caller* is responsible for deleting it
after the bot has finished playing it.

We intentionally avoid pyttsx3 because it requires platform-specific
TTS engines that aren't reliably available in container/VPS environments.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Optional

log = logging.getLogger("tts")


def spell_ticker(symbol: str) -> str:
    """Convert 'PLTR' -> 'P. L. T. R.' for clean letter-by-letter speech."""
    return ". ".join(list(symbol.upper())) + "."


def build_phrase(
    *,
    grade: str,
    symbol: str,
    change_pct: float,
    price: float,
    is_small_cap: bool = True,
) -> str:
    """Compose the sentence the bot will speak."""
    grade_spoken = {
        "A+": "A plus",
        "A": "A",
        "B": "B",
        "C": "C",
    }.get(grade, grade)
    band = "small cap" if is_small_cap else "mid cap"
    direction = "up" if change_pct >= 0 else "down"
    return (
        f"{grade_spoken} alert. {band}. "
        f"Ticker {spell_ticker(symbol)} "
        f"{direction} {abs(change_pct):.1f} percent. "
        f"Price {price:.2f}."
    )


def synthesize(
    phrase: str,
    *,
    out_dir: Optional[str] = None,
    lang: str = "en",
    tld: str = "com",
) -> Optional[str]:
    """Generate an MP3 file from `phrase`. Returns the path, or None on failure."""
    try:
        from gtts import gTTS  # imported lazily so unit-tests don't need it
    except ImportError:
        log.error("gTTS not installed (pip install gTTS)")
        return None

    safe = re.sub(r"[^A-Za-z0-9]+", "_", phrase)[:40] or "alert"
    out_dir = out_dir or tempfile.gettempdir()
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"tts_{safe}.mp3")
    try:
        gTTS(text=phrase, lang=lang, tld=tld).save(path)
        return path
    except Exception as e:  # noqa: BLE001
        log.warning("gTTS synthesis failed: %s", e)
        return None


# ---- Self test --------------------------------------------------------------
def _selftest() -> int:
    phrase = build_phrase(grade="A+", symbol="PLTR", change_pct=9.18, price=24.85)
    print("Phrase:", phrase)
    assert "P. L. T. R." in phrase
    assert "A plus alert" in phrase
    assert "9.2 percent" in phrase  # rounded
    print("OK: phrase composition works.")
    print("(Skipping actual gTTS network call in selftest.)")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
