"""Fill layer: converts the core's ExitSignals into priced fills (SPEC.md §13.3).

The intrabar ordering itself (same-bar SL-before-TP, breakeven-from-next-bar)
is core law in `core.strategy_ssr.manage_on_bar`; this module only prices what
the core decided, so backtest and live can never disagree about WHAT happened —
only the shells' fill prices differ.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Sequence

from backtest.costs import CostModel, exit_fill_price
from core.events import M5Bar
from core.strategy_ssr import ExitSignal


@dataclass(frozen=True, slots=True)
class Fill:
    kind: str
    ts_utc: datetime
    price: Decimal
    lots: Decimal
    pnl_gross: Decimal | None
    slippage_pips: Decimal


def price_exit_signals(
    signals: Sequence[ExitSignal],
    direction: Literal["long", "short"],
    bar: M5Bar,
    contract_size: Decimal,
    pip: Decimal,
    costs: CostModel,
    entry_price: Decimal | None = None,
) -> list[Fill]:
    fills: list[Fill] = []
    for s in signals:
        level = s.level if s.level is not None else bar.bid_c  # time/forced: market at close
        price = exit_fill_price(
            direction, s.kind, level, bar.bid_o, bar.spread_median, pip, costs
        )
        pnl = None
        if entry_price is not None:
            move = price - entry_price if direction == "long" else entry_price - price
            pnl = move * s.lots * contract_size
        fills.append(
            Fill(
                kind=s.kind,
                ts_utc=s.ts_utc,
                price=price,
                lots=s.lots,
                pnl_gross=pnl,
                slippage_pips=abs(price - level) / pip,
            )
        )
    return fills
