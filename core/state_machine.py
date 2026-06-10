"""Pair-day state machine (SPEC.md §3.2). Pure, immutable."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Phase(str, Enum):
    IDLE = "IDLE"
    RANGE_LOCKED = "RANGE_LOCKED"
    SWEPT_LOW = "SWEPT_LOW"
    SWEPT_HIGH = "SWEPT_HIGH"
    RECLAIMED = "RECLAIMED"
    FILTERS_PASSED = "FILTERS_PASSED"
    ORDER_SENT = "ORDER_SENT"
    MANAGING = "MANAGING"
    FLAT = "FLAT"
    NO_TRADE = "NO_TRADE"
    HALTED = "HALTED"


class InvalidTransition(Exception):
    pass


_ALLOWED: dict[Phase, frozenset[Phase]] = {
    Phase.IDLE: frozenset({Phase.RANGE_LOCKED, Phase.NO_TRADE}),
    Phase.RANGE_LOCKED: frozenset({Phase.SWEPT_LOW, Phase.SWEPT_HIGH, Phase.NO_TRADE}),
    # an attempt that dies (deep sweep, expiry, veto) returns to watching
    Phase.SWEPT_LOW: frozenset({Phase.RECLAIMED, Phase.RANGE_LOCKED}),
    Phase.SWEPT_HIGH: frozenset({Phase.RECLAIMED, Phase.RANGE_LOCKED}),
    Phase.RECLAIMED: frozenset({Phase.FILTERS_PASSED, Phase.RANGE_LOCKED}),
    Phase.FILTERS_PASSED: frozenset({Phase.ORDER_SENT, Phase.RANGE_LOCKED}),
    Phase.ORDER_SENT: frozenset({Phase.MANAGING, Phase.RANGE_LOCKED}),
    Phase.MANAGING: frozenset({Phase.FLAT}),
    # day continues after a flat: next setup or day end
    Phase.FLAT: frozenset({Phase.SWEPT_LOW, Phase.SWEPT_HIGH, Phase.RANGE_LOCKED}),
    Phase.NO_TRADE: frozenset(),
    # manual re-arm only
    Phase.HALTED: frozenset({Phase.IDLE}),
}


@dataclass(frozen=True, slots=True)
class StateMachine:
    phase: Phase = Phase.IDLE

    def to(self, new: Phase) -> "StateMachine":
        if new is Phase.HALTED:  # any state -> HALTED (§3.2)
            return StateMachine(new)
        if new not in _ALLOWED[self.phase]:
            raise InvalidTransition(f"{self.phase.value} -> {new.value} is not allowed")
        return StateMachine(new)
