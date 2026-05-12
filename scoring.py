"""Score a candidate gainer 0-5 (+1 momentum bonus) and assign a letter grade."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass
class ScoringConfig:
    rvol_threshold: float = 1.5   # was 3.0 — large stocks rarely 3x avg vol on a big day
    gap_threshold_pct: float = 2.0  # was 3.0 — catches intraday runners without full gap
    volume_threshold: int = 500_000
    tech_proximity_pct: float = 1.0
    nhod_proximity_pct: float = 0.05


@dataclass
class ScoreResult:
    catalyst: bool
    rvol_ok: bool
    gap_ok: bool
    volume_ok: bool
    technical_ok: bool
    nhod_bonus: bool
    rvol_value: Optional[float] = None
    gap_value: Optional[float] = None

    @property
    def base(self) -> int:
        return sum([self.catalyst, self.rvol_ok, self.gap_ok,
                    self.volume_ok, self.technical_ok])

    @property
    def total(self) -> int:
        return self.base + (1 if self.nhod_bonus else 0)

    @property
    def grade(self) -> str:
        return {6: "A+", 5: "A", 4: "B", 3: "C", 2: "D"}.get(self.total, "F")

    def display(self) -> str:
        return f"{self.total}/5"


def score(*, has_news, todays_volume, avg_volume_20d, todays_open, prev_close,
          todays_high, last_price, cfg=None):
    cfg = cfg or ScoringConfig()
    rvol_value = None
    rvol_ok = False
    if avg_volume_20d and avg_volume_20d > 0:
        rvol_value = todays_volume / avg_volume_20d
        rvol_ok = rvol_value >= cfg.rvol_threshold

    gap_value = None
    gap_ok = False
    if todays_open is not None and prev_close and prev_close > 0:
        gap_value = (todays_open - prev_close) / prev_close * 100.0
        gap_ok = gap_value >= cfg.gap_threshold_pct

    volume_ok = todays_volume >= cfg.volume_threshold

    technical_ok = False
    nhod_bonus = False
    if todays_high and todays_high > 0:
        proximity = (todays_high - last_price) / todays_high * 100.0
        technical_ok = proximity <= cfg.tech_proximity_pct
        nhod_bonus = proximity <= cfg.nhod_proximity_pct

    return ScoreResult(
        catalyst=has_news, rvol_ok=rvol_ok, gap_ok=gap_ok,
        volume_ok=volume_ok, technical_ok=technical_ok,
        nhod_bonus=nhod_bonus, rvol_value=rvol_value, gap_value=gap_value,
    )


def _selftest():
    cfg = ScoringConfig()
    a = score(has_news=True, todays_volume=2_310_000, avg_volume_20d=82_000,
              todays_open=15.20, prev_close=13.05, todays_high=17.85,
              last_price=17.85, cfg=cfg)
    assert a.grade == "A+", a
    print(f"A+ test: total={a.total} grade={a.grade} rvol={a.rvol_value:.1f}x")
    b = score(has_news=True, todays_volume=2_000_000, avg_volume_20d=300_000,
              todays_open=10.0, prev_close=9.95, todays_high=10.50,
              last_price=10.45, cfg=cfg)
    print(f"B test: total={b.total} grade={b.grade}")
    assert b.grade in ("B", "A"), b
    f = score(has_news=False, todays_volume=50_000, avg_volume_20d=300_000,
              todays_open=10.0, prev_close=9.95, todays_high=10.20,
              last_price=10.05, cfg=cfg)
    print(f"F test: total={f.total} grade={f.grade}")
    assert f.grade == "F", f
    print("OK")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
