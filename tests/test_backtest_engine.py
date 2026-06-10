"""M4 backtest-engine tests: the SAME pure core driven through backtest adapters,
end-to-end on synthetic days (SPEC.md §13.4 no-look-ahead wiring)."""
from datetime import date, timezone
from decimal import Decimal

import pytest

from adapters.ff_calendar import FileCalendar
from backtest.costs import CostModel
from backtest.engine import BacktestEngine, DictFeed
from core.config_schema import FirmProfile, InstrumentsConfig, StrategyConfig
from core.events import EconEvent
from core.reasons import ReasonCode
from tests.fixtures.synthetic import (
    DAY,
    asian_session,
    bar,
    clean_long_bars,
    t,
)

D = Decimal
UTC = timezone.utc


@pytest.fixture
def engine_parts(strategy_dict, firm_dict, instruments_dict):
    return (
        StrategyConfig.model_validate(strategy_dict),
        FirmProfile.model_validate(firm_dict),
        InstrumentsConfig.model_validate(instruments_dict),
    )


def _engine(parts, bars, calendar_events=(), adr=(D(60), D(50))):
    strategy, firm, instruments = parts
    feed = DictFeed({("EURUSD", DAY): bars})
    return BacktestEngine(
        strategy=strategy,
        firm=firm,
        instruments=instruments,
        feed=feed,
        calendar=FileCalendar(calendar_events),
        costs=CostModel(),
        adr_provider=lambda symbol, day: adr,
    )


def _win_day_bars():
    follow = [
        bar(t(7, 20), "1.04520", "1.04620", "1.04515", "1.04600"),  # TP1 touched
        bar(t(7, 25), "1.04600", "1.04700", "1.04595", "1.04690"),  # TP2 touched
    ]
    return asian_session() + clean_long_bars() + follow


def test_clean_long_full_trade(engine_parts):
    result = _engine(engine_parts, _win_day_bars()).run(["EURUSD"], [DAY])
    assert len(result.trades) == 1
    tr = result.trades[0]
    assert tr.direction == "long"
    assert tr.entry_price == D("1.04535")        # ask close + 0.3p slip
    assert tr.lots == D("1.90")                  # 175 / (9.2p x $10) floored to 0.01
    assert [f.kind for f in tr.exit_fills] == ["tp1", "tp2"]
    assert tr.exit_fills[0].price == D("1.04608")
    assert tr.exit_fills[1].price == D("1.04686")
    assert tr.pnl_usd == D("199.50")             # 69.35 + 143.45 - 13.30 commission
    assert tr.risk_usd == D("174.80")            # 1.90 x 9.2p x $10
    assert result.equity_curve[-1][1] == D("50199.50")


def test_sl_day_loses_about_one_r(engine_parts):
    follow = [bar(t(7, 20), "1.04520", "1.04525", "1.04420", "1.04430")]  # SL 1.04428 hit
    result = _engine(engine_parts, asian_session() + clean_long_bars() + follow).run(["EURUSD"], [DAY])
    assert len(result.trades) == 1
    tr = result.trades[0]
    assert [f.kind for f in tr.exit_fills] == ["sl"]
    assert tr.pnl_usd < D("-174.80")             # slippage + commission push past -1R
    assert tr.r_multiple < D("-1")
    # fixture spread is a fat 1.2 pips on a 9.2-pip stop: entry pays spread+slip
    # (1.5p) + exit slip (0.4p) + commission -> about -1.28R. Real raw-feed
    # spreads (~0.2p) land near -1.05R; the bound just guards against gross error.
    assert tr.r_multiple > D("-1.4")


def test_spread_gate_blocks_at_decision_with_reason_row(engine_parts):
    wide = D("0.00020")  # > abs cap 0.00012
    bars = asian_session() + [
        bar(t(7, 5), "1.04540", "1.04560", "1.04470", "1.04485", spread=wide),
        bar(t(7, 10), "1.04485", "1.04500", "1.04460", "1.04475", spread=wide),
        bar(t(7, 15), "1.04475", "1.04530", "1.04465", "1.04520", spread=wide),
    ]
    result = _engine(engine_parts, bars).run(["EURUSD"], [DAY])
    assert result.trades == []
    codes = {d.reason_code for d in result.decisions if not d.passed}
    assert ReasonCode.SPREAD_GATE.value in codes


def test_news_blackout_blocks_with_reason_row(engine_parts):
    nfp = EconEvent(id="x", ts_utc=t(7, 30), currency="USD", impact="high",
                    title="Event", source="test")
    result = _engine(engine_parts, _win_day_bars(), calendar_events=(nfp,)).run(["EURUSD"], [DAY])
    assert result.trades == []
    codes = {d.reason_code for d in result.decisions if not d.passed}
    assert ReasonCode.NEWS_BLACKOUT.value in codes


def test_no_trade_day_records_decision(engine_parts):
    result = _engine(engine_parts, asian_session(high=D("1.04580")) + clean_long_bars()).run(
        ["EURUSD"], [DAY]
    )
    assert result.trades == []
    codes = {d.reason_code for d in result.decisions}
    assert ReasonCode.RANGE_TOO_NARROW.value in codes


def test_adr_unavailable_is_no_trade(engine_parts):
    result = _engine(engine_parts, _win_day_bars(), adr=(None, None)).run(["EURUSD"], [DAY])
    assert result.trades == []
    codes = {d.reason_code for d in result.decisions}
    assert ReasonCode.DATA_INCOMPLETE.value in codes


def test_equity_curve_is_monotone_in_time(engine_parts):
    result = _engine(engine_parts, _win_day_bars()).run(["EURUSD"], [DAY])
    times = [ts for ts, _ in result.equity_curve]
    assert times == sorted(times)


def test_one_attempt_per_direction_no_second_trade(engine_parts):
    again = [
        bar(t(8, 5), "1.04540", "1.04560", "1.04470", "1.04485"),
        bar(t(8, 10), "1.04485", "1.04500", "1.04460", "1.04475"),
        bar(t(8, 15), "1.04475", "1.04530", "1.04465", "1.04520"),
        bar(t(8, 20), "1.04520", "1.04620", "1.04515", "1.04600"),
        bar(t(8, 25), "1.04600", "1.04700", "1.04595", "1.04690"),
    ]
    result = _engine(engine_parts, _win_day_bars() + again).run(["EURUSD"], [DAY])
    assert len(result.trades) == 1
