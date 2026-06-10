"""ExecutionPort and broker-facing value types (SPEC.md §10.3, §10.5).

The order manager (M5) is the only caller; the strategy core emits intents and
never touches this port directly. Adapters: MT5 (live/paper-gated), paper
simulator, backtest fills, fake broker for tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol, Sequence, runtime_checkable

Side = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class BracketOrder:
    """Market order with native SL/TP (bracket), idempotent via intent_id."""

    intent_id: str
    symbol: str  # canonical, e.g. EURUSD; adapters map to broker variant
    side: Side
    lots: Decimal
    sl: Decimal
    tp: Decimal
    max_deviation: Decimal  # price units
    magic: int
    comment: str


@dataclass(frozen=True, slots=True)
class OrderResult:
    ok: bool
    broker_ticket: str | None = None
    fill_price: Decimal | None = None
    filled_lots: Decimal | None = None
    error: str | None = None
    retryable: bool = False


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    ticket: str
    symbol: str
    side: Side
    lots: Decimal
    entry_price: Decimal
    sl: Decimal | None
    tp: Decimal | None
    magic: int
    comment: str
    unrealized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class AccountState:
    balance: Decimal
    equity: Decimal
    margin_free: Decimal
    currency: str = "USD"


@runtime_checkable
class ExecutionPort(Protocol):
    def place_bracket_market(self, order: BracketOrder) -> OrderResult: ...

    def modify_position(
        self, ticket: str, sl: Decimal | None, tp: Decimal | None
    ) -> OrderResult: ...

    def close_position(self, ticket: str, lots: Decimal | None = None) -> OrderResult:
        """Close fully, or partially when lots is given."""
        ...

    def positions(self) -> Sequence[BrokerPosition]: ...

    def account(self) -> AccountState: ...
