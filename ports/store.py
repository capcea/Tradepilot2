"""StorePort and persistence row types mirroring SPEC.md §11.

Local DB is the source of truth for intents and audit (§10.1); the broker is
the source of truth for positions. All money/price values cross this port as
Decimal; adapters own the storage encoding.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol, Sequence, runtime_checkable

from core.events import EconEvent, M5Bar


@dataclass(frozen=True, slots=True)
class SetupRow:
    id: str
    ts_utc: datetime
    symbol: str
    direction: str  # long|short
    range_high: Decimal
    range_low: Decimal
    sweep_extreme: Decimal
    reclaim_close: Decimal
    features_json: str
    status: str  # detected|vetoed|ordered|expired


@dataclass(frozen=True, slots=True)
class DecisionRow:
    id: str
    setup_id: str | None
    ts_utc: datetime
    stage: str
    passed: bool
    reason_code: str
    details_json: str


@dataclass(frozen=True, slots=True)
class OrderIntentRow:
    id: str
    setup_id: str | None
    ts_utc: datetime
    symbol: str
    side: str
    lots: Decimal
    entry: Decimal
    sl: Decimal
    tp: Decimal
    status: str  # pending|sent|filled|rejected|abandoned|closed
    broker_ticket: str | None
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class FillRow:
    id: str
    intent_id: str
    ts_utc: datetime
    price: Decimal
    lots: Decimal
    slippage_pips: Decimal
    kind: str  # entry|tp1|tp2|sl|time|forced


@dataclass(frozen=True, slots=True)
class EquitySnapshotRow:
    ts_utc: datetime
    balance: Decimal
    equity: Decimal
    hwm: Decimal
    firm_floor: Decimal
    dist_floor: Decimal


@dataclass(frozen=True, slots=True)
class RiskDayRow:
    d: date
    realized: Decimal
    fees: Decimal
    trades: int
    consec_losses: int
    halted: bool
    halt_reason: str | None
    consistency_headroom: Decimal


@dataclass(frozen=True, slots=True)
class ConfigVersionRow:
    id: str
    ts_utc: datetime
    author: str
    yaml: str
    checksum: str
    active: bool


class IdempotencyViolation(Exception):
    """Second insert with an existing idempotency key — double-send blocked at DB layer."""


@runtime_checkable
class StorePort(Protocol):
    # market data
    def upsert_candles(self, bars: Sequence[M5Bar]) -> int: ...
    def get_candles(self, symbol: str, start_utc: datetime, end_utc: datetime) -> Sequence[M5Bar]: ...

    # calendar
    def upsert_econ_events(self, events: Sequence[EconEvent]) -> int: ...
    def get_econ_events(self, start_utc: datetime, end_utc: datetime) -> Sequence[EconEvent]: ...

    # decision trail
    def insert_setup(self, row: SetupRow) -> None: ...
    def update_setup_status(self, setup_id: str, status: str) -> None: ...
    def insert_decision(self, row: DecisionRow) -> None: ...

    # order lifecycle
    def insert_order_intent(self, row: OrderIntentRow) -> None:
        """Raises IdempotencyViolation if the idempotency_key already exists."""
        ...

    def update_order_intent(
        self, intent_id: str, status: str, broker_ticket: str | None = None
    ) -> None: ...
    def get_order_intents(self, day: date) -> Sequence[OrderIntentRow]: ...
    def insert_fill(self, row: FillRow) -> None: ...

    # risk / equity
    def insert_equity_snapshot(self, row: EquitySnapshotRow) -> None: ...
    def upsert_risk_day(self, row: RiskDayRow) -> None: ...
    def get_risk_day(self, d: date) -> RiskDayRow | None: ...

    # governance + audit
    def insert_config_version(self, row: ConfigVersionRow) -> None: ...
    def get_active_config(self) -> ConfigVersionRow | None: ...
    def activate_config(self, config_id: str) -> None: ...
    def append_audit(self, ts_utc: datetime, actor: str, event: str, payload_json: str) -> None: ...
