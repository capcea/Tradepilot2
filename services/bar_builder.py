"""M5 bar construction from ticks or M1 candles (SPEC.md §13.1).

Bars carry bid and ask OHLC separately so spread is observable per bar. The
spread_median of a tick-built bar is the median tick spread; for an M1-built
bar it is the median of per-minute open spreads (the best available proxy —
documented in DECISIONS.md). Minutes missing on either side are dropped rather
than interpolated.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import groupby
from typing import Iterable, Sequence

from core.events import M1Candle, M5Bar, Tick

M5 = timedelta(minutes=5)


def floor_to_m5(ts: datetime) -> datetime:
    return ts.replace(minute=ts.minute - ts.minute % 5, second=0, microsecond=0)


def m5_from_ticks(ticks: Iterable[Tick]) -> list[M5Bar]:
    ordered = sorted(ticks, key=lambda t: t.ts_utc)
    bars: list[M5Bar] = []
    for bucket_open, group in groupby(ordered, key=lambda t: floor_to_m5(t.ts_utc)):
        chunk = list(group)
        bids = [t.bid for t in chunk]
        asks = [t.ask for t in chunk]
        spreads = sorted(a - b for a, b in zip(asks, bids))
        bars.append(
            M5Bar(
                symbol=chunk[0].symbol,
                ts_open_utc=bucket_open,
                bid_o=bids[0], bid_h=max(bids), bid_l=min(bids), bid_c=bids[-1],
                ask_o=asks[0], ask_h=max(asks), ask_l=min(asks), ask_c=asks[-1],
                tick_volume=len(chunk),
                spread_median=statistics.median(spreads),
            )
        )
    return bars


def m5_from_m1(bid: Sequence[M1Candle], ask: Sequence[M1Candle]) -> list[M5Bar]:
    ask_by_ts = {c.ts_open_utc: c for c in ask}
    paired = [
        (b, ask_by_ts[b.ts_open_utc])
        for b in sorted(bid, key=lambda c: c.ts_open_utc)
        if b.ts_open_utc in ask_by_ts
    ]
    bars: list[M5Bar] = []
    for bucket_open, group in groupby(paired, key=lambda pair: floor_to_m5(pair[0].ts_open_utc)):
        chunk = list(group)
        bs = [p[0] for p in chunk]
        as_ = [p[1] for p in chunk]
        spreads = [a.o - b.o for b, a in chunk]
        bars.append(
            M5Bar(
                symbol=bs[0].symbol,
                ts_open_utc=bucket_open,
                bid_o=bs[0].o, bid_h=max(c.h for c in bs),
                bid_l=min(c.l for c in bs), bid_c=bs[-1].c,
                ask_o=as_[0].o, ask_h=max(c.h for c in as_),
                ask_l=min(c.l for c in as_), ask_c=as_[-1].c,
                tick_volume=int(round(sum(c.volume for c in bs))),
                spread_median=statistics.median(sorted(spreads)),
            )
        )
    return bars


def find_missing_m5(bars: Sequence[M5Bar], start_utc: datetime, end_utc: datetime) -> list[datetime]:
    """Expected-but-absent M5 opens in [start_utc, end_utc); §6.4 gap detection."""
    present = {b.ts_open_utc for b in bars}
    missing = []
    ts = floor_to_m5(start_utc)
    while ts < end_utc:
        if ts >= start_utc and ts not in present:
            missing.append(ts)
        ts += M5
    return missing
