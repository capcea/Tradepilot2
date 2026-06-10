"""M5 end-to-end paper integration: bars -> SSR core -> order manager -> paper
broker with native brackets -> TP1 partial + breakeven -> TP2 bracket close.
Same core objects as the backtest; only adapters differ (SPEC.md §10.1)."""
from datetime import date, timezone
from decimal import Decimal

import pytest

from adapters.ff_calendar import FileCalendar
from adapters.paper import PaperBroker
from adapters.sqlite_store import SqliteStore
from core.config_schema import FirmProfile, InstrumentsConfig, StrategyConfig
from services.order_manager import OrderManager
from services.runner import PaperSession
from tests.fakes import FakeClock, RecordingAlerts
from tests.fixtures.synthetic import DAY, asian_session, clean_long_bars, t
from core.filters import OpsHealth

D = Decimal
UTC = timezone.utc
POINTS = {"EURUSD": D("0.00001"), "GBPUSD": D("0.00001")}


@pytest.fixture
def session(strategy_dict, firm_dict, instruments_dict, tmp_path):
    strategy = StrategyConfig.model_validate(strategy_dict)
    firm = FirmProfile.model_validate(firm_dict)
    instruments = InstrumentsConfig.model_validate(instruments_dict)
    clock = FakeClock(t(0, 0))
    broker = PaperBroker(clock=clock, starting_balance=D("50000"),
                         contract_sizes={"EURUSD": D("100000")})
    store = SqliteStore(tmp_path / "paper.sqlite", points=POINTS)
    om = OrderManager(execution=broker, store=store, clock=clock,
                      alerts=RecordingAlerts(), sleep=lambda s: None)
    sess = PaperSession(
        symbol="EURUSD", day=DAY, strategy=strategy, firm=firm,
        instruments=instruments, broker=broker, order_manager=om, clock=clock,
        store=store, calendar=FileCalendar(()), adr20_pips=D(60),
        adr20_pctile=D(50), starting_equity=D("50000"),
    )
    return sess, broker, clock, store


def _run_detection(sess, broker, clock):
    for b in asian_session() + clean_long_bars():
        clock._now = b.ts_close_utc
        broker.set_quote("EURUSD", bid=b.bid_c, ask=b.bid_c + b.spread_median)
        sess.process_bar(b)


def test_entry_placed_with_native_bracket(session):
    sess, broker, clock, store = session
    _run_detection(sess, broker, clock)
    positions = broker.positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.side == "long"
    assert p.sl == D("1.04428")
    assert p.tp == D("1.04690")          # TP2 rides natively on the broker
    assert p.comment == "2025-01-15|EURUSD|long"
    intents = store.get_order_intents(date(2025, 1, 15))
    assert intents[0].status == "filled"
    assert sess.open is not None


def test_tp1_partial_moves_sl_to_breakeven(session):
    sess, broker, clock, store = session
    _run_detection(sess, broker, clock)
    entry = sess.open.entry_fill
    broker.set_quote("EURUSD", bid=D("1.04612"), ask=D("1.04624"))
    clock.advance(60)
    sess.poll_quotes()
    positions = broker.positions()
    assert len(positions) == 1
    assert positions[0].lots == D("0.95")      # half of 1.90 closed
    assert positions[0].sl == entry            # breakeven on the broker side
    assert sess.open.pos.tp1_done


def test_tp2_bracket_closes_and_realizes(session):
    sess, broker, clock, store = session
    _run_detection(sess, broker, clock)
    broker.set_quote("EURUSD", bid=D("1.04612"), ask=D("1.04624"))
    clock.advance(60)
    sess.poll_quotes()
    broker.set_quote("EURUSD", bid=D("1.04700"), ask=D("1.04712"))
    clock.advance(60)
    sess.poll_quotes()
    assert broker.positions() == []
    assert sess.open is None
    assert len(sess.closed_trades) == 1
    trade = sess.closed_trades[0]
    assert trade["pnl_usd"] > D("0")
    assert sess.risk.day_realized == trade["pnl_usd"]
    assert store.get_order_intents(date(2025, 1, 15))[0].status == "closed"


def test_forced_exit_at_1930_london(session):
    sess, broker, clock, store = session
    _run_detection(sess, broker, clock)
    clock._now = t(19, 30)
    broker.set_quote("EURUSD", bid=D("1.04540"), ask=D("1.04552"))
    sess.poll_quotes()
    assert broker.positions() == []
    assert sess.open is None
    assert sess.closed_trades[0]["final_kind"] == "forced"


def test_native_sl_bracket_realizes_loss(session):
    sess, broker, clock, store = session
    _run_detection(sess, broker, clock)
    broker.set_quote("EURUSD", bid=D("1.04400"), ask=D("1.04412"))
    clock.advance(60)
    sess.poll_quotes()
    assert broker.positions() == []
    assert sess.closed_trades[0]["pnl_usd"] < D("0")
    assert sess.risk.consec_losses == 1


def test_ops_unhealthy_blocks_entry(strategy_dict, firm_dict, instruments_dict, tmp_path):
    strategy = StrategyConfig.model_validate(strategy_dict)
    firm = FirmProfile.model_validate(firm_dict)
    instruments = InstrumentsConfig.model_validate(instruments_dict)
    clock = FakeClock(t(0, 0))
    broker = PaperBroker(clock=clock, starting_balance=D("50000"),
                         contract_sizes={"EURUSD": D("100000")})
    store = SqliteStore(tmp_path / "p2.sqlite", points=POINTS)
    om = OrderManager(execution=broker, store=store, clock=clock,
                      alerts=RecordingAlerts(), sleep=lambda s: None)
    sess = PaperSession(
        symbol="EURUSD", day=DAY, strategy=strategy, firm=firm,
        instruments=instruments, broker=broker, order_manager=om, clock=clock,
        store=store, calendar=FileCalendar(()), adr20_pips=D(60),
        adr20_pctile=D(50), starting_equity=D("50000"),
        ops=OpsHealth(stale_tick=True),
    )
    _run_detection(sess, broker, clock)
    assert broker.positions() == []
    assert sess.open is None
