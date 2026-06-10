"""M3 compliance-engine tests (SPEC.md §7, §8): modeled trailing floor, firm daily
loss distance, and the REQUIRED property: random equity paths never breach the
modeled floor while the guards are on."""
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.compliance import (
    entry_blocked,
    firm_daily_breached,
    firm_daily_loss_used,
    floor_distance,
    initial_compliance,
    on_daily_reset,
    on_day_close,
    on_equity,
)
from core.reasons import ReasonCode

D = Decimal


@pytest.fixture
def firm(firm_dict):
    from core.config_schema import FirmProfile

    return FirmProfile.model_validate(firm_dict)


def test_initial_floor(firm):
    s = initial_compliance(firm, D("50000"))
    assert s.floor == D("47500")
    assert floor_distance(s, D("50000")) == D("2500")


def test_intraday_hwm_ratchets_floor(firm):
    s = initial_compliance(firm, D("50000"))
    s = on_equity(s, D("50500"), firm)
    assert s.floor == D("48000")
    s = on_equity(s, D("50100"), firm)   # equity dips, floor must NOT drop
    assert s.floor == D("48000")
    s = on_equity(s, D("51000"), firm)
    assert s.floor == D("48500")


def test_preset_case_hwm_rise_then_small_loss_no_breach(firm):
    # §8d preset: HWM rises intraday then small loss must not breach modeled floor
    s = initial_compliance(firm, D("50000"))
    s = on_equity(s, D("50800"), firm)
    s = on_equity(s, D("50500"), firm)
    assert floor_distance(s, D("50500")) == D("2200")
    assert D("50500") > s.floor


def test_eod_balance_mode_only_ratchets_on_day_close(firm_dict):
    from core.config_schema import FirmProfile

    firm_dict["trailing_dd"]["mode"] = "eod_balance"
    firm = FirmProfile.model_validate(firm_dict)
    s = initial_compliance(firm, D("50000"))
    s = on_equity(s, D("51000"), firm)       # intraday spike ignored in eod mode
    assert s.floor == D("47500")
    s = on_day_close(s, D("50600"), firm)    # balance at day close ratchets
    assert s.floor == D("48100")


def test_static_mode_never_moves(firm_dict):
    from core.config_schema import FirmProfile

    firm_dict["trailing_dd"]["mode"] = "static"
    firm = FirmProfile.model_validate(firm_dict)
    s = initial_compliance(firm, D("50000"))
    s = on_equity(s, D("53000"), firm)
    s = on_day_close(s, D("53000"), firm)
    assert s.floor == D("47500")


def test_entry_blocked_inside_buffer(firm):
    s = initial_compliance(firm, D("50000"))
    # floor 47500; equity 48200 -> distance 700 < 800 buffer
    assert entry_blocked(s, D("48200"), D("800")) == ReasonCode.FLOOR_BUFFER
    assert entry_blocked(s, D("48400"), D("800")) is None


def test_firm_daily_loss_tracking(firm):
    s = initial_compliance(firm, D("50000"))
    s = on_daily_reset(s, D("50000"))
    assert firm_daily_loss_used(s, D("48900")) == D("1100")
    assert not firm_daily_breached(s, D("48900"), firm)
    assert firm_daily_breached(s, D("48750"), firm)


def test_daily_anchor_resets(firm):
    s = initial_compliance(firm, D("50000"))
    s = on_daily_reset(s, D("50000"))
    s = on_daily_reset(s, D("49400"))
    assert firm_daily_loss_used(s, D("48900")) == D("500")


# ---------------------------------------------------------------------------
# REQUIRED property: random equity paths never breach the modeled trailing
# floor while guards are on (entry gate + per-trade loss bound << buffer).
# ---------------------------------------------------------------------------

# worst single-trade outcome: max risk $250 + worst-case slippage allowance $100
trade_cents = st.integers(min_value=-35000, max_value=50000)


@settings(deadline=None, max_examples=300)
@given(st.lists(trade_cents, max_size=120))
def test_guarded_equity_path_never_breaches_floor(firm_profile_session, pnls):
    firm = firm_profile_session
    buffer = D("800")
    equity = D("50000")
    s = initial_compliance(firm, equity)
    for cents in pnls:
        s = on_equity(s, equity, firm)
        if entry_blocked(s, equity, buffer) is not None:
            continue  # guard refuses the entry; equity cannot move from trading
        equity += D(cents) / 100
        s = on_equity(s, equity, firm)
        assert equity > s.floor, f"floor breached: equity={equity} floor={s.floor}"


@settings(deadline=None, max_examples=300)
@given(st.lists(trade_cents, max_size=120))
def test_floor_is_monotone_nondecreasing(firm_profile_session, pnls):
    firm = firm_profile_session
    equity = D("50000")
    s = initial_compliance(firm, equity)
    prev_floor = s.floor
    for cents in pnls:
        equity += D(cents) / 100
        s = on_equity(s, equity, firm)
        assert s.floor >= prev_floor
        prev_floor = s.floor
