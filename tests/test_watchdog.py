"""M5 watchdog tests (SPEC.md §19 rows 1, 10, 16)."""
from datetime import datetime, timedelta, timezone

from core.filters import OpsHealth
from services.watchdog import Watchdog, WatchdogConfig
from tests.fakes import FakeClock

UTC = timezone.utc
T0 = datetime(2025, 1, 15, 8, 0, tzinfo=UTC)


def _wd(clock):
    return Watchdog(clock, WatchdogConfig(tick_stale_s=10, bar_stale_s=420, clock_skew_s=2))


def test_healthy():
    clock = FakeClock(T0)
    ops = _wd(clock).assess(last_tick_ts=T0 - timedelta(seconds=1),
                            last_bar_close_ts=T0 - timedelta(seconds=60),
                            ntp_offset_s=0.1)
    assert ops == OpsHealth()


def test_stale_tick():
    clock = FakeClock(T0)
    ops = _wd(clock).assess(last_tick_ts=T0 - timedelta(seconds=11),
                            last_bar_close_ts=T0 - timedelta(seconds=60),
                            ntp_offset_s=0.1)
    assert ops.stale_tick


def test_stale_bar_counts_as_stale_tick_gate():
    clock = FakeClock(T0)
    ops = _wd(clock).assess(last_tick_ts=T0 - timedelta(seconds=1),
                            last_bar_close_ts=T0 - timedelta(seconds=500),
                            ntp_offset_s=0.1)
    assert ops.stale_tick


def test_clock_skew():
    clock = FakeClock(T0)
    ops = _wd(clock).assess(last_tick_ts=T0, last_bar_close_ts=T0, ntp_offset_s=3.0)
    assert ops.clock_skew


def test_unknown_ntp_is_not_skew():
    # failed NTP query must not halt trading by itself (alert separately)
    clock = FakeClock(T0)
    ops = _wd(clock).assess(last_tick_ts=T0, last_bar_close_ts=T0, ntp_offset_s=None)
    assert not ops.clock_skew


def test_no_data_yet_is_stale():
    clock = FakeClock(T0)
    ops = _wd(clock).assess(last_tick_ts=None, last_bar_close_ts=None, ntp_offset_s=0.0)
    assert ops.stale_tick


def test_kill_and_pause_passthrough():
    clock = FakeClock(T0)
    ops = _wd(clock).assess(last_tick_ts=T0, last_bar_close_ts=T0, ntp_offset_s=0.0,
                            kill=True, pause=True, reconnecting=True)
    assert ops.kill and ops.pause and ops.reconnecting
