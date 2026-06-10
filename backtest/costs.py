"""Conservative cost model (SPEC.md §13.3).

Entries buy the ask (bid + observed per-bar spread) and pay slippage_in; exits
pay slippage_out on EVERY exit kind, including limit-style TPs (conservative).
Stops gapped over fill from the bar open, never from the stop level. Commission
is $/lot round-turn, charged on the closed quantity.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class CostModel:
    slippage_in_pips: Decimal = Decimal("0.3")
    slippage_out_pips: Decimal = Decimal("0.4")
    commission_per_lot_rt: Decimal = Decimal("7")


def entry_fill_price(
    direction: Literal["long", "short"],
    signal_bid_close: Decimal,
    spread: Decimal,
    pip: Decimal,
    costs: CostModel,
) -> Decimal:
    slip = costs.slippage_in_pips * pip
    if direction == "long":
        return signal_bid_close + spread + slip  # buy the ask, slip against us
    return signal_bid_close - slip  # sell the bid, slip against us


def exit_fill_price(
    direction: Literal["long", "short"],
    kind: str,
    level_bid: Decimal,
    bar_open_bid: Decimal,
    spread: Decimal,
    pip: Decimal,
    costs: CostModel,
) -> Decimal:
    slip = costs.slippage_out_pips * pip
    if direction == "long":
        # long exits sell at bid
        eff = min(level_bid, bar_open_bid) if kind == "sl" else level_bid
        return eff - slip
    # short exits buy at ask = bid level + spread
    eff = max(level_bid, bar_open_bid) if kind == "sl" else level_bid
    return eff + spread + slip


def commission_usd(lots: Decimal, costs: CostModel) -> Decimal:
    return lots * costs.commission_per_lot_rt
