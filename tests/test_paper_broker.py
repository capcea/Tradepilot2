"""M5 paper-broker tests: simulated bracket execution off live quotes."""
from datetime import datetime, timezone
from decimal import Decimal

from adapters.paper import PaperBroker
from ports.execution import BracketOrder
from tests.fakes import FakeClock

D = Decimal
UTC = timezone.utc
T0 = datetime(2025, 1, 15, 7, 20, tzinfo=UTC)


def _order(**overrides) -> BracketOrder:
    base = dict(
        intent_id="i1", symbol="EURUSD", side="long", lots=D("1.0"),
        sl=D("1.04428"), tp=D("1.04690"), max_deviation=D("0.00015"),
        magic=778001, comment="i1",
    )
    base.update(overrides)
    return BracketOrder(**base)


def _broker():
    clock = FakeClock(T0)
    broker = PaperBroker(clock=clock, starting_balance=D("50000"),
                         contract_sizes={"EURUSD": D("100000")})
    broker.set_quote("EURUSD", bid=D("1.04520"), ask=D("1.04532"))
    return broker, clock


def test_long_entry_fills_at_ask():
    broker, _ = _broker()
    result = broker.place_bracket_market(_order())
    assert result.ok
    assert result.fill_price == D("1.04532")
    assert result.filled_lots == D("1.0")
    assert len(broker.positions()) == 1


def test_no_quote_rejects_retryable():
    broker, _ = _broker()
    result = broker.place_bracket_market(_order(symbol="GBPUSD"))
    assert not result.ok
    assert result.retryable


def test_close_position_realizes_pnl_at_bid():
    broker, _ = _broker()
    r = broker.place_bracket_market(_order())
    broker.set_quote("EURUSD", bid=D("1.04632"), ask=D("1.04644"))
    out = broker.close_position(r.broker_ticket)
    assert out.ok
    assert out.fill_price == D("1.04632")
    # (1.04632 - 1.04532) x 1.0 x 100000 = $100
    assert broker.account().balance == D("50100")
    assert broker.positions() == []


def test_partial_close():
    broker, _ = _broker()
    r = broker.place_bracket_market(_order())
    broker.set_quote("EURUSD", bid=D("1.04632"), ask=D("1.04644"))
    broker.close_position(r.broker_ticket, lots=D("0.5"))
    assert broker.positions()[0].lots == D("0.5")
    assert broker.account().balance == D("50050")


def test_bracket_sl_triggers_on_quote():
    broker, _ = _broker()
    broker.place_bracket_market(_order())
    broker.set_quote("EURUSD", bid=D("1.04420"), ask=D("1.04432"))  # below SL
    closed = broker.check_brackets()
    assert [c.kind for c in closed] == ["sl"]
    assert broker.positions() == []
    # filled at SL level (paper assumption): (1.04428-1.04532) x 100000 = -$104
    assert broker.account().balance == D("49896")


def test_bracket_tp_triggers_on_quote():
    broker, _ = _broker()
    broker.place_bracket_market(_order())
    broker.set_quote("EURUSD", bid=D("1.04700"), ask=D("1.04712"))
    closed = broker.check_brackets()
    assert [c.kind for c in closed] == ["tp"]
    assert broker.account().balance == D("50158")  # (1.04690-1.04532) x 100000


def test_short_bracket_sl_uses_ask():
    broker, _ = _broker()
    broker.place_bracket_market(_order(side="short", sl=D("1.04772"), tp=D("1.04510")))
    broker.set_quote("EURUSD", bid=D("1.04770"), ask=D("1.04782"))  # ask >= SL
    closed = broker.check_brackets()
    assert [c.kind for c in closed] == ["sl"]


def test_equity_includes_floating():
    broker, _ = _broker()
    broker.place_bracket_market(_order())
    broker.set_quote("EURUSD", bid=D("1.04632"), ask=D("1.04644"))
    acct = broker.account()
    assert acct.balance == D("50000")
    assert acct.equity == D("50100")
