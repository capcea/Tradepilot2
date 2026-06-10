"""M5 order-manager tests (SPEC.md §10.5): idempotency (double-send impossible),
partial-fill resize, restart reconcile adopt-or-flatten, rate limiter, retries."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from adapters.sqlite_store import SqliteStore
from ports.execution import OrderResult
from services.order_manager import EntryRequest, OrderManager, RateLimiter
from tests.fakes import FakeBroker, FakeClock, RecordingAlerts

D = Decimal
UTC = timezone.utc
T0 = datetime(2025, 1, 15, 7, 20, tzinfo=UTC)
POINTS = {"EURUSD": D("0.00001"), "GBPUSD": D("0.00001")}


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "om.sqlite", points=POINTS)


@pytest.fixture
def clock():
    return FakeClock(T0)


def _req(**overrides) -> EntryRequest:
    base = dict(
        intent_id="2025-01-15|EURUSD|long", setup_id="2025-01-15|EURUSD|long",
        symbol="EURUSD", side="long", lots=D("1.90"),
        entry_ref=D("1.04520"), sl=D("1.04428"), tp=D("1.04690"),
        max_deviation=D("0.00015"), pip=D("0.0001"),
    )
    base.update(overrides)
    return EntryRequest(**base)


def _manager(broker, store, clock, **kw):
    alerts = RecordingAlerts()
    kw.setdefault("sleep", lambda s: None)
    om = OrderManager(execution=broker, store=store, clock=clock, alerts=alerts, **kw)
    return om, alerts


def test_entry_happy_path_records_intent_and_fill(store, clock):
    broker = FakeBroker()
    broker.queue_place(OrderResult(ok=True, broker_ticket="T1",
                                   fill_price=D("1.04535"), filled_lots=D("1.90")))
    om, _ = _manager(broker, store, clock)
    status, result = om.submit_entry(_req())
    assert status == "filled"
    assert result.broker_ticket == "T1"
    intents = store.get_order_intents(date(2025, 1, 15))
    assert intents[0].status == "filled"
    assert intents[0].broker_ticket == "T1"


def test_double_send_impossible(store, clock):
    broker = FakeBroker()
    om, _ = _manager(broker, store, clock)
    om.submit_entry(_req())
    clock.advance(600)  # well past the rate limit window
    status, result = om.submit_entry(_req())
    assert status == "duplicate"
    assert result is None
    assert len(broker.place_calls) == 1  # the second send never reached the broker


def test_partial_fill_resizes_position(store, clock):
    broker = FakeBroker()
    broker.queue_place(OrderResult(ok=True, broker_ticket="T1",
                                   fill_price=D("1.04535"), filled_lots=D("1.10")))
    om, _ = _manager(broker, store, clock)
    status, result = om.submit_entry(_req(lots=D("1.90")))
    assert status == "filled"
    pos = om.position_from_entry(_req(lots=D("1.90")), result, entry_ts=clock.now_utc())
    assert pos.lots_total == D("1.10")  # SL/TP management sized to ACTUAL fill
    assert pos.lots_open == D("1.10")
    assert pos.sl == D("1.04428")


def test_transient_reject_retries_then_succeeds(store, clock):
    broker = FakeBroker()
    broker.queue_place(
        OrderResult(ok=False, error="requote", retryable=True),
        OrderResult(ok=True, broker_ticket="T2", fill_price=D("1.04536"), filled_lots=D("1.90")),
    )
    om, _ = _manager(broker, store, clock)
    status, result = om.submit_entry(_req())
    assert status == "filled"
    assert len(broker.place_calls) == 2


def test_hard_reject_no_retry(store, clock):
    broker = FakeBroker()
    broker.queue_place(OrderResult(ok=False, error="invalid stops", retryable=False))
    om, _ = _manager(broker, store, clock)
    status, _ = om.submit_entry(_req())
    assert status == "rejected"
    assert len(broker.place_calls) == 1
    assert store.get_order_intents(date(2025, 1, 15))[0].status == "rejected"


def test_three_transient_rejects_abandons_and_alerts(store, clock):
    broker = FakeBroker()
    broker.queue_place(*[OrderResult(ok=False, error="requote", retryable=True)] * 3)
    om, alerts = _manager(broker, store, clock, max_retries=3)
    status, _ = om.submit_entry(_req())
    assert status == "abandoned"
    assert len(broker.place_calls) == 3
    assert store.get_order_intents(date(2025, 1, 15))[0].status == "abandoned"
    assert any("abandon" in m.lower() for _, m in alerts.messages)


# ---------------------------------------------------------------------------
# Rate limiter (§10.5: max 1 entry / 5 min, max 6 order ops / day)
# ---------------------------------------------------------------------------

def test_rate_limiter_blocks_second_entry_within_5min(store, clock):
    broker = FakeBroker()
    om, _ = _manager(broker, store, clock)
    om.submit_entry(_req())
    clock.advance(60)
    status, _ = om.submit_entry(_req(intent_id="2025-01-15|GBPUSD|long",
                                     setup_id="2025-01-15|GBPUSD|long", symbol="GBPUSD"))
    assert status == "rate_limited"
    assert len(broker.place_calls) == 1


def test_rate_limiter_allows_entry_after_5min(store, clock):
    broker = FakeBroker()
    om, _ = _manager(broker, store, clock)
    om.submit_entry(_req())
    clock.advance(301)
    status, _ = om.submit_entry(_req(intent_id="2025-01-15|GBPUSD|long",
                                     setup_id="2025-01-15|GBPUSD|long", symbol="GBPUSD"))
    assert status == "filled"


def test_rate_limiter_daily_op_cap():
    clock = FakeClock(T0)
    rl = RateLimiter(clock)
    for _ in range(6):
        assert rl.allow_op()
        rl.record_op()
    assert not rl.allow_op()
    clock.advance(24 * 3600)
    assert rl.allow_op()  # new day resets


def test_runaway_loop_hits_limiter_not_broker(store, clock):
    broker = FakeBroker()
    om, _ = _manager(broker, store, clock)
    sent = 0
    for i in range(20):  # simulated runaway strategy loop
        status, _ = om.submit_entry(_req(intent_id=f"k{i}", setup_id=f"k{i}"))
        sent += status == "filled"
        clock.advance(301)
    assert sent <= 6  # daily op cap, independent of strategy logic


# ---------------------------------------------------------------------------
# Restart reconcile: adopt-or-flatten (§10.5)
# ---------------------------------------------------------------------------

def test_reconcile_flattens_unknown_position(store, clock):
    broker = FakeBroker()
    broker.add_position(ticket="T9", comment="not-ours", magic=999999)
    om, alerts = _manager(broker, store, clock)
    report = om.reconcile()
    assert "T9" in report.flattened
    assert broker.positions() == []
    assert any("T9" in m for _, m in alerts.messages)


def test_reconcile_keeps_matched_position(store, clock):
    broker = FakeBroker()
    broker.queue_place(OrderResult(ok=True, broker_ticket="T5",
                                   fill_price=D("1.04535"), filled_lots=D("1.90")))
    om, _ = _manager(broker, store, clock)
    om.submit_entry(_req())
    report = om.reconcile()
    assert report.flattened == ()
    assert [p.ticket for p in broker.positions()] == ["T5"]
    assert report.matched == ("T5",)


def test_reconcile_adopt_policy(store, clock):
    broker = FakeBroker()
    broker.add_position(ticket="T7", comment="2025-01-15|EURUSD|long", magic=778001)
    om, _ = _manager(broker, store, clock)
    report = om.reconcile(adopt=True)
    assert report.adopted == ("T7",)
    assert [p.ticket for p in broker.positions()] == ["T7"]
