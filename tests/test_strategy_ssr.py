"""M2 SSR engine tests on the synthetic fixtures (SPEC.md §3-§5, M2 exit gate).

Required fixture cases: clean long, clean short, deep-sweep skip, no-reclaim
expiry, reclaim-quality fail, stop-out-of-bounds skip (spread/blackout skips are
filter tests; DST week is a NoTradeDay case here).
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.config_schema import StrategyConfig
from core.reasons import ReasonCode
from core.strategy_ssr import (
    AttemptEnded,
    DayInputs,
    ExitSignal,
    NoTradeDay,
    OpenPosition,
    RangeLocked,
    SetupDetected,
    SSREngine,
    manage_on_bar,
)
from core.timebase import session_windows
from tests.fixtures.synthetic import (
    DAY,
    RANGE_HIGH,
    RANGE_LOW,
    asian_session,
    bar,
    clean_long_bars,
    clean_short_bars,
    deep_sweep_bars,
    no_reclaim_bars,
    reclaim_quality_fail_bars,
    stop_oob_bars,
    t,
)

UTC = timezone.utc
PIP = Decimal("0.0001")


@pytest.fixture
def cfg(strategy_dict) -> StrategyConfig:
    return StrategyConfig.model_validate(strategy_dict)


def _engine(cfg, adr20=Decimal(60), violations=(), day=DAY) -> SSREngine:
    day_inputs = DayInputs(
        trading_day=day,
        windows=session_windows(day),
        window_violations=tuple(violations),
        adr20_pips=adr20,
    )
    return SSREngine(
        symbol="EURUSD",
        pair=cfg.pairs["EURUSD"],
        shared=cfg.shared,
        pip=PIP,
        day=day_inputs,
    )


def _run(engine, bars):
    out = []
    for b in bars:
        out.extend(engine.on_bar(b))
    return out


def _only(outputs, kind):
    return [o for o in outputs if isinstance(o, kind)]


# ---------------------------------------------------------------------------
# Range lock
# ---------------------------------------------------------------------------

def test_range_locks_with_correct_levels(cfg):
    out = _run(_engine(cfg), asian_session() + clean_long_bars()[:1])
    locked = _only(out, RangeLocked)
    assert len(locked) == 1
    assert locked[0].range_high == RANGE_HIGH
    assert locked[0].range_low == RANGE_LOW
    assert locked[0].width_pips == Decimal(20)


def test_range_too_narrow_is_no_trade(cfg):
    out = _run(_engine(cfg), asian_session(high=Decimal("1.04580")) + clean_long_bars())
    nt = _only(out, NoTradeDay)
    assert len(nt) == 1
    assert ReasonCode.RANGE_TOO_NARROW in {f.code for f in nt[0].failures}
    assert _only(out, SetupDetected) == []


def test_range_too_wide_is_no_trade(cfg):
    out = _run(_engine(cfg), asian_session(high=Decimal("1.04900")) + clean_long_bars())
    nt = _only(out, NoTradeDay)
    assert len(nt) == 1
    assert ReasonCode.RANGE_TOO_WIDE in {f.code for f in nt[0].failures}


def test_missing_asian_bars_is_no_trade(cfg):
    drops = (t(3, 0), t(3, 5), t(3, 10), t(3, 15))  # 4 missing > 2 allowed
    out = _run(_engine(cfg), asian_session(drop_opens=drops) + clean_long_bars())
    nt = _only(out, NoTradeDay)
    assert len(nt) == 1
    assert ReasonCode.DATA_INCOMPLETE in {f.code for f in nt[0].failures}


def test_dst_anomaly_is_no_trade_before_anything_else(cfg):
    eng = _engine(cfg, violations=("ASIAN_WINDOW_DURATION_MISMATCH:x",))
    out = _run(eng, asian_session() + clean_long_bars())
    nt = _only(out, NoTradeDay)
    assert len(nt) == 1
    assert ReasonCode.DST_ANOMALY in {f.code for f in nt[0].failures}
    assert _only(out, RangeLocked) == []


def test_adr_unavailable_is_no_trade(cfg):
    out = _run(_engine(cfg, adr20=None), asian_session() + clean_long_bars())
    nt = _only(out, NoTradeDay)
    assert len(nt) == 1
    assert ReasonCode.DATA_INCOMPLETE in {f.code for f in nt[0].failures}


# ---------------------------------------------------------------------------
# Clean long / clean short
# ---------------------------------------------------------------------------

def test_clean_long_setup(cfg):
    out = _run(_engine(cfg), asian_session() + clean_long_bars())
    setups = _only(out, SetupDetected)
    assert len(setups) == 1
    c = setups[0].candidate
    assert c.direction == "long"
    assert c.setup_id == "2025-01-15|EURUSD|long"
    assert c.range_high == RANGE_HIGH and c.range_low == RANGE_LOW
    assert c.sweep_extreme == Decimal("1.04460")
    assert c.reclaim_close == Decimal("1.04520")
    assert c.entry_ref == Decimal("1.04520")
    assert c.sl == Decimal("1.04428")          # sweep_low - (2 pips + 0.00012 spread)
    assert c.stop_pips == Decimal("9.2")
    assert c.tp1 == Decimal("1.04612")         # +1.0R
    assert c.tp2 == Decimal("1.04690")         # min(+2.2R, range_high - 1 pip)
    assert c.decision_spread == Decimal("0.00012")


def test_clean_short_setup(cfg):
    out = _run(_engine(cfg), asian_session() + clean_short_bars())
    setups = _only(out, SetupDetected)
    assert len(setups) == 1
    c = setups[0].candidate
    assert c.direction == "short"
    assert c.sweep_extreme == Decimal("1.04740")
    assert c.reclaim_close == Decimal("1.04680")
    assert c.sl == Decimal("1.04772")
    assert c.stop_pips == Decimal("9.2")
    assert c.tp1 == Decimal("1.04588")
    assert c.tp2 == Decimal("1.04510")         # max(-2.2R, range_low + 1 pip)


def test_one_attempt_per_direction_per_day(cfg):
    eng = _engine(cfg)
    out1 = _run(eng, asian_session() + clean_long_bars())
    assert len(_only(out1, SetupDetected)) == 1
    # identical sweep/reclaim sequence later the same day must NOT re-arm
    later = [
        bar(t(8, 5), "1.04540", "1.04560", "1.04470", "1.04485"),
        bar(t(8, 10), "1.04485", "1.04500", "1.04460", "1.04475"),
        bar(t(8, 15), "1.04475", "1.04530", "1.04465", "1.04520"),
    ]
    out2 = _run(eng, later)
    assert _only(out2, SetupDetected) == []


# ---------------------------------------------------------------------------
# Skip fixtures
# ---------------------------------------------------------------------------

def test_deep_sweep_skip(cfg):
    out = _run(_engine(cfg), asian_session() + deep_sweep_bars())
    ended = _only(out, AttemptEnded)
    assert len(ended) == 1
    assert ended[0].direction == "long"
    assert ended[0].code == ReasonCode.SWEEP_TOO_DEEP
    assert _only(out, SetupDetected) == []


def test_no_reclaim_expiry(cfg):
    out = _run(_engine(cfg), asian_session() + no_reclaim_bars())
    ended = _only(out, AttemptEnded)
    assert [e.code for e in ended] == [ReasonCode.NO_RECLAIM]
    assert _only(out, SetupDetected) == []


def test_reclaim_quality_fail(cfg):
    out = _run(_engine(cfg), asian_session() + reclaim_quality_fail_bars())
    ended = _only(out, AttemptEnded)
    assert [e.code for e in ended] == [ReasonCode.RECLAIM_QUALITY]
    assert _only(out, SetupDetected) == []


def test_stop_out_of_bounds_skip(cfg):
    out = _run(_engine(cfg), asian_session() + stop_oob_bars())
    ended = _only(out, AttemptEnded)
    assert [e.code for e in ended] == [ReasonCode.STOP_OOB]
    assert _only(out, SetupDetected) == []


def test_sweep_after_entry_window_ignored(cfg):
    late = [bar(t(11, 0), "1.04540", "1.04560", "1.04470", "1.04520")]
    out = _run(_engine(cfg), asian_session() + late)
    assert _only(out, SetupDetected) == []
    assert _only(out, AttemptEnded) == []


# ---------------------------------------------------------------------------
# Position management (§3.1 tp1/tp2/time/forced; SL-before-TP priority)
# ---------------------------------------------------------------------------

def _pos(**overrides) -> OpenPosition:
    base = dict(
        symbol="EURUSD", direction="long",
        entry_price=Decimal("1.04520"), entry_ts_utc=t(7, 20),
        sl=Decimal("1.04428"), tp1=Decimal("1.04612"), tp2=Decimal("1.04690"),
        lots_total=Decimal("0.10"), lots_open=Decimal("0.10"),
        tp1_done=False, r_price=Decimal("0.00092"),
    )
    base.update(overrides)
    return OpenPosition(**base)


FORCED = datetime(2025, 1, 15, 19, 30, tzinfo=UTC)


def _manage(pos, b):
    return manage_on_bar(
        pos, b, forced_exit_utc=FORCED, time_stop_min=90,
        time_stop_threshold_r=Decimal("0.5"), tp1_fraction=Decimal("0.5"),
    )


def test_tp1_partial_and_breakeven(cfg):
    new_pos, signals = _manage(_pos(), bar(t(7, 25), "1.04530", "1.04620", "1.04525", "1.04600"))
    assert [s.kind for s in signals] == ["tp1"]
    assert signals[0].lots == Decimal("0.05")
    assert new_pos.lots_open == Decimal("0.05")
    assert new_pos.tp1_done is True
    assert new_pos.sl == Decimal("1.04520")  # breakeven


def test_sl_before_tp_same_bar(cfg):
    # bar touches both SL and TP1 -> conservative: whole position dies at SL
    new_pos, signals = _manage(_pos(), bar(t(7, 25), "1.04530", "1.04620", "1.04420", "1.04600"))
    assert [s.kind for s in signals] == ["sl"]
    assert signals[0].lots == Decimal("0.10")
    assert new_pos is None


def test_tp1_and_tp2_same_bar(cfg):
    new_pos, signals = _manage(_pos(), bar(t(7, 25), "1.04530", "1.04700", "1.04525", "1.04680"))
    assert [s.kind for s in signals] == ["tp1", "tp2"]
    assert signals[0].lots == Decimal("0.05")
    assert signals[1].lots == Decimal("0.05")
    assert new_pos is None


def test_breakeven_applies_from_next_bar(cfg):
    pos, _ = _manage(_pos(), bar(t(7, 25), "1.04530", "1.04620", "1.04525", "1.04600"))
    new_pos, signals = _manage(pos, bar(t(7, 30), "1.04560", "1.04565", "1.04500", "1.04510"))
    assert [s.kind for s in signals] == ["sl"]
    assert signals[0].level == Decimal("1.04520")
    assert new_pos is None


def test_time_stop_fires_when_under_half_r(cfg):
    # entry 07:20; bar closing 08:50 is 90 min later; close 1.04550 < entry + 0.5R (1.04566)
    new_pos, signals = _manage(_pos(), bar(t(8, 45), "1.04540", "1.04560", "1.04530", "1.04550"))
    assert [s.kind for s in signals] == ["time"]
    assert new_pos is None


def test_time_stop_skipped_when_above_half_r(cfg):
    new_pos, signals = _manage(_pos(), bar(t(8, 45), "1.04570", "1.04590", "1.04560", "1.04580"))
    assert signals == []
    assert new_pos is not None


def test_forced_exit(cfg):
    new_pos, signals = _manage(_pos(), bar(t(19, 25), "1.04540", "1.04560", "1.04530", "1.04550"))
    assert [s.kind for s in signals] == ["forced"]
    assert new_pos is None


def test_short_sl_before_tp(cfg):
    pos = _pos(direction="short", entry_price=Decimal("1.04680"), sl=Decimal("1.04772"),
               tp1=Decimal("1.04588"), tp2=Decimal("1.04510"))
    new_pos, signals = _manage(pos, bar(t(7, 25), "1.04700", "1.04780", "1.04580", "1.04600"))
    assert [s.kind for s in signals] == ["sl"]
    assert new_pos is None


def test_short_tp1_partial(cfg):
    pos = _pos(direction="short", entry_price=Decimal("1.04680"), sl=Decimal("1.04772"),
               tp1=Decimal("1.04588"), tp2=Decimal("1.04510"))
    new_pos, signals = _manage(pos, bar(t(7, 25), "1.04660", "1.04665", "1.04580", "1.04600"))
    assert [s.kind for s in signals] == ["tp1"]
    assert new_pos.sl == Decimal("1.04680")
    assert new_pos.lots_open == Decimal("0.05")
