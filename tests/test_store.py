"""M1 sqlite store tests: §11 schema, idempotency at the DB layer, append-only audit."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import sqlalchemy

from adapters.sqlite_store import SqliteStore
from core.events import EconEvent, M5Bar
from ports.store import (
    ConfigVersionRow,
    DecisionRow,
    EquitySnapshotRow,
    FillRow,
    IdempotencyViolation,
    OrderIntentRow,
    RiskDayRow,
    SetupRow,
)

UTC = timezone.utc
POINTS = {"EURUSD": Decimal("0.00001"), "GBPUSD": Decimal("0.00001")}


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "test.sqlite", points=POINTS)


def _bar(minute: int, symbol: str = "EURUSD") -> M5Bar:
    return M5Bar(
        symbol=symbol,
        ts_open_utc=datetime(2024, 3, 15, 10, minute, tzinfo=UTC),
        bid_o=Decimal("1.04500"), bid_h=Decimal("1.04530"),
        bid_l=Decimal("1.04480"), bid_c=Decimal("1.04510"),
        ask_o=Decimal("1.04512"), ask_h=Decimal("1.04541"),
        ask_l=Decimal("1.04495"), ask_c=Decimal("1.04520"),
        tick_volume=42,
        spread_median=Decimal("0.00012"),
    )


def _intent(key: str = "2024-03-15|EURUSD|long|s1", intent_id: str = "i1") -> OrderIntentRow:
    return OrderIntentRow(
        id=intent_id, setup_id="s1", ts_utc=datetime(2024, 3, 15, 8, 0, tzinfo=UTC),
        symbol="EURUSD", side="long", lots=Decimal("0.12"),
        entry=Decimal("1.04510"), sl=Decimal("1.04390"), tp=Decimal("1.04630"),
        status="pending", broker_ticket=None, idempotency_key=key,
    )


def test_candle_roundtrip_preserves_decimals(store):
    bars = [_bar(0), _bar(5)]
    assert store.upsert_candles(bars) == 2
    out = store.get_candles("EURUSD", datetime(2024, 3, 15, tzinfo=UTC), datetime(2024, 3, 16, tzinfo=UTC))
    assert len(out) == 2
    assert out[0].bid_h == Decimal("1.04530")
    assert isinstance(out[0].bid_h, Decimal)
    assert out[0].spread_median == Decimal("0.00012")
    assert out[0].tick_volume == 42


def test_candle_upsert_is_idempotent(store):
    store.upsert_candles([_bar(0)])
    store.upsert_candles([_bar(0)])
    out = store.get_candles("EURUSD", datetime(2024, 3, 15, tzinfo=UTC), datetime(2024, 3, 16, tzinfo=UTC))
    assert len(out) == 1


def test_candle_window_is_half_open(store):
    store.upsert_candles([_bar(0), _bar(5)])
    out = store.get_candles(
        "EURUSD", datetime(2024, 3, 15, 10, 0, tzinfo=UTC), datetime(2024, 3, 15, 10, 5, tzinfo=UTC)
    )
    assert len(out) == 1


def test_econ_event_upsert_by_id(store):
    e = EconEvent(id="abc", ts_utc=datetime(2026, 6, 5, 12, 30, tzinfo=UTC),
                  currency="USD", impact="high", title="NFP", source="csv")
    assert store.upsert_econ_events([e, e]) >= 1
    store.upsert_econ_events([e])
    out = store.get_econ_events(datetime(2026, 6, 5, tzinfo=UTC), datetime(2026, 6, 6, tzinfo=UTC))
    assert len(out) == 1
    assert out[0].impact == "high"


def test_order_intent_double_send_impossible(store):
    store.insert_order_intent(_intent())
    with pytest.raises(IdempotencyViolation):
        store.insert_order_intent(_intent(intent_id="i2"))  # same key, different id


def test_order_intent_update_and_fetch(store):
    store.insert_order_intent(_intent())
    store.update_order_intent("i1", status="filled", broker_ticket="T100")
    rows = store.get_order_intents(date(2024, 3, 15))
    assert rows[0].status == "filled"
    assert rows[0].broker_ticket == "T100"
    assert rows[0].lots == Decimal("0.12")


def test_setup_decision_fill_rows(store):
    store.insert_setup(SetupRow(
        id="s1", ts_utc=datetime(2024, 3, 15, 7, 40, tzinfo=UTC), symbol="EURUSD",
        direction="long", range_high=Decimal("1.0470"), range_low=Decimal("1.0450"),
        sweep_extreme=Decimal("1.04480"), reclaim_close=Decimal("1.04510"),
        features_json="{}", status="detected",
    ))
    store.update_setup_status("s1", "ordered")
    store.insert_decision(DecisionRow(
        id="d1", setup_id="s1", ts_utc=datetime(2024, 3, 15, 7, 40, tzinfo=UTC),
        stage="filters", passed=False, reason_code="SPREAD_GATE", details_json="{}",
    ))
    store.insert_fill(FillRow(
        id="f1", intent_id="i1", ts_utc=datetime(2024, 3, 15, 8, 0, 5, tzinfo=UTC),
        price=Decimal("1.04513"), lots=Decimal("0.12"),
        slippage_pips=Decimal("0.3"), kind="entry",
    ))
    assert store.get_setup("s1").status == "ordered"
    assert store.get_decisions("s1")[0].reason_code == "SPREAD_GATE"


def test_risk_day_upsert(store):
    row = RiskDayRow(d=date(2024, 3, 15), realized=Decimal("-350"), fees=Decimal("7"),
                     trades=2, consec_losses=2, halted=True, halt_reason="CONSEC_LOSS",
                     consistency_headroom=Decimal("1050"))
    store.upsert_risk_day(row)
    store.upsert_risk_day(RiskDayRow(d=date(2024, 3, 15), realized=Decimal("-100"),
                                     fees=Decimal("7"), trades=3, consec_losses=0,
                                     halted=False, halt_reason=None,
                                     consistency_headroom=Decimal("800")))
    out = store.get_risk_day(date(2024, 3, 15))
    assert out.realized == Decimal("-100")
    assert out.trades == 3
    assert out.halted is False


def test_equity_snapshot_insert(store):
    store.insert_equity_snapshot(EquitySnapshotRow(
        ts_utc=datetime(2024, 3, 15, 8, 0, tzinfo=UTC), balance=Decimal("50000"),
        equity=Decimal("50012.50"), hwm=Decimal("50012.50"),
        firm_floor=Decimal("47512.50"), dist_floor=Decimal("2500"),
    ))


def test_config_version_activation_is_exclusive(store):
    a = ConfigVersionRow(id="c1", ts_utc=datetime(2024, 3, 1, tzinfo=UTC), author="andy",
                         yaml="a: 1", checksum="x" * 64, active=True)
    b = ConfigVersionRow(id="c2", ts_utc=datetime(2024, 3, 2, tzinfo=UTC), author="andy",
                         yaml="a: 2", checksum="y" * 64, active=False)
    store.insert_config_version(a)
    store.insert_config_version(b)
    assert store.get_active_config().id == "c1"
    store.activate_config("c2")
    assert store.get_active_config().id == "c2"


def test_audit_is_append_only(store):
    store.append_audit(datetime(2024, 3, 15, tzinfo=UTC), "system", "boot", "{}")
    with pytest.raises(sqlalchemy.exc.DatabaseError):
        with store.engine.begin() as conn:
            conn.execute(sqlalchemy.text("UPDATE audit SET event='tampered'"))
    with pytest.raises(sqlalchemy.exc.DatabaseError):
        with store.engine.begin() as conn:
            conn.execute(sqlalchemy.text("DELETE FROM audit"))
