"""Filter pipeline: the complete §6 no-trade list, plus ADR/percentile helpers.

Pure module. `evaluate_no_trade` reports EVERY failed condition (not just the
first) so the decision trail shows the full picture; an empty result means the
entry gate is open.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from core.events import EconEvent
from core.reasons import ReasonCode
from core.timebase import SessionWindows


@dataclass(frozen=True, slots=True)
class OpsHealth:
    stale_tick: bool = False
    clock_skew: bool = False
    reconnecting: bool = False
    kill: bool = False
    pause: bool = False


@dataclass(frozen=True, slots=True)
class FilterFailure:
    code: ReasonCode
    detail: str = ""


@dataclass(frozen=True)
class FilterInputs:
    ts_utc: datetime
    windows: SessionWindows
    window_violations: tuple[str, ...]
    events: tuple[EconEvent, ...]
    currencies: tuple[str, ...]
    news_pre_min: int
    news_post_min: int
    news_lookahead_min: int
    spread: Decimal
    spread_abs_cap: Decimal
    spread_median_60m: Decimal | None
    spread_median_mult: Decimal
    adr20_pctile: Decimal | None
    adr_skip_low: int
    adr_skip_high: int
    bank_holiday: bool
    monday_gap: bool
    halt_reasons: tuple[ReasonCode, ...]
    ops: OpsHealth
    symbol_valid: bool
    position_open_in_risk_unit: bool


def spread_gate_ok(
    spread: Decimal,
    median_60m: Decimal | None,
    median_mult: Decimal,
    abs_cap: Decimal,
) -> bool:
    """§3.1 spread gate: spread <= mult x rolling median AND <= absolute cap.
    With no median yet (warm-up), only the absolute cap applies — conservative
    enough because the cap is the binding constraint in calm conditions."""
    if spread > abs_cap:
        return False
    if median_60m is not None and spread > median_mult * median_60m:
        return False
    return True


def evaluate_no_trade(i: FilterInputs) -> tuple[FilterFailure, ...]:
    fails: list[FilterFailure] = []

    # §6.9 DST/window self-check
    if i.window_violations:
        fails.append(FilterFailure(ReasonCode.DST_ANOMALY, ";".join(i.window_violations)))

    # §6.6 risk/compliance halts (computed by the risk engine, passed through)
    for code in i.halt_reasons:
        fails.append(FilterFailure(code, "halt active"))

    # §6.1 / §6.2 news
    for e in i.events:
        if e.impact != "high" or e.currency not in i.currencies:
            continue
        blackout_start = e.ts_utc - timedelta(minutes=i.news_pre_min)
        blackout_end = e.ts_utc + timedelta(minutes=i.news_post_min)
        if blackout_start <= i.ts_utc <= blackout_end:
            fails.append(FilterFailure(ReasonCode.NEWS_BLACKOUT, f"{e.currency} {e.title} @ {e.ts_utc:%H:%M}"))
        elif timedelta(0) < e.ts_utc - i.ts_utc <= timedelta(minutes=i.news_lookahead_min):
            fails.append(FilterFailure(ReasonCode.NEWS_LOOKAHEAD, f"{e.currency} {e.title} @ {e.ts_utc:%H:%M}"))

    # §6.3 spread gate
    if not spread_gate_ok(i.spread, i.spread_median_60m, i.spread_median_mult, i.spread_abs_cap):
        fails.append(FilterFailure(
            ReasonCode.SPREAD_GATE,
            f"spread={i.spread} median={i.spread_median_60m} cap={i.spread_abs_cap}",
        ))

    # §6.5 ADR regime
    if i.adr20_pctile is None:
        fails.append(FilterFailure(ReasonCode.DATA_INCOMPLETE, "adr20 percentile unavailable"))
    elif i.adr20_pctile < i.adr_skip_low:
        fails.append(FilterFailure(ReasonCode.ADR_REGIME_LOW, f"pctile={i.adr20_pctile}"))
    elif i.adr20_pctile > i.adr_skip_high:
        fails.append(FilterFailure(ReasonCode.ADR_REGIME_HIGH, f"pctile={i.adr20_pctile}"))

    # §6.7 holidays / rollover / Friday cutoff + entry window
    if i.bank_holiday:
        fails.append(FilterFailure(ReasonCode.BANK_HOLIDAY))
    w = i.windows
    if w.rollover_start_utc <= i.ts_utc < w.rollover_end_utc:
        fails.append(FilterFailure(ReasonCode.ROLLOVER_WINDOW))
    if w.friday_no_new_after_utc is not None and i.ts_utc >= w.friday_no_new_after_utc:
        fails.append(FilterFailure(ReasonCode.FRIDAY_LATE))
    if not (w.entry_start_utc <= i.ts_utc <= w.entry_end_utc):
        fails.append(FilterFailure(ReasonCode.OUTSIDE_ENTRY_WINDOW))

    # §6.8 Monday gap
    if i.monday_gap:
        fails.append(FilterFailure(ReasonCode.MONDAY_GAP))

    # §6.10 ops health
    if i.ops.stale_tick or i.ops.clock_skew or i.ops.reconnecting:
        detail = ",".join(
            n for n, v in (("stale_tick", i.ops.stale_tick), ("clock_skew", i.ops.clock_skew),
                           ("reconnecting", i.ops.reconnecting)) if v
        )
        fails.append(FilterFailure(ReasonCode.OPS_UNHEALTHY, detail))
    if i.ops.kill:
        fails.append(FilterFailure(ReasonCode.KILLED))
    if i.ops.pause:
        fails.append(FilterFailure(ReasonCode.PAUSED))

    # §6.11 / §6.12
    if not i.symbol_valid:
        fails.append(FilterFailure(ReasonCode.SYMBOL_INVALID))
    if i.position_open_in_risk_unit:
        fails.append(FilterFailure(ReasonCode.POSITION_OPEN))

    return tuple(fails)


# ---------------------------------------------------------------------------
# In-house ADR/percentile helpers (no TA-Lib per build brief)
# ---------------------------------------------------------------------------

def compute_adr(daily_ranges: Sequence[Decimal], n: int = 20) -> Decimal | None:
    """Mean of the last n daily ranges; None when history is insufficient."""
    if len(daily_ranges) < n:
        return None
    window = list(daily_ranges)[-n:]
    return sum(window) / Decimal(n)


def percentile_rank(values: Sequence[Decimal], x: Decimal) -> Decimal:
    """Share of values <= x, in [0, 100]."""
    if not values:
        raise ValueError("percentile_rank of empty history")
    count = sum(1 for v in values if v <= x)
    return Decimal(100 * count) / Decimal(len(values))


def monday_gap_flag(prev_close: Decimal, today_open: Decimal, adr20_price: Decimal) -> bool:
    """§6.8: opened more than 0.5 x ADR20 away from Friday's close."""
    return abs(today_open - prev_close) > Decimal("0.5") * adr20_price


def compute_atr(
    highs: Sequence[Decimal], lows: Sequence[Decimal], closes: Sequence[Decimal], n: int = 14
) -> Decimal | None:
    """Simple-mean ATR over the last n true ranges (Phase-2 ML feature input)."""
    if len(highs) < n + 1 or len(lows) != len(highs) or len(closes) != len(highs):
        return None
    trs = []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    window = trs[-n:]
    return sum(window) / Decimal(n)
