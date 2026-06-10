"""M5 MT5 adapter tests — pure parts only (request building, symbol validation,
retcode mapping, live-gate refusal) against a fake mt5 API. No terminal needed;
the adapter is import-guarded so this file runs on any platform."""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from adapters.mt5.adapter import (
    MT5Adapter,
    build_order_request,
    map_retcode,
    validate_symbol,
)
from core.config_schema import InstrumentsConfig
from ports.execution import BracketOrder

D = Decimal


class FakeMT5:
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REQUOTE = 10004
    TRADE_RETCODE_PRICE_OFF = 10021
    TRADE_RETCODE_INVALID_STOPS = 10016
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2

    def __init__(self):
        self.sent = []

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=1.04520, ask=1.04532, time_msc=0)

    def order_send(self, request):
        self.sent.append(request)
        return SimpleNamespace(retcode=self.TRADE_RETCODE_DONE, order=42,
                               price=1.04533, volume=request["volume"])


def _spec(instruments_dict):
    return InstrumentsConfig.model_validate(instruments_dict).instruments["EURUSD"]


def _order(**overrides) -> BracketOrder:
    base = dict(
        intent_id="2025-01-15|EURUSD|long", symbol="EURUSD", side="long",
        lots=D("1.90"), sl=D("1.04428"), tp=D("1.04690"),
        max_deviation=D("0.00015"), magic=778001, comment="2025-01-15|EURUSD|long",
    )
    base.update(overrides)
    return BracketOrder(**base)


def test_validate_symbol_passes_matching_info(instruments_dict):
    info = SimpleNamespace(digits=5, point=0.00001, trade_contract_size=100000.0,
                           volume_min=0.01, volume_step=0.01, volume_max=100.0)
    assert validate_symbol(_spec(instruments_dict), info) == ()


@pytest.mark.parametrize(
    ("field", "value", "needle"),
    [("digits", 3, "digits"), ("point", 0.001, "point"),
     ("trade_contract_size", 1000.0, "contract"), ("volume_min", 0.1, "volume_min")],
)
def test_validate_symbol_refuses_mismatch(instruments_dict, field, value, needle):
    info = SimpleNamespace(digits=5, point=0.00001, trade_contract_size=100000.0,
                           volume_min=0.01, volume_step=0.01, volume_max=100.0)
    setattr(info, field, value)
    violations = validate_symbol(_spec(instruments_dict), info)
    assert violations
    assert any(needle in v for v in violations)


def test_build_order_request_long(instruments_dict):
    api = FakeMT5()
    info = SimpleNamespace(filling_mode=FakeMT5.SYMBOL_FILLING_IOC)
    req = build_order_request(
        api, _order(), broker_symbol="EURUSD.pro", price=D("1.04532"),
        point=_spec(instruments_dict).point, symbol_info=info,
    )
    assert req["action"] == api.TRADE_ACTION_DEAL
    assert req["symbol"] == "EURUSD.pro"
    assert req["type"] == api.ORDER_TYPE_BUY
    assert req["volume"] == 1.90
    assert req["sl"] == 1.04428
    assert req["tp"] == 1.04690
    assert req["deviation"] == 15  # 0.00015 / point
    assert req["magic"] == 778001
    assert req["comment"] == "2025-01-15|EURUSD|long"
    assert req["type_filling"] == api.ORDER_FILLING_IOC


def test_build_order_request_short_uses_sell(instruments_dict):
    api = FakeMT5()
    info = SimpleNamespace(filling_mode=FakeMT5.SYMBOL_FILLING_FOK)
    req = build_order_request(
        api, _order(side="short"), broker_symbol="EURUSD", price=D("1.04520"),
        point=_spec(instruments_dict).point, symbol_info=info,
    )
    assert req["type"] == api.ORDER_TYPE_SELL
    assert req["type_filling"] == api.ORDER_FILLING_FOK


def test_map_retcode():
    api = FakeMT5()
    assert map_retcode(api, api.TRADE_RETCODE_DONE) == ("ok", False)
    assert map_retcode(api, api.TRADE_RETCODE_REQUOTE) == ("requote", True)
    assert map_retcode(api, api.TRADE_RETCODE_PRICE_OFF) == ("price_off", True)
    assert map_retcode(api, api.TRADE_RETCODE_INVALID_STOPS) == ("invalid_stops", False)


class DenyGate:
    def check(self):
        from services.live_gate import LiveGateResult

        return LiveGateResult(allowed=False, reasons=("LIVE_TRADING env not set",))


def test_live_gate_refuses_order_before_broker(instruments_dict):
    api = FakeMT5()
    adapter = MT5Adapter(
        api=api,
        symbol_map={"EURUSD": "EURUSD"},
        specs=InstrumentsConfig.model_validate(instruments_dict).instruments,
        magic=778001,
        gate=DenyGate(),
    )
    result = adapter.place_bracket_market(_order())
    assert not result.ok
    assert "live gate" in result.error.lower()
    assert api.sent == []  # nothing ever reached the broker API
