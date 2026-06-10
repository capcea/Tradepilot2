"""Session Sweep-and-Reclaim engine — pure detection + management (SPEC.md §3-§5).

The engine consumes closed M5 bars for ONE symbol and ONE trading day and emits
value objects (RangeLocked / NoTradeDay / AttemptEnded / SetupDetected). It
performs no I/O, reads no clock, and sizes nothing: sizing, account-state
filters and order handling belong to the risk engine and shells.

Conventions (DECISIONS.md M2):
- All OHLC predicates evaluate on the BID side (the MT5 chart feed).
- Direction invalidation: penetration beyond the extreme greater than
  sweep_max_pen (0.60 x range) kills the direction — a close that far outside
  is implied (close >= low), so the wick test is the binding, conservative one.
- The FIRST bar that closes back inside the range by >= 1 pip is THE reclaim
  candidate; if its close quality fails the 40% rule, the direction attempt is
  consumed (vetoed setups do not retry the same direction, §3.2).
- One long and one short attempt per pair per day.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from core.config_schema import PairParams, SharedParams
from core.events import M5Bar
from core.filters import FilterFailure
from core.reasons import ReasonCode
from core.timebase import SessionWindows

Direction = Literal["long", "short"]


# ---------------------------------------------------------------------------
# Inputs / outputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DayInputs:
    trading_day: date
    windows: SessionWindows
    window_violations: tuple[str, ...]
    adr20_pips: Decimal | None


@dataclass(frozen=True, slots=True)
class RangeLocked:
    symbol: str
    range_high: Decimal
    range_low: Decimal
    width_pips: Decimal


@dataclass(frozen=True, slots=True)
class NoTradeDay:
    symbol: str
    failures: tuple[FilterFailure, ...]


@dataclass(frozen=True, slots=True)
class AttemptEnded:
    symbol: str
    direction: Direction
    code: ReasonCode
    detail: str = ""


@dataclass(frozen=True)
class SetupCandidate:
    setup_id: str
    symbol: str
    direction: Direction
    ts_utc: datetime  # reclaim bar close time == decision time
    range_high: Decimal
    range_low: Decimal
    sweep_extreme: Decimal
    reclaim_close: Decimal
    entry_ref: Decimal
    sl: Decimal
    tp1: Decimal
    tp2: Decimal
    stop_pips: Decimal
    decision_spread: Decimal
    features: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SetupDetected:
    candidate: SetupCandidate


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class _Attempt:
    __slots__ = ("state", "sweep_extreme", "bars_since_pen")

    def __init__(self) -> None:
        self.state: str = "watching"  # watching | swept | done
        self.sweep_extreme: Decimal | None = None
        self.bars_since_pen: int = 0


class SSREngine:
    def __init__(
        self,
        symbol: str,
        pair: PairParams,
        shared: SharedParams,
        pip: Decimal,
        day: DayInputs,
    ):
        self.symbol = symbol
        self.pair = pair
        self.shared = shared
        self.pip = pip
        self.day = day
        self._asian_bars: list[M5Bar] = []
        self._locked = False
        self._no_trade = False
        self._dead = False  # day fully announced; ignore further bars
        self.range_high: Decimal | None = None
        self.range_low: Decimal | None = None
        self._attempts: dict[Direction, _Attempt] = {"long": _Attempt(), "short": _Attempt()}

    # -- public ---------------------------------------------------------------

    def on_bar(self, bar: M5Bar) -> list[object]:
        if self._dead:
            return []
        w = self.day.windows

        if self.day.window_violations:
            self._dead = True
            return [NoTradeDay(self.symbol, (FilterFailure(
                ReasonCode.DST_ANOMALY, ";".join(self.day.window_violations)),))]

        if w.asian_start_utc <= bar.ts_open_utc < w.asian_end_utc:
            self._asian_bars.append(bar)
            return []

        out: list[object] = []
        if not self._locked and bar.ts_open_utc >= w.asian_end_utc:
            out.extend(self._lock_range())
            if self._dead:
                return out

        if (
            self._locked
            and bar.ts_open_utc >= w.entry_start_utc
            and bar.ts_close_utc <= w.entry_end_utc
        ):
            out.extend(self._process("long", bar))
            out.extend(self._process("short", bar))
        return out

    # -- range lock -------------------------------------------------------------

    def _lock_range(self) -> list[object]:
        w = self.day.windows
        fails: list[FilterFailure] = []

        expected = int((w.asian_end_utc - w.asian_start_utc) / timedelta(minutes=5))
        missing = expected - len(self._asian_bars)
        if missing > 2:
            fails.append(FilterFailure(ReasonCode.DATA_INCOMPLETE, f"{missing} asian bars missing"))
        if self.day.adr20_pips is None:
            fails.append(FilterFailure(ReasonCode.DATA_INCOMPLETE, "adr20 unavailable"))

        if not self._asian_bars:
            fails.append(FilterFailure(ReasonCode.DATA_INCOMPLETE, "no asian bars at all"))
            self._dead = True
            return [NoTradeDay(self.symbol, tuple(fails))]

        high = max(b.bid_h for b in self._asian_bars)
        low = min(b.bid_l for b in self._asian_bars)
        width_pips = (high - low) / self.pip
        if width_pips < self.pair.range_min_pips:
            fails.append(FilterFailure(ReasonCode.RANGE_TOO_NARROW, f"width={width_pips}p"))
        if self.day.adr20_pips is not None:
            cap = self.shared.range_max_adr_mult * self.day.adr20_pips
            if width_pips > cap:
                fails.append(FilterFailure(ReasonCode.RANGE_TOO_WIDE, f"width={width_pips}p cap={cap}p"))

        if fails:
            self._dead = True
            self._no_trade = True
            return [NoTradeDay(self.symbol, tuple(fails))]

        self.range_high, self.range_low = high, low
        self._locked = True
        return [RangeLocked(self.symbol, high, low, width_pips)]

    # -- sweep / reclaim ---------------------------------------------------------

    def _process(self, direction: Direction, bar: M5Bar) -> list[object]:
        a = self._attempts[direction]
        if a.state == "done":
            return []

        long = direction == "long"
        width = self.range_high - self.range_low
        min_pen = max(self.pair.sweep_min_pips * self.pip, Decimal("0.1") * width)
        max_pen = self.shared.sweep_max_range_mult * width
        pen = (self.range_low - bar.bid_l) if long else (bar.bid_h - self.range_high)

        if a.state == "watching":
            if pen < min_pen:
                return []
            if pen > max_pen:
                a.state = "done"
                return [AttemptEnded(self.symbol, direction, ReasonCode.SWEEP_TOO_DEEP,
                                     f"pen={pen / self.pip}p")]
            a.state = "swept"
            a.sweep_extreme = bar.bid_l if long else bar.bid_h
            a.bars_since_pen = 0
        else:  # swept
            a.bars_since_pen += 1
            a.sweep_extreme = (
                min(a.sweep_extreme, bar.bid_l) if long else max(a.sweep_extreme, bar.bid_h)
            )
            if pen > max_pen:
                a.state = "done"
                return [AttemptEnded(self.symbol, direction, ReasonCode.SWEEP_TOO_DEEP,
                                     f"pen={pen / self.pip}p")]

        inside = (
            bar.bid_c >= self.range_low + self.pip if long
            else bar.bid_c <= self.range_high - self.pip
        )
        if inside:
            a.state = "done"
            return self._on_reclaim(direction, a, bar)
        if a.bars_since_pen > self.shared.reclaim_bars:
            a.state = "done"
            return [AttemptEnded(self.symbol, direction, ReasonCode.NO_RECLAIM,
                                 f"no reclaim within {self.shared.reclaim_bars} bars")]
        return []

    def _on_reclaim(self, direction: Direction, a: _Attempt, bar: M5Bar) -> list[object]:
        long = direction == "long"
        bar_range = bar.bid_h - bar.bid_l
        q = self.shared.reclaim_quality_pct
        quality_ok = (
            bar.bid_c >= bar.bid_h - q * bar_range if long
            else bar.bid_c <= bar.bid_l + q * bar_range
        )
        if not quality_ok:
            return [AttemptEnded(self.symbol, direction, ReasonCode.RECLAIM_QUALITY,
                                 f"close={bar.bid_c}")]

        spread = bar.spread_median
        buffer = self.pair.stop_buffer_pips * self.pip + spread
        entry_ref = bar.bid_c
        if long:
            sl = a.sweep_extreme - buffer
            stop_dist = entry_ref - sl
        else:
            sl = a.sweep_extreme + buffer
            stop_dist = sl - entry_ref
        stop_pips = stop_dist / self.pip
        if not (self.pair.stop_min <= stop_pips <= self.pair.stop_max):
            return [AttemptEnded(self.symbol, direction, ReasonCode.STOP_OOB,
                                 f"stop={stop_pips}p bounds=[{self.pair.stop_min},{self.pair.stop_max}]")]

        if long:
            tp1 = entry_ref + self.shared.tp1_r * stop_dist
            tp2 = min(entry_ref + self.shared.tp2_r_cap * stop_dist, self.range_high - self.pip)
            structure_ok = tp2 > tp1 > entry_ref
        else:
            tp1 = entry_ref - self.shared.tp1_r * stop_dist
            tp2 = max(entry_ref - self.shared.tp2_r_cap * stop_dist, self.range_low + self.pip)
            structure_ok = tp2 < tp1 < entry_ref
        if not structure_ok:
            return [AttemptEnded(self.symbol, direction, ReasonCode.TARGET_STRUCTURE_INVALID,
                                 f"tp1={tp1} tp2={tp2}")]

        width_pips = (self.range_high - self.range_low) / self.pip
        candidate = SetupCandidate(
            setup_id=f"{self.day.trading_day.isoformat()}|{self.symbol}|{direction}",
            symbol=self.symbol,
            direction=direction,
            ts_utc=bar.ts_close_utc,
            range_high=self.range_high,
            range_low=self.range_low,
            sweep_extreme=a.sweep_extreme,
            reclaim_close=bar.bid_c,
            entry_ref=entry_ref,
            sl=sl,
            tp1=tp1,
            tp2=tp2,
            stop_pips=stop_pips,
            decision_spread=spread,
            features={
                "range_width_pips": str(width_pips),
                "sweep_depth_pips": str(
                    ((self.range_low - a.sweep_extreme) if long else (a.sweep_extreme - self.range_high))
                    / self.pip
                ),
                "bars_to_reclaim": a.bars_since_pen,
                "adr20_pips": str(self.day.adr20_pips),
            },
        )
        return [SetupDetected(candidate)]


# ---------------------------------------------------------------------------
# Position management (§3.1 exits). Pure; the fill model lives in the shells.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class OpenPosition:
    symbol: str
    direction: Direction
    entry_price: Decimal
    entry_ts_utc: datetime
    sl: Decimal
    tp1: Decimal
    tp2: Decimal
    lots_total: Decimal
    lots_open: Decimal
    tp1_done: bool
    r_price: Decimal  # initial stop distance in price units


@dataclass(frozen=True, slots=True)
class ExitSignal:
    kind: Literal["sl", "tp1", "tp2", "time", "forced"]
    lots: Decimal
    level: Decimal | None
    ts_utc: datetime


def manage_on_quote(
    pos: OpenPosition,
    bid: Decimal,
    now: datetime,
    *,
    forced_exit_utc: datetime,
    time_stop_min: int,
    time_stop_threshold_r: Decimal,
    tp1_fraction: Decimal,
) -> tuple[OpenPosition | None, list[ExitSignal]]:
    """Live/paper management on a quote poll. SL and TP2 are NATIVE brackets at
    the broker (never managed only in client memory, §4.7); this handles what
    brackets cannot: the TP1 partial + breakeven move, the time stop, and the
    forced exit."""
    long = pos.direction == "long"
    if (not pos.tp1_done) and (bid >= pos.tp1 if long else bid <= pos.tp1):
        lots = pos.lots_total * tp1_fraction
        new_pos = replace(
            pos, lots_open=pos.lots_open - lots, tp1_done=True, sl=pos.entry_price
        )
        return new_pos, [ExitSignal("tp1", lots, pos.tp1, now)]
    if now >= forced_exit_utc:
        return None, [ExitSignal("forced", pos.lots_open, None, now)]
    if now - pos.entry_ts_utc >= timedelta(minutes=time_stop_min):
        unrealized_r = (
            (bid - pos.entry_price) / pos.r_price if long
            else (pos.entry_price - bid) / pos.r_price
        )
        if unrealized_r < time_stop_threshold_r:
            return None, [ExitSignal("time", pos.lots_open, None, now)]
    return pos, []


def manage_on_bar(
    pos: OpenPosition,
    bar: M5Bar,
    *,
    forced_exit_utc: datetime,
    time_stop_min: int,
    time_stop_threshold_r: Decimal,
    tp1_fraction: Decimal,
) -> tuple[OpenPosition | None, list[ExitSignal]]:
    """Apply §3.1 exits for one closed bar. Conservative intrabar ordering:
    if SL and any TP are both touched, SL wins (same-bar SL-before-TP).
    Breakeven SL set by TP1 only binds from the NEXT bar."""
    long = pos.direction == "long"
    ts = bar.ts_close_utc

    sl_hit = bar.bid_l <= pos.sl if long else bar.bid_h >= pos.sl
    tp1_hit = (not pos.tp1_done) and (bar.bid_h >= pos.tp1 if long else bar.bid_l <= pos.tp1)
    tp2_hit = bar.bid_h >= pos.tp2 if long else bar.bid_l <= pos.tp2

    if sl_hit:
        return None, [ExitSignal("sl", pos.lots_open, pos.sl, ts)]

    signals: list[ExitSignal] = []
    new_pos = pos
    if tp1_hit:
        close_lots = pos.lots_total * tp1_fraction
        signals.append(ExitSignal("tp1", close_lots, pos.tp1, ts))
        new_pos = replace(
            pos, lots_open=pos.lots_open - close_lots, tp1_done=True, sl=pos.entry_price
        )
    if tp2_hit and new_pos.tp1_done:
        signals.append(ExitSignal("tp2", new_pos.lots_open, new_pos.tp2, ts))
        return None, signals
    if signals:
        return new_pos, signals

    if ts >= forced_exit_utc:
        return None, [ExitSignal("forced", pos.lots_open, None, ts)]

    if ts - pos.entry_ts_utc >= timedelta(minutes=time_stop_min):
        unrealized_r = (
            (bar.bid_c - pos.entry_price) / pos.r_price if long
            else (pos.entry_price - bar.bid_c) / pos.r_price
        )
        if unrealized_r < time_stop_threshold_r:
            return None, [ExitSignal("time", pos.lots_open, None, ts)]

    return new_pos, []
