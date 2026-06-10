"""M3 sizing tests (SPEC.md §4.5, §7 sizing formula + margin check)."""
from decimal import Decimal

from core.config_schema import InstrumentsConfig
from core.reasons import ReasonCode
from core.sizing import floor_to_step, margin_ok, margin_required, pip_value_per_lot, size_lots


def _spec(instruments_dict):
    return InstrumentsConfig.model_validate(instruments_dict).instruments["EURUSD"]


def test_pip_value_usd_quote():
    assert pip_value_per_lot(Decimal("100000"), Decimal("0.0001")) == Decimal("10")


def test_floor_to_step():
    assert floor_to_step(Decimal("1.259"), Decimal("0.01")) == Decimal("1.25")
    assert floor_to_step(Decimal("0.999"), Decimal("0.01")) == Decimal("0.99")
    assert floor_to_step(Decimal("1.25"), Decimal("0.01")) == Decimal("1.25")


def test_size_exact_spec_example(instruments_dict):
    # $175 risk, 14-pip stop, $10/pip/lot -> 1.25 lots exactly
    lots, reason = size_lots(
        risk_usd=Decimal("175"), stop_pips=Decimal("14"),
        pip_value_lot=Decimal("10"), spec=_spec(instruments_dict),
    )
    assert reason is None
    assert lots == Decimal("1.25")


def test_size_floors_to_lot_step(instruments_dict):
    lots, reason = size_lots(
        risk_usd=Decimal("175"), stop_pips=Decimal("13"),
        pip_value_lot=Decimal("10"), spec=_spec(instruments_dict),
    )
    # 175/130 = 1.3461... -> 1.34: flooring means realized risk <= configured risk
    assert reason is None
    assert lots == Decimal("1.34")


def test_size_below_min_lot_skips(instruments_dict):
    lots, reason = size_lots(
        risk_usd=Decimal("1"), stop_pips=Decimal("14"),
        pip_value_lot=Decimal("10"), spec=_spec(instruments_dict),
    )
    assert lots is None
    assert reason == ReasonCode.SIZE_BELOW_MIN_LOT


def test_size_caps_at_max_lot(instruments_dict):
    d = dict(instruments_dict)
    d["instruments"]["EURUSD"]["max_lot"] = 1
    lots, reason = size_lots(
        risk_usd=Decimal("250"), stop_pips=Decimal("7"),
        pip_value_lot=Decimal("10"), spec=_spec(d), max_lots_cap=Decimal("10"),
    )
    assert reason is None
    assert lots == Decimal("1")  # capping down only ever reduces risk


def test_size_caps_at_firm_max_lots(instruments_dict):
    lots, reason = size_lots(
        risk_usd=Decimal("250"), stop_pips=Decimal("7"),
        pip_value_lot=Decimal("10"), spec=_spec(instruments_dict),
        max_lots_cap=Decimal("2"),
    )
    assert reason is None
    assert lots == Decimal("2")


def test_margin_required():
    m = margin_required(lots=Decimal("1.25"), contract_size=Decimal("100000"),
                        price=Decimal("1.05"), leverage=Decimal("100"))
    assert m == Decimal("1312.5")


def test_margin_check_passes_with_headroom():
    assert margin_ok(equity=Decimal("50000"), margin_used=Decimal("0"),
                     new_margin=Decimal("1312.5"), min_free_frac=Decimal("0.6"))


def test_margin_check_fails_when_free_margin_below_60pct():
    assert not margin_ok(equity=Decimal("50000"), margin_used=Decimal("15000"),
                         new_margin=Decimal("10000"), min_free_frac=Decimal("0.6"))
