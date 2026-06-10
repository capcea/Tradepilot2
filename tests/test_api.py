"""M6 API tests (SPEC.md §12): token auth, state/risk/decisions endpoints,
pause/resume/flat-all/kill, config governance (risk reductions only mid-eval)."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import yaml
from fastapi.testclient import TestClient

from adapters.ff_calendar import FileCalendar
from adapters.paper import PaperBroker
from adapters.sqlite_store import SqliteStore
from api.app import ApiContext, create_app
from core.config_schema import FirmProfile, StrategyConfig
from core.events import EconEvent
from ports.store import ConfigVersionRow, DecisionRow, RiskDayRow, SetupRow
from tests.fakes import FakeClock, RecordingAlerts

D = Decimal
UTC = timezone.utc
NOW = datetime(2025, 1, 15, 8, 0, tzinfo=UTC)
TOKEN = "test-token-123"
POINTS = {"EURUSD": D("0.00001"), "GBPUSD": D("0.00001")}


@pytest.fixture
def ctx_client(strategy_dict, firm_dict, tmp_path):
    strategy = StrategyConfig.model_validate(strategy_dict)
    firm = FirmProfile.model_validate(firm_dict)
    clock = FakeClock(NOW)
    store = SqliteStore(tmp_path / "api.sqlite", points=POINTS)
    broker = PaperBroker(clock=clock, starting_balance=D("50000"),
                         contract_sizes={"EURUSD": D("100000")})
    broker.set_quote("EURUSD", bid=D("1.04520"), ask=D("1.04532"))
    store.insert_config_version(ConfigVersionRow(
        id="c1", ts_utc=NOW, author="andy", yaml=yaml.safe_dump(strategy_dict),
        checksum="f" * 64, active=True,
    ))
    events = (EconEvent(id="e1", ts_utc=datetime(2025, 1, 15, 13, 30, tzinfo=UTC),
                        currency="USD", impact="high", title="CPI", source="test"),)
    ctx = ApiContext(
        store=store, execution=broker, calendar=FileCalendar(events), clock=clock,
        strategy=strategy, firm=firm, config_checksum="f" * 64,
        kill_file=tmp_path / "KILL", alerts=RecordingAlerts(),
        status_provider=lambda: {
            "phase": "RANGE_LOCKED", "tick_age_s": 0.4, "bar_age_s": 30.0,
            "ntp_offset_s": 0.05, "broker_connected": True,
            "flattener_heartbeat_age_s": 12.0,
        },
    )
    app = create_app(ctx, token=TOKEN)
    return ctx, TestClient(app), store, broker


def _h(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_auth_required(ctx_client):
    _, client, _, _ = ctx_client
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers=_h("wrong")).status_code == 401
    assert client.get("/health", headers=_h()).status_code == 200


def test_health(ctx_client):
    _, client, _, _ = ctx_client
    body = client.get("/health", headers=_h()).json()
    assert body["broker_connected"] is True
    assert body["tick_age_s"] == 0.4
    assert body["flattener_heartbeat_age_s"] == 12.0


def test_state(ctx_client):
    _, client, _, _ = ctx_client
    body = client.get("/state", headers=_h()).json()
    assert body["phase"] == "RANGE_LOCKED"
    assert body["config_checksum"] == "f" * 64
    assert body["paused"] is False
    assert body["killed"] is False


def test_positions(ctx_client):
    ctx, client, _, broker = ctx_client
    from ports.execution import BracketOrder

    broker.place_bracket_market(BracketOrder(
        intent_id="i1", symbol="EURUSD", side="long", lots=D("1.0"),
        sl=D("1.04428"), tp=D("1.04690"), max_deviation=D("0.0001"),
        magic=778001, comment="i1"))
    body = client.get("/positions", headers=_h()).json()
    assert len(body) == 1
    assert body[0]["symbol"] == "EURUSD"
    assert body[0]["lots"] == "1.0"


def test_risk_endpoint(ctx_client):
    _, client, store, _ = ctx_client
    store.upsert_risk_day(RiskDayRow(
        d=date(2025, 1, 15), realized=D("-150"), fees=D("7"), trades=1,
        consec_losses=1, halted=False, halt_reason=None,
        consistency_headroom=D("850")))
    body = client.get("/risk", headers=_h()).json()
    assert D(body["day_realized"]) == D("-150")  # string-encoded exact decimal
    assert body["trades_today"] == 1
    assert "firm_daily_loss_limit" in body
    assert D(body["internal_day_hard_stop"]) == D("-600")


def test_setups_and_decisions(ctx_client):
    _, client, store, _ = ctx_client
    store.insert_setup(SetupRow(
        id="s1", ts_utc=NOW, symbol="EURUSD", direction="long",
        range_high=D("1.047"), range_low=D("1.045"), sweep_extreme=D("1.0446"),
        reclaim_close=D("1.0452"), features_json="{}", status="vetoed"))
    store.insert_decision(DecisionRow(
        id="d1", setup_id="s1", ts_utc=NOW, stage="filters", passed=False,
        reason_code="SPREAD_GATE", details_json="{}"))
    setups = client.get("/setups", params={"date": "2025-01-15"}, headers=_h()).json()
    assert len(setups) == 1
    decisions = client.get("/decisions", params={"setup_id": "s1"}, headers=_h()).json()
    assert decisions[0]["reason_code"] == "SPREAD_GATE"


def test_calendar_next(ctx_client):
    _, client, _, _ = ctx_client
    body = client.get("/calendar/next", headers=_h()).json()
    assert len(body) == 1
    assert body[0]["title"] == "CPI"


def test_pnl_today(ctx_client):
    _, client, store, _ = ctx_client
    store.upsert_risk_day(RiskDayRow(
        d=date(2025, 1, 15), realized=D("199.50"), fees=D("13.30"), trades=1,
        consec_losses=0, halted=False, halt_reason=None,
        consistency_headroom=D("500.50")))
    body = client.get("/pnl/today", headers=_h()).json()
    assert D(body["realized"]) == D("199.50")


def test_pause_resume(ctx_client):
    ctx, client, _, _ = ctx_client
    assert client.post("/pause", headers=_h()).status_code == 200
    assert ctx.paused is True
    assert client.get("/state", headers=_h()).json()["paused"] is True
    assert client.post("/resume", headers=_h()).status_code == 200
    assert ctx.paused is False


def test_flat_all(ctx_client):
    ctx, client, _, broker = ctx_client
    from ports.execution import BracketOrder

    broker.place_bracket_market(BracketOrder(
        intent_id="i1", symbol="EURUSD", side="long", lots=D("1.0"),
        sl=D("1.04428"), tp=D("1.04690"), max_deviation=D("0.0001"),
        magic=778001, comment="i1"))
    body = client.post("/flat-all", headers=_h()).json()
    assert body["ok"] is True
    assert broker.positions() == []


def test_kill_switch(ctx_client):
    ctx, client, _, broker = ctx_client
    from ports.execution import BracketOrder

    broker.place_bracket_market(BracketOrder(
        intent_id="i1", symbol="EURUSD", side="long", lots=D("1.0"),
        sl=D("1.04428"), tp=D("1.04690"), max_deviation=D("0.0001"),
        magic=778001, comment="i1"))
    body = client.post("/kill", headers=_h()).json()
    assert body["killed"] is True
    assert broker.positions() == []
    assert ctx.kill_file.exists()
    assert ctx.paused is True
    # manual re-arm required: resume refuses while the kill file exists
    assert client.post("/resume", headers=_h()).status_code == 409
    ctx.kill_file.unlink()
    assert client.post("/resume", headers=_h()).status_code == 200


def test_config_risk_reduction_accepted(ctx_client, strategy_dict):
    ctx, client, store, _ = ctx_client
    strategy_dict["risk"]["per_trade_usd"] = 150  # reduction
    resp = client.post("/config", headers=_h(),
                       json={"yaml": yaml.safe_dump(strategy_dict), "author": "andy"})
    assert resp.status_code == 200
    assert store.get_active_config().checksum == resp.json()["checksum"]
    assert ctx.strategy.risk.per_trade_usd == D("150")


def test_config_risk_increase_rejected_mid_eval(ctx_client, strategy_dict):
    _, client, store, _ = ctx_client
    strategy_dict["risk"]["per_trade_usd"] = 250  # increase
    resp = client.post("/config", headers=_h(),
                       json={"yaml": yaml.safe_dump(strategy_dict), "author": "andy"})
    assert resp.status_code == 422
    assert store.get_active_config().id == "c1"  # unchanged


def test_config_invalid_yaml_rejected(ctx_client):
    _, client, _, _ = ctx_client
    resp = client.post("/config", headers=_h(),
                       json={"yaml": "strategy: [unclosed", "author": "andy"})
    assert resp.status_code == 422


def test_config_non_risk_change_rejected_mid_eval(ctx_client, strategy_dict):
    _, client, _, _ = ctx_client
    strategy_dict["shared"]["reclaim_bars"] = 8
    resp = client.post("/config", headers=_h(),
                       json={"yaml": yaml.safe_dump(strategy_dict), "author": "andy"})
    assert resp.status_code == 422


def test_daily_report(ctx_client):
    _, client, store, _ = ctx_client
    store.insert_setup(SetupRow(
        id="s1", ts_utc=NOW, symbol="EURUSD", direction="long",
        range_high=D("1.047"), range_low=D("1.045"), sweep_extreme=D("1.0446"),
        reclaim_close=D("1.0452"), features_json="{}", status="vetoed"))
    store.insert_decision(DecisionRow(
        id="d1", setup_id="s1", ts_utc=NOW, stage="filters", passed=False,
        reason_code="NEWS_BLACKOUT", details_json="{}"))
    body = client.get("/reports/daily", params={"date": "2025-01-15"}, headers=_h()).json()
    assert body["date"] == "2025-01-15"
    assert len(body["setups"]) == 1
    assert len(body["decisions"]) == 1
    assert "NEWS_BLACKOUT" in body["summary"]


def test_logs_endpoint(ctx_client):
    ctx, client, _, _ = ctx_client
    ctx.log("info", "engine started")
    ctx.log("error", "something failed")
    body = client.get("/logs", params={"level": "error"}, headers=_h()).json()
    assert len(body) == 1
    assert body[0]["message"] == "something failed"


def test_dashboard_served(ctx_client):
    _, client, _, _ = ctx_client
    resp = client.get("/")
    assert resp.status_code == 200
    assert "TradePilot" in resp.text
