"""Order manager (SPEC.md §10.5): idempotent intents, retries with backoff,
rate limiting, restart reconciliation.

Safety properties:
- Every order starts as an order_intent row with a UNIQUE idempotency key
  BEFORE anything is sent — double-send is impossible at the DB layer.
- The rate limiter (max 1 entry / 5 min, max 6 order ops / day) binds ENTRIES
  only; risk-reducing operations (closes, SL tightening) are never blocked —
  a limiter that can prevent flattening would be a hazard, not a safeguard.
- 3 retries with exponential backoff on transient rejects, then abandon+alert.
- On restart, unmatched broker positions are flattened by default (adopt is an
  explicit opt-in), and every action is alerted.
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Callable

from core.strategy_ssr import OpenPosition
from ports.clock import ClockPort
from ports.execution import BracketOrder, ExecutionPort, OrderResult
from ports.store import FillRow, IdempotencyViolation, OrderIntentRow, StorePort


@dataclass(frozen=True, slots=True)
class EntryRequest:
    intent_id: str
    setup_id: str
    symbol: str
    side: str
    lots: Decimal
    entry_ref: Decimal
    sl: Decimal
    tp: Decimal
    max_deviation: Decimal
    pip: Decimal


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    matched: tuple[str, ...]
    flattened: tuple[str, ...]
    adopted: tuple[str, ...]


class RateLimiter:
    """Anti-runaway backstop independent of strategy logic (§10.5, §19 row 14)."""

    def __init__(self, clock: ClockPort, entry_interval_s: int = 300, max_ops_per_day: int = 6):
        self.clock = clock
        self.entry_interval_s = entry_interval_s
        self.max_ops_per_day = max_ops_per_day
        self._last_entry: datetime | None = None
        self._ops = 0
        self._ops_day: date | None = None

    def _roll(self) -> None:
        today = self.clock.now_utc().date()
        if today != self._ops_day:
            self._ops_day = today
            self._ops = 0

    def allow_op(self) -> bool:
        self._roll()
        return self._ops < self.max_ops_per_day

    def allow_entry(self) -> bool:
        self._roll()
        if self._ops >= self.max_ops_per_day:
            return False
        if self._last_entry is None:
            return True
        return (self.clock.now_utc() - self._last_entry).total_seconds() >= self.entry_interval_s

    def record_op(self) -> None:
        self._roll()
        self._ops += 1

    def record_entry(self) -> None:
        self.record_op()
        self._last_entry = self.clock.now_utc()


class OrderManager:
    def __init__(
        self,
        execution: ExecutionPort,
        store: StorePort,
        clock: ClockPort,
        alerts=None,
        magic: int = 778001,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        sleep: Callable[[float], None] = _time.sleep,
        rate_limiter: RateLimiter | None = None,
    ):
        self.execution = execution
        self.store = store
        self.clock = clock
        self.alerts = alerts
        self.magic = magic
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self.sleep = sleep
        self.rate = rate_limiter or RateLimiter(clock)

    # -- entries -----------------------------------------------------------------

    def submit_entry(self, req: EntryRequest) -> tuple[str, OrderResult | None]:
        now = self.clock.now_utc()
        try:
            self.store.insert_order_intent(OrderIntentRow(
                id=req.intent_id, setup_id=req.setup_id, ts_utc=now,
                symbol=req.symbol, side=req.side, lots=req.lots,
                entry=req.entry_ref, sl=req.sl, tp=req.tp,
                status="pending", broker_ticket=None, idempotency_key=req.intent_id,
            ))
        except IdempotencyViolation:
            return "duplicate", None

        if not self.rate.allow_entry():
            self.store.update_order_intent(req.intent_id, "rate_limited")
            self._alert("warning", f"entry rate-limited: {req.intent_id}")
            return "rate_limited", None

        order = BracketOrder(
            intent_id=req.intent_id, symbol=req.symbol, side=req.side,
            lots=req.lots, sl=req.sl, tp=req.tp, max_deviation=req.max_deviation,
            magic=self.magic, comment=req.intent_id,
        )
        last: OrderResult | None = None
        for attempt in range(self.max_retries):
            self.store.update_order_intent(req.intent_id, "sent")
            result = self.execution.place_bracket_market(order)
            last = result
            if result.ok:
                self.rate.record_entry()
                self.store.update_order_intent(
                    req.intent_id, "filled", broker_ticket=result.broker_ticket
                )
                slip = (
                    abs(result.fill_price - req.entry_ref) / req.pip
                    if result.fill_price is not None else Decimal(0)
                )
                self.store.insert_fill(FillRow(
                    id=f"{req.intent_id}|entry", intent_id=req.intent_id,
                    ts_utc=self.clock.now_utc(), price=result.fill_price or req.entry_ref,
                    lots=result.filled_lots or req.lots, slippage_pips=slip, kind="entry",
                ))
                if result.filled_lots is not None and result.filled_lots < req.lots:
                    self._alert(
                        "warning",
                        f"partial fill {result.filled_lots}/{req.lots} on {req.intent_id}; "
                        "management sized to actual fill",
                    )
                return "filled", result
            if not result.retryable:
                self.store.update_order_intent(req.intent_id, "rejected")
                self._alert("error", f"order rejected ({result.error}): {req.intent_id}")
                return "rejected", result
            self.sleep(self.backoff_s * (2 ** attempt))
        self.store.update_order_intent(req.intent_id, "abandoned")
        self._alert("error", f"order abandoned after {self.max_retries} retries: {req.intent_id}")
        return "abandoned", last

    def position_from_entry(
        self, req: EntryRequest, result: OrderResult, entry_ts: datetime,
        tp1: Decimal | None = None, tp2: Decimal | None = None,
    ) -> OpenPosition:
        """Build the managed position from the ACTUAL fill (partial-fill resize)."""
        lots = result.filled_lots if result.filled_lots is not None else req.lots
        entry = result.fill_price if result.fill_price is not None else req.entry_ref
        return OpenPosition(
            symbol=req.symbol, direction=req.side, entry_price=entry,
            entry_ts_utc=entry_ts, sl=req.sl,
            tp1=tp1 if tp1 is not None else req.tp,
            tp2=tp2 if tp2 is not None else req.tp,
            lots_total=lots, lots_open=lots, tp1_done=False,
            r_price=abs(req.entry_ref - req.sl),
        )

    # -- closes / modifies (risk-reducing: never rate-blocked) ----------------------

    def close_position(
        self, ticket: str, lots: Decimal | None, kind: str, intent_id: str
    ) -> tuple[str, OrderResult | None]:
        last: OrderResult | None = None
        for attempt in range(self.max_retries):
            result = self.execution.close_position(ticket, lots)
            last = result
            self.rate.record_op()
            if result.ok:
                self.store.insert_fill(FillRow(
                    id=f"{intent_id}|{kind}|{self.clock.now_utc().isoformat()}",
                    intent_id=intent_id, ts_utc=self.clock.now_utc(),
                    price=result.fill_price or Decimal(0),
                    lots=result.filled_lots or (lots or Decimal(0)),
                    slippage_pips=Decimal(0), kind=kind,
                ))
                return "closed", result
            if not result.retryable:
                break
            self.sleep(self.backoff_s * (2 ** attempt))
        self._alert("critical", f"close FAILED for ticket {ticket} ({kind})")
        return "failed", last

    def modify_sl(self, ticket: str, sl: Decimal, tp: Decimal | None = None) -> OrderResult:
        result = self.execution.modify_position(ticket, sl, tp)
        self.rate.record_op()
        if not result.ok:
            self._alert("error", f"SL/TP modify failed on {ticket}: {result.error}")
        return result

    # -- restart reconciliation (§10.5 adopt-or-flatten) ----------------------------

    def reconcile(self, adopt: bool = False) -> ReconcileReport:
        positions = self.execution.positions()
        intents = self.store.get_order_intents(self.clock.now_utc().date())
        known = {i.broker_ticket for i in intents if i.broker_ticket}
        matched: list[str] = []
        flattened: list[str] = []
        adopted: list[str] = []
        for p in positions:
            if p.ticket in known:
                matched.append(p.ticket)
            elif adopt and p.magic == self.magic:
                adopted.append(p.ticket)
                self._alert("warning", f"adopted broker position {p.ticket} ({p.comment})")
            else:
                self.execution.close_position(p.ticket)
                flattened.append(p.ticket)
                self._alert("error", f"flattened unmatched broker position {p.ticket} ({p.comment})")
        return ReconcileReport(tuple(matched), tuple(flattened), tuple(adopted))

    def _alert(self, level: str, message: str) -> None:
        if self.alerts is not None:
            self.alerts.alert(level, message)
