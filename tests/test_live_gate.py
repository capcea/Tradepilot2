"""M5 live-gate tests (build-brief non-negotiable): live orders require
LIVE_TRADING=1 AND ./ARM_LIVE AND config checksum matching the DB active row."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from adapters.sqlite_store import SqliteStore
from ports.store import ConfigVersionRow
from services.live_gate import LiveGate

UTC = timezone.utc
CHECKSUM = "a" * 64
POINTS = {"EURUSD": Decimal("0.00001")}


@pytest.fixture
def store(tmp_path):
    s = SqliteStore(tmp_path / "gate.sqlite", points=POINTS)
    s.insert_config_version(ConfigVersionRow(
        id="c1", ts_utc=datetime(2025, 1, 1, tzinfo=UTC), author="andy",
        yaml="x", checksum=CHECKSUM, active=True,
    ))
    return s


def _gate(store, tmp_path, env, arm: bool):
    arm_file = tmp_path / "ARM_LIVE"
    if arm:
        arm_file.write_text("armed", encoding="utf-8")
    return LiveGate(store=store, expected_checksum=CHECKSUM, arm_file=arm_file, env=env)


def test_all_conditions_met_allows(store, tmp_path):
    g = _gate(store, tmp_path, {"LIVE_TRADING": "1"}, arm=True)
    res = g.check()
    assert res.allowed
    assert res.reasons == ()


def test_env_missing_refuses(store, tmp_path):
    res = _gate(store, tmp_path, {}, arm=True).check()
    assert not res.allowed
    assert any("LIVE_TRADING" in r for r in res.reasons)


def test_env_zero_refuses(store, tmp_path):
    res = _gate(store, tmp_path, {"LIVE_TRADING": "0"}, arm=True).check()
    assert not res.allowed


def test_arm_file_missing_refuses(store, tmp_path):
    res = _gate(store, tmp_path, {"LIVE_TRADING": "1"}, arm=False).check()
    assert not res.allowed
    assert any("ARM_LIVE" in r for r in res.reasons)


def test_checksum_mismatch_refuses(store, tmp_path):
    store.insert_config_version(ConfigVersionRow(
        id="c2", ts_utc=datetime(2025, 1, 2, tzinfo=UTC), author="andy",
        yaml="y", checksum="b" * 64, active=False,
    ))
    store.activate_config("c2")
    res = _gate(store, tmp_path, {"LIVE_TRADING": "1"}, arm=True).check()
    assert not res.allowed
    assert any("checksum" in r for r in res.reasons)


def test_no_active_config_refuses(tmp_path):
    s = SqliteStore(tmp_path / "empty.sqlite", points=POINTS)
    g = LiveGate(store=s, expected_checksum=CHECKSUM, arm_file=tmp_path / "ARM_LIVE",
                 env={"LIVE_TRADING": "1"})
    assert not g.check().allowed


def test_all_three_reasons_reported(store, tmp_path):
    store.activate_config("nonexistent")  # deactivates everything
    res = _gate(store, tmp_path, {}, arm=False).check()
    assert len(res.reasons) == 3
