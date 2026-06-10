"""M0 instrument-spec tests (SPEC.md §10.4, §11)."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.config_schema import InstrumentsConfig


def test_shipped_instruments_valid(instruments_dict):
    cfg = InstrumentsConfig.model_validate(instruments_dict)
    eu = cfg.instruments["EURUSD"]
    assert eu.digits == 5
    assert eu.point == Decimal("0.00001")
    assert eu.pip == Decimal("0.0001")
    assert eu.contract_size == Decimal("100000")
    assert eu.broker_symbols[0] == "EURUSD"


def test_price_fields_are_decimal(instruments_dict):
    cfg = InstrumentsConfig.model_validate(instruments_dict)
    eu = cfg.instruments["EURUSD"]
    assert isinstance(eu.point, Decimal)
    assert isinstance(eu.pip, Decimal)
    assert isinstance(eu.min_lot, Decimal)


def test_point_must_match_digits(instruments_dict):
    instruments_dict["instruments"]["EURUSD"]["point"] = 0.0001  # 4-digit point, digits=5
    with pytest.raises(ValidationError):
        InstrumentsConfig.model_validate(instruments_dict)


def test_pip_must_be_ten_points(instruments_dict):
    instruments_dict["instruments"]["EURUSD"]["pip"] = 0.001
    with pytest.raises(ValidationError):
        InstrumentsConfig.model_validate(instruments_dict)


def test_min_lot_cannot_exceed_max_lot(instruments_dict):
    instruments_dict["instruments"]["EURUSD"]["min_lot"] = 200
    with pytest.raises(ValidationError):
        InstrumentsConfig.model_validate(instruments_dict)


def test_lot_step_must_be_positive(instruments_dict):
    instruments_dict["instruments"]["EURUSD"]["lot_step"] = 0
    with pytest.raises(ValidationError):
        InstrumentsConfig.model_validate(instruments_dict)


def test_min_lot_must_align_to_lot_step(instruments_dict):
    instruments_dict["instruments"]["EURUSD"]["min_lot"] = 0.015
    with pytest.raises(ValidationError):
        InstrumentsConfig.model_validate(instruments_dict)


def test_broker_symbols_must_not_be_empty(instruments_dict):
    instruments_dict["instruments"]["EURUSD"]["broker_symbols"] = []
    with pytest.raises(ValidationError):
        InstrumentsConfig.model_validate(instruments_dict)


def test_unknown_field_rejected(instruments_dict):
    instruments_dict["instruments"]["EURUSD"]["swap_long"] = -2.5
    with pytest.raises(ValidationError):
        InstrumentsConfig.model_validate(instruments_dict)
