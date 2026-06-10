"""M2 filter-pipeline tests — the complete §6 no-trade list, each with a reason code."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.events import EconEvent
from core.filters import (
    FilterInputs,
    OpsHealth,
    compute_adr,
    evaluate_no_trade,
    monday_gap_flag,
    percentile_rank,
    spread_gate_ok,
)
from core.reasons import ReasonCode
from core.timebase import session_windows

UTC = timezone.utc
DAY = date(2025, 1, 15)
W = session_windows(DAY)


def _event(hh, mm, currency="USD", impact="high", day=DAY):
    ts = datetime(day.year, day.month, day.day, hh, mm, tzinfo=UTC)
    return EconEvent(id=f"e{hh}{mm}{currency}", ts_utc=ts, currency=currency,
                     impact=impact, title="evt", source="test")


def _inputs(**overrides) -> FilterInputs:
    base = dict(
        ts_utc=datetime(2025, 1, 15, 8, 0, tzinfo=UTC),
        windows=W,
        window_violations=(),
        events=(),
        currencies=("EUR", "USD"),
        news_pre_min=10,
        news_post_min=20,
        news_lookahead_min=30,
        spread=Decimal("0.00010"),
        spread_abs_cap=Decimal("0.00012"),
        spread_median_60m=Decimal("0.00009"),
        spread_median_mult=Decimal("1.8"),
        adr20_pctile=Decimal("50"),
        adr_skip_low=25,
        adr_skip_high=90,
        bank_holiday=False,
        monday_gap=False,
        halt_reasons=(),
        ops=OpsHealth(),
        symbol_valid=True,
        position_open_in_risk_unit=False,
    )
    base.update(overrides)
    return FilterInputs(**base)


def _codes(inputs) -> set[ReasonCode]:
    return {f.code for f in evaluate_no_trade(inputs)}


def test_all_clear_passes():
    assert evaluate_no_trade(_inputs()) == ()


# --- §6.1/6.2 news ----------------------------------------------------------

def test_blackout_pre_window():
    codes = _codes(_inputs(events=(_event(8, 5),)))  # event in 5 min, pre=10
    assert ReasonCode.NEWS_BLACKOUT in codes


def test_blackout_post_window():
    codes = _codes(_inputs(events=(_event(7, 45),)))  # 15 min ago, post=20
    assert ReasonCode.NEWS_BLACKOUT in codes


def test_blackout_expired_event_passes():
    codes = _codes(_inputs(events=(_event(7, 30),)))  # 30 min ago > post
    assert ReasonCode.NEWS_BLACKOUT not in codes


def test_lookahead_block():
    codes = _codes(_inputs(events=(_event(8, 25),)))  # in 25 min <= 30
    assert ReasonCode.NEWS_LOOKAHEAD in codes


def test_far_future_event_passes():
    assert evaluate_no_trade(_inputs(events=(_event(9, 0),))) == ()  # in 60 min


def test_low_impact_and_foreign_currency_ignored():
    events = (_event(8, 5, impact="low"), _event(8, 5, currency="GBP"))
    assert evaluate_no_trade(_inputs(events=events)) == ()


# --- §6.3 spread ------------------------------------------------------------

def test_spread_gate_relative_fail():
    codes = _codes(_inputs(spread=Decimal("0.000170")))  # > 1.8 x 0.00009
    assert ReasonCode.SPREAD_GATE in codes


def test_spread_gate_absolute_fail():
    codes = _codes(_inputs(spread=Decimal("0.00013"), spread_median_60m=Decimal("0.00010")))
    assert ReasonCode.SPREAD_GATE in codes


def test_spread_gate_ok_helper():
    assert spread_gate_ok(Decimal("0.00010"), Decimal("0.00009"),
                          Decimal("1.8"), Decimal("0.00012"))
    assert not spread_gate_ok(Decimal("0.00020"), Decimal("0.00009"),
                              Decimal("1.8"), Decimal("0.00012"))


# --- §6.5 ADR regime --------------------------------------------------------

def test_adr_regime_low():
    assert ReasonCode.ADR_REGIME_LOW in _codes(_inputs(adr20_pctile=Decimal("20")))


def test_adr_regime_high():
    assert ReasonCode.ADR_REGIME_HIGH in _codes(_inputs(adr20_pctile=Decimal("95")))


def test_adr_unavailable_is_conservative_no_trade():
    assert ReasonCode.DATA_INCOMPLETE in _codes(_inputs(adr20_pctile=None))


# --- §6.6 halts passthrough --------------------------------------------------

def test_halt_reasons_propagate():
    codes = _codes(_inputs(halt_reasons=(ReasonCode.DAILY_HARD_STOP,)))
    assert ReasonCode.DAILY_HARD_STOP in codes


# --- §6.7 sessions ----------------------------------------------------------

def test_bank_holiday():
    assert ReasonCode.BANK_HOLIDAY in _codes(_inputs(bank_holiday=True))


def test_rollover_window_blocked():
    codes = _codes(_inputs(ts_utc=datetime(2025, 1, 15, 21, 50, tzinfo=UTC)))
    assert ReasonCode.ROLLOVER_WINDOW in codes


def test_friday_late_blocked():
    fri = date(2025, 1, 17)
    codes = _codes(_inputs(windows=session_windows(fri),
                           ts_utc=datetime(2025, 1, 17, 16, 30, tzinfo=UTC)))
    assert ReasonCode.FRIDAY_LATE in codes


def test_friday_before_cutoff_passes():
    fri = date(2025, 1, 17)
    codes = _codes(_inputs(windows=session_windows(fri),
                           ts_utc=datetime(2025, 1, 17, 9, 0, tzinfo=UTC)))
    assert ReasonCode.FRIDAY_LATE not in codes


def test_outside_entry_window():
    codes = _codes(_inputs(ts_utc=datetime(2025, 1, 15, 11, 0, tzinfo=UTC)))
    assert ReasonCode.OUTSIDE_ENTRY_WINDOW in codes


# --- §6.8 Monday gap, §6.9 DST, §6.10 ops, §6.11/6.12 -------------------------

def test_monday_gap():
    assert ReasonCode.MONDAY_GAP in _codes(_inputs(monday_gap=True))


def test_monday_gap_helper():
    adr = Decimal("0.0060")  # 60 pips
    assert monday_gap_flag(Decimal("1.0500"), Decimal("1.0540"), adr)  # 40 > 30 pips
    assert not monday_gap_flag(Decimal("1.0500"), Decimal("1.0520"), adr)


def test_dst_anomaly():
    codes = _codes(_inputs(window_violations=("ASIAN_WINDOW_DURATION_MISMATCH:x",)))
    assert ReasonCode.DST_ANOMALY in codes


@pytest.mark.parametrize(
    ("ops", "code"),
    [
        (OpsHealth(stale_tick=True), ReasonCode.OPS_UNHEALTHY),
        (OpsHealth(clock_skew=True), ReasonCode.OPS_UNHEALTHY),
        (OpsHealth(reconnecting=True), ReasonCode.OPS_UNHEALTHY),
        (OpsHealth(kill=True), ReasonCode.KILLED),
        (OpsHealth(pause=True), ReasonCode.PAUSED),
    ],
)
def test_ops_health(ops, code):
    assert code in _codes(_inputs(ops=ops))


def test_symbol_invalid():
    assert ReasonCode.SYMBOL_INVALID in _codes(_inputs(symbol_valid=False))


def test_position_open_in_risk_unit():
    assert ReasonCode.POSITION_OPEN in _codes(_inputs(position_open_in_risk_unit=True))


def test_multiple_failures_all_reported():
    codes = _codes(_inputs(bank_holiday=True, monday_gap=True, symbol_valid=False))
    assert {ReasonCode.BANK_HOLIDAY, ReasonCode.MONDAY_GAP, ReasonCode.SYMBOL_INVALID} <= codes


# --- indicator helpers --------------------------------------------------------

def test_compute_adr():
    ranges = [Decimal(60)] * 19 + [Decimal(80)]
    assert compute_adr(ranges, 20) == Decimal(61)
    assert compute_adr([Decimal(60)] * 5, 20) is None  # not enough history


def test_percentile_rank():
    values = [Decimal(i) for i in range(1, 101)]
    assert percentile_rank(values, Decimal(50)) == Decimal(50)
    assert percentile_rank(values, Decimal(100)) == Decimal(100)
    assert percentile_rank(values, Decimal("0.5")) == Decimal(0)
