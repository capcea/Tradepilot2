"""M3 risk-engine tests (SPEC.md §7 internal rules) incl. hypothesis properties."""
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.config_schema import StrategyConfig
from core.reasons import ReasonCode
from core.risk import (
    entry_halts,
    initial_risk_state,
    latch_hard_stop,
    on_entry_opened,
    on_trade_closed,
    per_trade_risk,
    roll_to_day,
    should_flatten_day,
)

D = Decimal
MON = date(2025, 1, 13)
TUE = date(2025, 1, 14)
WED = date(2025, 1, 15)
NEXT_MON = date(2025, 1, 20)


@pytest.fixture
def rcfg(strategy_dict):
    return StrategyConfig.model_validate(strategy_dict).risk


def test_default_risk_is_175(rcfg):
    s = initial_risk_state(MON)
    assert per_trade_risk(s, rcfg) == D("175")


def test_two_loss_day_triggers_cooldown_next_day(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("-175"))
    s = on_trade_closed(s, D("-175"))
    assert per_trade_risk(s, rcfg) == D("175")  # cooldown starts NEXT day
    s = roll_to_day(s, TUE)
    assert per_trade_risk(s, rcfg) == D("125")


def test_green_day_restores_risk(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("-175"))
    s = on_trade_closed(s, D("-175"))
    s = roll_to_day(s, TUE)
    s = on_trade_closed(s, D("200"))
    s = roll_to_day(s, WED)
    assert per_trade_risk(s, rcfg) == D("175")


def test_flat_day_keeps_cooldown(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("-175"))
    s = on_trade_closed(s, D("-175"))
    s = roll_to_day(s, TUE)          # cooldown on, no trades happen
    s = roll_to_day(s, WED)
    assert per_trade_risk(s, rcfg) == D("125")


def test_consecutive_loss_halt(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("-100"))
    assert ReasonCode.CONSEC_LOSS_HALT not in entry_halts(s, rcfg)
    s = on_trade_closed(s, D("-100"))
    assert ReasonCode.CONSEC_LOSS_HALT in entry_halts(s, rcfg)


def test_win_resets_consecutive_losses(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("-100"))
    s = on_trade_closed(s, D("150"))
    s = on_trade_closed(s, D("-100"))
    assert ReasonCode.CONSEC_LOSS_HALT not in entry_halts(s, rcfg)


def test_max_entries_halt(rcfg):
    s = initial_risk_state(MON)
    for _ in range(3):
        s = on_entry_opened(s)
    assert ReasonCode.MAX_ENTRIES in entry_halts(s, rcfg)


def test_daily_soft_stop_blocks_entries_including_floating(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("-300"))
    assert ReasonCode.DAILY_SOFT_STOP not in entry_halts(s, rcfg)
    halts = entry_halts(s, rcfg, floating_usd=D("-60"))
    assert ReasonCode.DAILY_SOFT_STOP in halts


def test_daily_hard_stop_flattens(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("-550"))
    assert not should_flatten_day(s, rcfg, floating_usd=D("0"))
    assert should_flatten_day(s, rcfg, floating_usd=D("-60"))


def test_hard_stop_latches_even_after_recovery(rcfg):
    s = initial_risk_state(MON)
    s = latch_hard_stop(s)
    s = on_trade_closed(s, D("700"))  # hypothetical recovery
    assert ReasonCode.DAILY_HARD_STOP in entry_halts(s, rcfg)


def test_weekly_stop(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("-600"))
    s = roll_to_day(s, TUE)
    s = on_trade_closed(s, D("-600"))
    assert ReasonCode.WEEKLY_STOP in entry_halts(s, rcfg)
    s = roll_to_day(s, WED)  # same week -> still halted
    assert ReasonCode.WEEKLY_STOP in entry_halts(s, rcfg)
    s = roll_to_day(s, NEXT_MON)  # new week -> reset
    assert ReasonCode.WEEKLY_STOP not in entry_halts(s, rcfg)


def test_consistency_cap(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("700"))
    assert ReasonCode.CONSISTENCY_CAP in entry_halts(s, rcfg)


def test_consistency_cap_counts_floating(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("400"))
    assert ReasonCode.CONSISTENCY_CAP in entry_halts(s, rcfg, floating_usd=D("350"))


def test_day_roll_resets_day_fields(rcfg):
    s = initial_risk_state(MON)
    s = on_trade_closed(s, D("-350"))
    s = on_entry_opened(s)
    s = roll_to_day(s, TUE)
    halts = entry_halts(s, rcfg)
    assert ReasonCode.DAILY_SOFT_STOP not in halts
    assert ReasonCode.MAX_ENTRIES not in halts


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

pnl_cents = st.integers(min_value=-35000, max_value=50000)


@settings(deadline=None, max_examples=200)
@given(st.lists(pnl_cents, max_size=60))
def test_risk_never_exceeds_configured_per_trade(risk_cfg_session, pnls):
    rcfg = risk_cfg_session
    s = initial_risk_state(MON)
    day = MON
    for i, cents in enumerate(pnls):
        if i % 3 == 0 and i:
            day = date.fromordinal(day.toordinal() + 1)
            s = roll_to_day(s, day)
        s = on_trade_closed(s, D(cents) / 100)
        assert per_trade_risk(s, rcfg) <= rcfg.per_trade_usd
        assert per_trade_risk(s, rcfg) >= rcfg.cooldown_usd


@settings(deadline=None, max_examples=200)
@given(st.lists(pnl_cents, min_size=1, max_size=30))
def test_consistency_guard_halts_at_threshold(risk_cfg_session, pnls):
    rcfg = risk_cfg_session
    s = initial_risk_state(MON)
    for cents in pnls:
        s = on_trade_closed(s, D(cents) / 100)
        halted = ReasonCode.CONSISTENCY_CAP in entry_halts(s, rcfg)
        assert halted == (s.day_realized >= rcfg.consistency_day_cap)
