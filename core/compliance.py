"""Prop-firm compliance engine (SPEC.md §7, §8) — pure trailing-floor model.

Modes:
- intraday_equity (default and harshest): HWM and floor ratchet on every equity
  observation; the floor never goes down.
- eod_balance: floor ratchets only on day-close balance.
- static: floor fixed at initial equity minus the drawdown amount.

The startup validator already guarantees the internal floor_buffer sits strictly
inside the firm's trailing drawdown; `entry_blocked` enforces it at runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from core.config_schema import FirmProfile
from core.reasons import ReasonCode

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class ComplianceState:
    hwm: Decimal
    floor: Decimal
    day_anchor: Decimal  # equity at the firm's daily reset (daily-loss basis)


def initial_compliance(firm: FirmProfile, starting_equity: Decimal) -> ComplianceState:
    return ComplianceState(
        hwm=starting_equity,
        floor=starting_equity - firm.trailing_dd.amount,
        day_anchor=starting_equity,
    )


def on_equity(s: ComplianceState, equity: Decimal, firm: FirmProfile) -> ComplianceState:
    if firm.trailing_dd.mode != "intraday_equity":
        return s
    hwm = max(s.hwm, equity)
    floor = max(s.floor, hwm - firm.trailing_dd.amount)
    return replace(s, hwm=hwm, floor=floor)


def on_day_close(s: ComplianceState, balance: Decimal, firm: FirmProfile) -> ComplianceState:
    if firm.trailing_dd.mode != "eod_balance":
        return s
    hwm = max(s.hwm, balance)
    floor = max(s.floor, hwm - firm.trailing_dd.amount)
    return replace(s, hwm=hwm, floor=floor)


def on_daily_reset(s: ComplianceState, equity: Decimal) -> ComplianceState:
    return replace(s, day_anchor=equity)


def floor_distance(s: ComplianceState, equity: Decimal) -> Decimal:
    return equity - s.floor


def entry_blocked(
    s: ComplianceState, equity: Decimal, floor_buffer: Decimal
) -> ReasonCode | None:
    """§7: trading halts when equity - firm_floor < floor_buffer."""
    if floor_distance(s, equity) < floor_buffer:
        return ReasonCode.FLOOR_BUFFER
    return None


def firm_daily_loss_used(s: ComplianceState, equity: Decimal) -> Decimal:
    return max(ZERO, s.day_anchor - equity)


def firm_daily_breached(s: ComplianceState, equity: Decimal, firm: FirmProfile) -> bool:
    return firm_daily_loss_used(s, equity) >= firm.daily_loss.amount
