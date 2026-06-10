"""Position sizing + margin check (SPEC.md §4.5, §7). Pure Decimal math.

Flooring to lot step means realized risk can only be <= configured risk; caps
(broker max lot, firm max lots) likewise only ever reduce risk.
"""
from __future__ import annotations

from decimal import Decimal

from core.config_schema import InstrumentSpec
from core.reasons import ReasonCode


def pip_value_per_lot(
    contract_size: Decimal, pip: Decimal, quote_to_account: Decimal = Decimal(1)
) -> Decimal:
    """Pip value of one standard lot in account currency. For USD-quoted pairs on
    a USD account the conversion factor is 1 (=> $10/pip/lot); kept generic per §7."""
    return contract_size * pip * quote_to_account


def floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value // step) * step


def size_lots(
    risk_usd: Decimal,
    stop_pips: Decimal,
    pip_value_lot: Decimal,
    spec: InstrumentSpec,
    max_lots_cap: Decimal | None = None,
) -> tuple[Decimal | None, ReasonCode | None]:
    """lots = floor_step(risk / (stop_pips x pip_value)), §4.5. Returns
    (lots, None) or (None, reason). Skips when below broker min lot."""
    if stop_pips <= 0 or pip_value_lot <= 0:
        raise ValueError("stop_pips and pip_value_lot must be positive")
    raw = risk_usd / (stop_pips * pip_value_lot)
    lots = floor_to_step(raw, spec.lot_step)
    if lots < spec.min_lot:
        return None, ReasonCode.SIZE_BELOW_MIN_LOT
    cap = spec.max_lot if max_lots_cap is None else min(spec.max_lot, max_lots_cap)
    return min(lots, cap), None


def margin_required(
    lots: Decimal, contract_size: Decimal, price: Decimal, leverage: Decimal
) -> Decimal:
    return lots * contract_size * price / leverage


def margin_ok(
    equity: Decimal,
    margin_used: Decimal,
    new_margin: Decimal,
    min_free_frac: Decimal = Decimal("0.6"),
) -> bool:
    """§7: the trade must leave >= 60% of equity as free margin."""
    free_after = equity - (margin_used + new_margin)
    return free_after >= min_free_frac * equity
