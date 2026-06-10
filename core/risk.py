"""Internal risk engine (SPEC.md §7) — pure, immutable state + transition functions.

Internal rules sit strictly inside the firm's limits (validated at startup):
soft stop blocks entries, hard stop flattens AND latches for the day, weekly
stop halts until the next ISO week, 2 consecutive losses halt the day, 3
entries cap the day, and the +$700 consistency guard blocks further entries.
Per-trade risk is auto-reduced the day after a 2-loss day and restored only
after a green day — risk can never increase after losses (no martingale by
construction).
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from core.config_schema import RiskConfig
from core.reasons import ReasonCode

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class RiskState:
    day: date
    week_anchor: date
    day_realized: Decimal = ZERO
    week_realized: Decimal = ZERO
    trades_opened: int = 0
    consec_losses: int = 0
    losses_today: int = 0
    cooldown_active: bool = False
    hard_latched: bool = False


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def initial_risk_state(day: date) -> RiskState:
    return RiskState(day=day, week_anchor=_monday(day))


def on_entry_opened(s: RiskState) -> RiskState:
    return replace(s, trades_opened=s.trades_opened + 1)


def on_trade_closed(s: RiskState, pnl_usd: Decimal) -> RiskState:
    if pnl_usd < 0:
        consec, losses = s.consec_losses + 1, s.losses_today + 1
    elif pnl_usd > 0:
        consec, losses = 0, s.losses_today
    else:  # scratch: conservative — does not reset the loss streak
        consec, losses = s.consec_losses, s.losses_today
    return replace(
        s,
        day_realized=s.day_realized + pnl_usd,
        week_realized=s.week_realized + pnl_usd,
        consec_losses=consec,
        losses_today=losses,
    )


def roll_to_day(s: RiskState, new_day: date) -> RiskState:
    """Day rollover at the firm's daily reset. Cooldown turns on after a 2-loss
    day and stays on until a green (strictly positive) day."""
    cooldown = s.losses_today >= 2 or (s.cooldown_active and s.day_realized <= 0)
    new_anchor = _monday(new_day)
    week_realized = ZERO if new_anchor != s.week_anchor else s.week_realized
    return RiskState(
        day=new_day,
        week_anchor=new_anchor,
        week_realized=week_realized,
        cooldown_active=cooldown,
    )


def latch_hard_stop(s: RiskState) -> RiskState:
    return replace(s, hard_latched=True)


def per_trade_risk(s: RiskState, cfg: RiskConfig) -> Decimal:
    return cfg.cooldown_usd if s.cooldown_active else cfg.per_trade_usd


def should_flatten_day(s: RiskState, cfg: RiskConfig, floating_usd: Decimal = ZERO) -> bool:
    """§7 daily hard stop on realized + floating."""
    return s.day_realized + floating_usd <= cfg.day_hard_stop


def entry_halts(
    s: RiskState, cfg: RiskConfig, floating_usd: Decimal = ZERO
) -> tuple[ReasonCode, ...]:
    day_pnl = s.day_realized + floating_usd
    halts: list[ReasonCode] = []
    if s.hard_latched or day_pnl <= cfg.day_hard_stop:
        halts.append(ReasonCode.DAILY_HARD_STOP)
    elif day_pnl <= cfg.day_soft_stop:
        halts.append(ReasonCode.DAILY_SOFT_STOP)
    if s.week_realized + floating_usd <= cfg.week_stop:
        halts.append(ReasonCode.WEEKLY_STOP)
    if s.consec_losses >= cfg.consec_loss_halt:
        halts.append(ReasonCode.CONSEC_LOSS_HALT)
    if s.trades_opened >= cfg.max_entries_day:
        halts.append(ReasonCode.MAX_ENTRIES)
    if day_pnl >= cfg.consistency_day_cap:
        halts.append(ReasonCode.CONSISTENCY_CAP)
    return tuple(halts)
