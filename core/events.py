"""Domain events and market-data value types (SPEC.md §10.1, §13.4).

Pure module. These are the ONLY shapes the strategy/risk core consumes; the
backtest and live shells construct identical objects, which is what guarantees
"backtest code == live code". Prices and money are Decimal everywhere.

SSR convention: OHLC predicates (range, sweep, reclaim) are evaluated on BID
prices — the MT5 chart feed — while the ask side exists so spread is observable
per bar (§13.1) and entries/exits can be costed on the correct side.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class Tick:
    symbol: str
    ts_utc: datetime
    bid: Decimal
    ask: Decimal
    bid_vol: float = 0.0
    ask_vol: float = 0.0

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class M1Candle:
    """One-minute candle for a single side (bid OR ask) of the book."""

    symbol: str
    side: Literal["bid", "ask"]
    ts_open_utc: datetime
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    volume: float


@dataclass(frozen=True, slots=True)
class M5Bar:
    """Closed M5 bar with both sides, timestamped by UTC open time."""

    symbol: str
    ts_open_utc: datetime
    bid_o: Decimal
    bid_h: Decimal
    bid_l: Decimal
    bid_c: Decimal
    ask_o: Decimal
    ask_h: Decimal
    ask_l: Decimal
    ask_c: Decimal
    tick_volume: int
    spread_median: Decimal  # in price units, e.g. Decimal("0.00012")

    @property
    def ts_close_utc(self) -> datetime:
        from datetime import timedelta

        return self.ts_open_utc + timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class EconEvent:
    id: str
    ts_utc: datetime
    currency: str
    impact: Literal["high", "medium", "low", "holiday", "unknown"]
    title: str
    source: str


# ---------------------------------------------------------------------------
# Events consumed by the core (§ build brief: BarClosed, TickBatch,
# CalendarUpdate, Command)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BarClosed:
    bar: M5Bar


@dataclass(frozen=True, slots=True)
class TickBatch:
    """Latest quote state per poll cycle; used for spread-at-send checks."""

    symbol: str
    ts_utc: datetime
    bid: Decimal
    ask: Decimal

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class CalendarUpdate:
    events: tuple[EconEvent, ...]


@dataclass(frozen=True, slots=True)
class Command:
    kind: Literal["pause", "resume", "kill", "flat_all"]
    reason: str = ""
