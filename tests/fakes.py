"""Shared fakes for M5 execution-layer tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ports.execution import AccountState, BracketOrder, BrokerPosition, OrderResult

UTC = timezone.utc
D = Decimal


class FakeClock:
    def __init__(self, start: datetime):
        self._now = start

    def now_utc(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


class RecordingAlerts:
    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def alert(self, level: str, message: str) -> None:
        self.messages.append((level, message))


class FakeBroker:
    """Scriptable ExecutionPort. place/close results pop from queues; default ok."""

    def __init__(self, balance: Decimal = D("50000")):
        self.place_results: list[OrderResult] = []
        self.close_results: list[OrderResult] = []
        self.modify_results: list[OrderResult] = []
        self.place_calls: list[BracketOrder] = []
        self.close_calls: list[tuple[str, Decimal | None]] = []
        self.modify_calls: list[tuple[str, Decimal | None, Decimal | None]] = []
        self._positions: list[BrokerPosition] = []
        self.balance = balance
        self._ticket_seq = 100

    # -- scripting helpers ---------------------------------------------------

    def queue_place(self, *results: OrderResult) -> None:
        self.place_results.extend(results)

    def queue_close(self, *results: OrderResult) -> None:
        self.close_results.extend(results)

    def add_position(self, **kw) -> BrokerPosition:
        base = dict(
            ticket=str(self._ticket_seq), symbol="EURUSD", side="long",
            lots=D("1.0"), entry_price=D("1.05"), sl=D("1.04"), tp=D("1.06"),
            magic=778001, comment="2025-01-15|EURUSD|long",
            unrealized_pnl=D("0"),
        )
        base.update(kw)
        self._ticket_seq += 1
        pos = BrokerPosition(**base)
        self._positions.append(pos)
        return pos

    # -- ExecutionPort ---------------------------------------------------------

    def place_bracket_market(self, order: BracketOrder) -> OrderResult:
        self.place_calls.append(order)
        if self.place_results:
            result = self.place_results.pop(0)
        else:
            result = OrderResult(
                ok=True, broker_ticket=str(self._ticket_seq),
                fill_price=order.sl + (order.tp - order.sl) / 4,  # arbitrary in-range price
                filled_lots=order.lots,
            )
        if result.ok:
            self.add_position(
                ticket=result.broker_ticket or str(self._ticket_seq),
                symbol=order.symbol, side=order.side,
                lots=result.filled_lots or order.lots,
                entry_price=result.fill_price or D("1.05"),
                sl=order.sl, tp=order.tp, magic=order.magic, comment=order.comment,
            )
        return result

    def modify_position(self, ticket, sl, tp) -> OrderResult:
        self.modify_calls.append((ticket, sl, tp))
        return self.modify_results.pop(0) if self.modify_results else OrderResult(ok=True, broker_ticket=ticket)

    def close_position(self, ticket, lots=None) -> OrderResult:
        self.close_calls.append((ticket, lots))
        result = self.close_results.pop(0) if self.close_results else OrderResult(
            ok=True, broker_ticket=ticket, fill_price=D("1.05"), filled_lots=lots
        )
        if result.ok:
            self._positions = [
                p if p.ticket != ticket else _shrink(p, lots) for p in self._positions
            ]
            self._positions = [p for p in self._positions if p is not None]
        return result

    def positions(self) -> list[BrokerPosition]:
        return list(self._positions)

    def account(self) -> AccountState:
        return AccountState(balance=self.balance, equity=self.balance, margin_free=self.balance)


def _shrink(p: BrokerPosition, lots: Decimal | None) -> BrokerPosition | None:
    if lots is None or lots >= p.lots:
        return None
    from dataclasses import replace

    return replace(p, lots=p.lots - lots)
