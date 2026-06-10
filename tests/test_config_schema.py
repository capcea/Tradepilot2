"""M0 strategy-config schema tests (SPEC.md §3.1, §7, Appendix A)."""
from datetime import time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.config_schema import StrategyConfig
from core.timebase import SessionTimesConfig


def test_shipped_strategy_config_is_valid(strategy_dict):
    cfg = StrategyConfig.model_validate(strategy_dict)
    assert cfg.strategy == "ssr_v1"
    assert set(cfg.pairs) == {"EURUSD", "GBPUSD"}


def test_money_fields_are_decimal_not_float(strategy_dict):
    cfg = StrategyConfig.model_validate(strategy_dict)
    assert isinstance(cfg.risk.per_trade_usd, Decimal)
    assert cfg.risk.per_trade_usd == Decimal("175")
    assert isinstance(cfg.pairs["EURUSD"].spread_abs_cap, Decimal)
    assert cfg.pairs["EURUSD"].spread_abs_cap == Decimal("1.2")
    assert cfg.shared.reclaim_quality_pct == Decimal("0.40")


def test_times_parsed(strategy_dict):
    cfg = StrategyConfig.model_validate(strategy_dict)
    assert cfg.shared.forced_exit_london == time(19, 30)
    assert cfg.shared.entry_window_london == (time(7, 5), time(10, 30))
    assert cfg.shared.asian_window_london == (time(0, 0), time(6, 55))


def test_session_times_glue(strategy_dict):
    cfg = StrategyConfig.model_validate(strategy_dict)
    times = cfg.session_times()
    assert isinstance(times, SessionTimesConfig)
    assert times.asian_end == time(6, 55)
    assert times.entry_start == time(7, 5)
    assert times.forced_exit == time(19, 30)


def test_unknown_field_rejected(strategy_dict):
    strategy_dict["surprise"] = 1
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_unknown_pair_field_rejected(strategy_dict):
    strategy_dict["pairs"]["EURUSD"]["lot_size"] = 1
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


@pytest.mark.parametrize("value", [100, 300, 0, -175])
def test_per_trade_risk_outside_spec_bounds_rejected(strategy_dict, value):
    # SPEC §7: 1R bounds are $150-$250.
    strategy_dict["risk"]["per_trade_usd"] = value
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_cooldown_must_not_exceed_per_trade(strategy_dict):
    # SPEC §7: risk never increases after losses.
    strategy_dict["risk"]["cooldown_usd"] = 200
    strategy_dict["risk"]["per_trade_usd"] = 175
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_day_stops_must_be_negative(strategy_dict):
    strategy_dict["risk"]["day_soft_stop"] = 350
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_hard_stop_must_be_beyond_soft_stop(strategy_dict):
    strategy_dict["risk"]["day_soft_stop"] = -350
    strategy_dict["risk"]["day_hard_stop"] = -300
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_week_stop_must_be_beyond_day_hard_stop(strategy_dict):
    strategy_dict["risk"]["week_stop"] = -500
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


@pytest.mark.parametrize("value", [4, 0, -1])
def test_max_entries_day_capped_at_3(strategy_dict, value):
    strategy_dict["risk"]["max_entries_day"] = value
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_max_concurrent_must_be_1_in_v1(strategy_dict):
    strategy_dict["risk"]["max_concurrent"] = 2
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_stop_min_must_be_below_stop_max(strategy_dict):
    strategy_dict["pairs"]["EURUSD"]["stop_min"] = 22
    strategy_dict["pairs"]["EURUSD"]["stop_max"] = 22
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


@pytest.mark.parametrize("value", [0, 1.5, -0.4])
def test_reclaim_quality_pct_must_be_fraction(strategy_dict, value):
    strategy_dict["shared"]["reclaim_quality_pct"] = value
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


@pytest.mark.parametrize("value", [0, 1.5])
def test_tp1_close_must_be_fraction_of_position(strategy_dict, value):
    strategy_dict["shared"]["tp1_close"] = value
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_tp2_cap_must_exceed_tp1(strategy_dict):
    strategy_dict["shared"]["tp2_r_cap"] = 1.0
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_asian_window_must_end_before_entry_window(strategy_dict):
    strategy_dict["shared"]["entry_window_london"] = ["06:30", "10:30"]
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_entry_window_must_end_before_forced_exit(strategy_dict):
    strategy_dict["shared"]["forced_exit_london"] = "10:00"
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_adr_percentiles_must_be_ordered(strategy_dict):
    strategy_dict["shared"]["adr_pctile_skip"] = [90, 25]
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_spread_median_mult_must_be_at_least_1(strategy_dict):
    strategy_dict["shared"]["spread_median_mult"] = 0.5
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_every_pair_needs_news_currencies(strategy_dict):
    del strategy_dict["news"]["currencies"]["GBPUSD"]
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_pairs_must_not_be_empty(strategy_dict):
    strategy_dict["pairs"] = {}
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)


def test_unknown_strategy_name_rejected(strategy_dict):
    strategy_dict["strategy"] = "ssr_v2"
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(strategy_dict)
