"""M0 firm-profile and startup cross-validation tests (SPEC.md §8)."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.config_schema import (
    FirmProfile,
    InstrumentsConfig,
    StartupValidationError,
    StrategyConfig,
    validate_startup,
)


def test_shipped_firm_profile_is_valid(firm_dict):
    firm = FirmProfile.model_validate(firm_dict)
    assert firm.account_size == Decimal("50000")
    assert firm.trailing_dd.mode == "intraday_equity"
    assert firm.daily_loss.reset_time.hour == 17
    assert firm.daily_loss.reset_zone == "America/New_York"


def test_money_fields_are_decimal(firm_dict):
    firm = FirmProfile.model_validate(firm_dict)
    assert isinstance(firm.profit_target, Decimal)
    assert isinstance(firm.daily_loss.amount, Decimal)


def test_ea_policy_disallowed_refused(firm_dict):
    # SPEC §8a: refuse to run if ea_policy.allowed is false.
    firm_dict["ea_policy"]["allowed"] = False
    with pytest.raises(ValidationError):
        FirmProfile.model_validate(firm_dict)


@pytest.mark.parametrize(
    "missing", ["account_size", "profit_target", "trailing_dd", "daily_loss", "ea_policy"]
)
def test_incomplete_profile_refused(firm_dict, missing):
    del firm_dict[missing]
    with pytest.raises(ValidationError):
        FirmProfile.model_validate(firm_dict)


def test_unknown_trailing_mode_refused(firm_dict):
    firm_dict["trailing_dd"]["mode"] = "weekly_balance"
    with pytest.raises(ValidationError):
        FirmProfile.model_validate(firm_dict)


def test_unmodeled_daily_loss_basis_refused(firm_dict):
    # v1 only models the harsher equity-including-floating basis (DECISIONS.md).
    firm_dict["daily_loss"]["basis"] = "balance_only"
    with pytest.raises(ValidationError):
        FirmProfile.model_validate(firm_dict)


def test_bad_reset_clause_refused(firm_dict):
    firm_dict["daily_loss"]["reset"] = "5pm New York"
    with pytest.raises(ValidationError):
        FirmProfile.model_validate(firm_dict)


def test_consistency_pct_null_allowed(firm_dict):
    firm_dict["consistency_pct"] = None
    firm = FirmProfile.model_validate(firm_dict)
    assert firm.consistency_pct is None


@pytest.mark.parametrize("value", [0, -10, 101])
def test_consistency_pct_out_of_range_refused(firm_dict, value):
    firm_dict["consistency_pct"] = value
    with pytest.raises(ValidationError):
        FirmProfile.model_validate(firm_dict)


def test_trailing_dd_must_be_inside_account(firm_dict):
    firm_dict["trailing_dd"]["amount"] = 50000
    with pytest.raises(ValidationError):
        FirmProfile.model_validate(firm_dict)


def test_unknown_field_refused(firm_dict):
    firm_dict["bonus_rule"] = {}
    with pytest.raises(ValidationError):
        FirmProfile.model_validate(firm_dict)


# ---------------------------------------------------------------------------
# Startup cross-validation: internal buffers strictly inside firm limits (§8a/b)
# ---------------------------------------------------------------------------

def _models(strategy_dict, firm_dict):
    return (
        StrategyConfig.model_validate(strategy_dict),
        FirmProfile.model_validate(firm_dict),
    )


def test_shipped_configs_pass_startup_validation(strategy_dict, firm_dict, instruments_dict):
    strategy, firm = _models(strategy_dict, firm_dict)
    instruments = InstrumentsConfig.model_validate(instruments_dict)
    validate_startup(strategy, firm, instruments)  # must not raise


def test_day_hard_stop_must_be_strictly_inside_firm_daily_limit(strategy_dict, firm_dict):
    firm_dict["daily_loss"]["amount"] = 600  # equal to |day_hard_stop| -> not inside
    strategy, firm = _models(strategy_dict, firm_dict)
    with pytest.raises(StartupValidationError) as exc:
        validate_startup(strategy, firm)
    assert "DAY_HARD_STOP_NOT_INSIDE_FIRM_DAILY_LIMIT" in exc.value.violations


def test_floor_buffer_must_be_inside_trailing_dd(strategy_dict, firm_dict):
    firm_dict["trailing_dd"]["amount"] = 800
    strategy, firm = _models(strategy_dict, firm_dict)
    with pytest.raises(StartupValidationError) as exc:
        validate_startup(strategy, firm)
    assert "FLOOR_BUFFER_NOT_INSIDE_TRAILING_DD" in exc.value.violations


def test_week_stop_must_be_inside_trailing_dd(strategy_dict, firm_dict):
    firm_dict["trailing_dd"]["amount"] = 1200
    strategy, firm = _models(strategy_dict, firm_dict)
    with pytest.raises(StartupValidationError) as exc:
        validate_startup(strategy, firm)
    assert "WEEK_STOP_NOT_INSIDE_TRAILING_DD" in exc.value.violations


def test_consistency_cap_must_be_inside_firm_rule(strategy_dict, firm_dict):
    firm_dict["consistency_pct"] = 20  # 20% of $3000 = $600 < $700 cap
    strategy, firm = _models(strategy_dict, firm_dict)
    with pytest.raises(StartupValidationError) as exc:
        validate_startup(strategy, firm)
    assert "CONSISTENCY_CAP_NOT_INSIDE_FIRM_RULE" in exc.value.violations


def test_consistency_skipped_when_firm_has_no_rule(strategy_dict, firm_dict):
    firm_dict["consistency_pct"] = None
    strategy, firm = _models(strategy_dict, firm_dict)
    validate_startup(strategy, firm)  # must not raise


def test_news_blackout_must_cover_firm_window(strategy_dict, firm_dict):
    firm_dict["news_rule"]["blackout_pre_min"] = 15  # firm stricter than strategy's 10
    strategy, firm = _models(strategy_dict, firm_dict)
    with pytest.raises(StartupValidationError) as exc:
        validate_startup(strategy, firm)
    assert "NEWS_PRE_BLACKOUT_NARROWER_THAN_FIRM" in exc.value.violations


def test_news_post_blackout_must_cover_firm_window(strategy_dict, firm_dict):
    firm_dict["news_rule"]["blackout_post_min"] = 30
    strategy, firm = _models(strategy_dict, firm_dict)
    with pytest.raises(StartupValidationError) as exc:
        validate_startup(strategy, firm)
    assert "NEWS_POST_BLACKOUT_NARROWER_THAN_FIRM" in exc.value.violations


def test_every_pair_needs_an_instrument_spec(strategy_dict, firm_dict, instruments_dict):
    del instruments_dict["instruments"]["GBPUSD"]
    strategy, firm = _models(strategy_dict, firm_dict)
    instruments = InstrumentsConfig.model_validate(instruments_dict)
    with pytest.raises(StartupValidationError) as exc:
        validate_startup(strategy, firm, instruments)
    assert "SYMBOL_MISSING_INSTRUMENT_SPEC:GBPUSD" in exc.value.violations


def test_violations_accumulate(strategy_dict, firm_dict):
    firm_dict["daily_loss"]["amount"] = 500
    firm_dict["consistency_pct"] = 10
    strategy, firm = _models(strategy_dict, firm_dict)
    with pytest.raises(StartupValidationError) as exc:
        validate_startup(strategy, firm)
    assert len(exc.value.violations) >= 2
