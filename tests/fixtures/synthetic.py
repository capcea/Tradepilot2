"""Synthetic M5 fixture builders for the SSR core tests (SPEC.md M2).

All fixtures use 2025-01-15 (winter Wednesday: London == UTC) unless stated, an
Asian range of 1.04500-1.04700 (20 pips) and ADR20 = 60 pips, so:
  sweep_min_pen = max(2 pips, 0.1 x 20 pips) = 2 pips
  sweep_max_pen = 0.6 x 20 pips = 12 pips
  range_max     = 0.6 x 60 pips = 36 pips
Spread is a constant 1.2 points (0.00012) so stop_buffer = 2 pips + 0.00012.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from core.events import M5Bar

UTC = timezone.utc
DAY = date(2025, 1, 15)
SPREAD = Decimal("0.00012")

RANGE_LOW = Decimal("1.04500")
RANGE_HIGH = Decimal("1.04700")


def bar(
    ts: datetime,
    o: str | Decimal,
    h: str | Decimal,
    l: str | Decimal,
    c: str | Decimal,
    symbol: str = "EURUSD",
    spread: Decimal = SPREAD,
    vol: int = 25,
) -> M5Bar:
    o, h, l, c = Decimal(o), Decimal(h), Decimal(l), Decimal(c)
    assert h >= max(o, c) and l <= min(o, c), "fixture bar is not OHLC-sane"
    return M5Bar(
        symbol=symbol, ts_open_utc=ts,
        bid_o=o, bid_h=h, bid_l=l, bid_c=c,
        ask_o=o + spread, ask_h=h + spread, ask_l=l + spread, ask_c=c + spread,
        tick_volume=vol, spread_median=spread,
    )


def t(hh: int, mm: int, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)


def asian_session(
    low: Decimal = RANGE_LOW,
    high: Decimal = RANGE_HIGH,
    symbol: str = "EURUSD",
    day: date = DAY,
    drop_opens: tuple[datetime, ...] = (),
) -> list[M5Bar]:
    """83 flat bars 00:00..06:50 spanning exactly [low, high]."""
    bars = []
    ts = t(0, 0, day)
    end = t(6, 55, day)
    mid = (low + high) / 2
    while ts < end:
        if ts not in drop_opens:
            bars.append(bar(ts, mid, high, low, mid, symbol=symbol))
        ts += timedelta(minutes=5)
    return bars


# --- entry-window continuation bars -----------------------------------------

def clean_long_bars() -> list[M5Bar]:
    """Sweep below range low, two-bar penetration, quality reclaim at 07:15."""
    return [
        bar(t(7, 5), "1.04540", "1.04560", "1.04470", "1.04485"),   # pen 3 pips
        bar(t(7, 10), "1.04485", "1.04500", "1.04460", "1.04475"),  # sweep_low 1.04460
        bar(t(7, 15), "1.04475", "1.04530", "1.04465", "1.04520"),  # reclaim, top 40%
    ]


def clean_short_bars() -> list[M5Bar]:
    return [
        bar(t(7, 5), "1.04660", "1.04730", "1.04640", "1.04715"),   # pen 3 pips above
        bar(t(7, 10), "1.04715", "1.04740", "1.04700", "1.04720"),  # sweep_high 1.04740
        bar(t(7, 15), "1.04670", "1.04735", "1.04670", "1.04680"),  # reclaim, bottom 40%
    ]


def deep_sweep_bars() -> list[M5Bar]:
    """Penetration 13 pips > sweep_max_pen 12 -> real breakout, skip."""
    return [bar(t(7, 5), "1.04520", "1.04540", "1.04370", "1.04400")]


def no_reclaim_bars() -> list[M5Bar]:
    """First penetration at 07:05; six more bars never close >= 1 pip inside."""
    bars = [bar(t(7, 5), "1.04540", "1.04560", "1.04470", "1.04480")]
    for i, mm in enumerate((10, 15, 20, 25, 30, 35, 40)):
        bars.append(bar(t(7, mm), "1.04480", "1.04508", "1.04465", "1.04505"))
    return bars


def reclaim_quality_fail_bars() -> list[M5Bar]:
    """Reclaim close is back inside by >= 1 pip but only in the lower 60%."""
    return [
        bar(t(7, 5), "1.04540", "1.04560", "1.04470", "1.04485"),
        bar(t(7, 10), "1.04485", "1.04560", "1.04460", "1.04512"),  # quality threshold 1.04520
    ]


def stop_oob_bars() -> list[M5Bar]:
    """Deep-but-legal sweep + high reclaim close -> stop distance 24.2 pips > 22."""
    return [
        bar(t(7, 5), "1.04520", "1.04540", "1.04390", "1.04420"),   # pen 11 pips (legal)
        bar(t(7, 10), "1.04420", "1.04620", "1.04400", "1.04600"),  # reclaim, quality OK
    ]
