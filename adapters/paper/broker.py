"""Paper broker: ExecutionPort + quote-driven bracket emulation (SPEC.md M5).

Paper assumptions (documented, deliberately simple): entries fill at the
current quote with zero extra slippage; bracket exits fill AT the bracket
level once a quote crosses it. The paper-vs-backtest slippage difference is
part of what the §15 slippage audit measures.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Literal

from core.events import TickBatch
from ports.clock import ClockPort
from ports.execution import AccountState, BracketOrder, BrokerPosition, OrderResult

D = Decimal
ZERO = D(0)


@dataclass(frozen=True, slots=True)
class BracketClose:
    ticket: str
    kind: Literal["sl", "tp"]
    price: Decimal
    lots: Decimal


class PaperBroker:
    def __init__(
        self,
        clock: ClockPort,
        starting_balance: Decimal,
        contract_sizes: dict[str, Decimal],
    ):
        self.clock = clock
        self.balance = starting_balance
        self.contract_sizes = contract_sizes
        self._quotes: dict[str, TickBatch] = {}
        self._positions: dict[str, BrokerPosition] = {}
        self._seq = 1

    # -- quotes -----------------------------------------------------------------

    def set_quote(self, symbol: str, bid: Decimal, ask: Decimal) -> None:
        self._quotes[symbol] = TickBatch(
            symbol=symbol, ts_utc=self.clock.now_utc(), bid=bid, ask=ask
        )

    def latest_quote(self, symbol: str) -> TickBatch | None:
        return self._quotes.get(symbol)

    # -- ExecutionPort -------------------------------------------------------------

    def place_bracket_market(self, order: BracketOrder) -> OrderResult:
        q = self._quotes.get(order.symbol)
        if q is None:
            return OrderResult(ok=False, error="no quote", retryable=True)
        fill = q.ask if order.side == "long" else q.bid
        ticket = f"P{self._seq}"
        self._seq += 1
        self._positions[ticket] = BrokerPosition(
            ticket=ticket, symbol=order.symbol, side=order.side, lots=order.lots,
            entry_price=fill, sl=order.sl, tp=order.tp, magic=order.magic,
            comment=order.comment, unrealized_pnl=ZERO,
        )
        return OrderResult(ok=True, broker_ticket=ticket, fill_price=fill,
                           filled_lots=order.lots)

    def modify_position(self, ticket, sl, tp) -> OrderResult:
        p = self._positions.get(ticket)
        if p is None:
            return OrderResult(ok=False, error="unknown ticket", retryable=False)
        self._positions[ticket] = replace(
            p, sl=sl if sl is not None else p.sl, tp=tp if tp is not None else p.tp
        )
        return OrderResult(ok=True, broker_ticket=ticket)

    def close_position(self, ticket, lots=None) -> OrderResult:
        p = self._positions.get(ticket)
        if p is None:
            return OrderResult(ok=False, error="unknown ticket", retryable=False)
        q = self._quotes.get(p.symbol)
        if q is None:
            return OrderResult(ok=False, error="no quote", retryable=True)
        price = q.bid if p.side == "long" else q.ask
        return self._close_at(ticket, price, lots)

    def _close_at(self, ticket: str, price: Decimal, lots: Decimal | None) -> OrderResult:
        p = self._positions[ticket]
        close_lots = p.lots if lots is None or lots >= p.lots else lots
        move = price - p.entry_price if p.side == "long" else p.entry_price - price
        self.balance += move * close_lots * self.contract_sizes[p.symbol]
        if close_lots >= p.lots:
            del self._positions[ticket]
        else:
            self._positions[ticket] = replace(p, lots=p.lots - close_lots)
        return OrderResult(ok=True, broker_ticket=ticket, fill_price=price,
                           filled_lots=close_lots)

    def positions(self) -> list[BrokerPosition]:
        out = []
        for p in self._positions.values():
            q = self._quotes.get(p.symbol)
            upl = ZERO
            if q is not None:
                mark = q.bid if p.side == "long" else q.ask
                move = mark - p.entry_price if p.side == "long" else p.entry_price - mark
                upl = move * p.lots * self.contract_sizes[p.symbol]
            out.append(replace(p, unrealized_pnl=upl))
        return out

    def account(self) -> AccountState:
        floating = sum((p.unrealized_pnl for p in self.positions()), ZERO)
        return AccountState(balance=self.balance, equity=self.balance + floating,
                            margin_free=self.balance)

    # -- bracket emulation ----------------------------------------------------------

    def check_brackets(self) -> list[BracketClose]:
        """Trigger SL/TP exactly as the broker would, off the latest quotes."""
        closed: list[BracketClose] = []
        for ticket, p in list(self._positions.items()):
            q = self._quotes.get(p.symbol)
            if q is None:
                continue
            if p.side == "long":
                if p.sl is not None and q.bid <= p.sl:
                    closed.append(BracketClose(ticket, "sl", p.sl, p.lots))
                    self._close_at(ticket, p.sl, None)
                elif p.tp is not None and q.bid >= p.tp:
                    closed.append(BracketClose(ticket, "tp", p.tp, p.lots))
                    self._close_at(ticket, p.tp, None)
            else:
                if p.sl is not None and q.ask >= p.sl:
                    closed.append(BracketClose(ticket, "sl", p.sl, p.lots))
                    self._close_at(ticket, p.sl, None)
                elif p.tp is not None and q.ask <= p.tp:
                    closed.append(BracketClose(ticket, "tp", p.tp, p.lots))
                    self._close_at(ticket, p.tp, None)
        return closed
