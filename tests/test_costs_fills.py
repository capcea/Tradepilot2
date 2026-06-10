"""M4 cost/fill model tests (SPEC.md §13.3): conservative entry/exit pricing,
same-bar SL-before-TP verified through the fill layer."""
from datetime import datetime, timezone
from decimal import Decimal

from backtest.costs import CostModel, commission_usd, entry_fill_price, exit_fill_price
from backtest.fills import price_exit_signals
from core.strategy_ssr import ExitSignal, OpenPosition
from tests.fixtures.synthetic import bar, t

D = Decimal
PIP = D("0.0001")
COSTS = CostModel()  # defaults: slip_in 0.3p, slip_out 0.4p, $7/lot RT


def test_default_cost_model_matches_spec():
    assert COSTS.slippage_in_pips == D("0.3")
    assert COSTS.slippage_out_pips == D("0.4")
    assert COSTS.commission_per_lot_rt == D("7")


def test_entry_fill_long_pays_spread_plus_slippage():
    # signal close (bid) 1.04520, spread 0.00012 -> ask 1.04532 + 0.3p slip
    fill = entry_fill_price("long", D("1.04520"), D("0.00012"), PIP, COSTS)
    assert fill == D("1.04535")


def test_entry_fill_short_pays_slippage_below_bid():
    fill = entry_fill_price("short", D("1.04680"), D("0.00012"), PIP, COSTS)
    assert fill == D("1.04677")


def test_exit_fill_long_sl_pays_slippage_below_level():
    fill = exit_fill_price("long", "sl", D("1.04428"), D("1.04500"), D("0.00012"), PIP, COSTS)
    assert fill == D("1.04424")


def test_exit_fill_long_sl_gap_open_fills_at_open():
    # bar opens BELOW the stop: fill from the gap open, not the stop level
    fill = exit_fill_price("long", "sl", D("1.04428"), D("1.04380"), D("0.00012"), PIP, COSTS)
    assert fill == D("1.04376")


def test_exit_fill_short_sl_pays_spread_and_slippage():
    # short exit buys at ask = bid level + spread, plus slippage
    fill = exit_fill_price("short", "sl", D("1.04772"), D("1.04700"), D("0.00012"), PIP, COSTS)
    assert fill == D("1.04788")


def test_exit_fill_tp_no_gap_bonus():
    fill = exit_fill_price("long", "tp1", D("1.04612"), D("1.04500"), D("0.00012"), PIP, COSTS)
    assert fill == D("1.04608")


def test_commission():
    assert commission_usd(D("1.90"), COSTS) == D("13.30")


def _pos(**overrides) -> OpenPosition:
    base = dict(
        symbol="EURUSD", direction="long",
        entry_price=D("1.04535"), entry_ts_utc=t(7, 20),
        sl=D("1.04428"), tp1=D("1.04612"), tp2=D("1.04690"),
        lots_total=D("1.90"), lots_open=D("1.90"),
        tp1_done=False, r_price=D("0.00107"),
    )
    base.update(overrides)
    return OpenPosition(**base)


def test_same_bar_sl_before_tp_in_fill_layer():
    """REQUIRED: when one M5 bar touches both SL and TP without tick data,
    the position must die at SL (conservative)."""
    from core.strategy_ssr import manage_on_bar

    both = bar(t(7, 25), "1.04530", "1.04700", "1.04420", "1.04600")
    new_pos, signals = manage_on_bar(
        _pos(), both, forced_exit_utc=t(19, 30), time_stop_min=90,
        time_stop_threshold_r=D("0.5"), tp1_fraction=D("0.5"),
    )
    assert new_pos is None
    fills = price_exit_signals(signals, "long", both, D("100000"), PIP, COSTS)
    assert len(fills) == 1
    assert fills[0].kind == "sl"
    assert fills[0].price == D("1.04424")  # sl 1.04428 - 0.4p slip
    assert fills[0].lots == D("1.90")


def test_priced_exit_pnl_gross():
    sig = [ExitSignal("tp1", D("0.95"), D("1.04612"), t(7, 25))]
    b = bar(t(7, 25), "1.04530", "1.04620", "1.04525", "1.04600")
    fills = price_exit_signals(sig, "long", b, D("100000"), PIP, COSTS, entry_price=D("1.04535"))
    f = fills[0]
    assert f.price == D("1.04608")
    assert f.pnl_gross == D("69.35")  # (1.04608-1.04535) x 0.95 x 100000
