"""M1 bar-builder tests: ticks -> M5 bid/ask bars; M1 candles -> M5 (SPEC.md §13.1)."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.events import M1Candle, Tick
from services.bar_builder import find_missing_m5, floor_to_m5, m5_from_m1, m5_from_ticks

UTC = timezone.utc


def _t(hh, mm, ss, bid, ask):
    return Tick(
        symbol="EURUSD",
        ts_utc=datetime(2024, 3, 15, hh, mm, ss, tzinfo=UTC),
        bid=Decimal(bid),
        ask=Decimal(ask),
    )


def _c(side, hh, mm, o, h, l, c, vol=1.0):
    return M1Candle(
        symbol="EURUSD",
        side=side,
        ts_open_utc=datetime(2024, 3, 15, hh, mm, tzinfo=UTC),
        o=Decimal(o),
        h=Decimal(h),
        l=Decimal(l),
        c=Decimal(c),
        volume=vol,
    )


def test_floor_to_m5():
    assert floor_to_m5(datetime(2024, 3, 15, 10, 7, 33, tzinfo=UTC)) == datetime(
        2024, 3, 15, 10, 5, tzinfo=UTC
    )
    assert floor_to_m5(datetime(2024, 3, 15, 10, 5, 0, tzinfo=UTC)) == datetime(
        2024, 3, 15, 10, 5, tzinfo=UTC
    )


def test_m5_from_ticks_ohlc_both_sides():
    ticks = [
        _t(10, 0, 1, "1.04500", "1.04512"),   # open
        _t(10, 2, 0, "1.04530", "1.04541"),   # high (bid)
        _t(10, 3, 0, "1.04480", "1.04495"),   # low (bid)
        _t(10, 4, 59, "1.04510", "1.04520"),  # close
    ]
    bars = m5_from_ticks(ticks)
    assert len(bars) == 1
    b = bars[0]
    assert b.ts_open_utc == datetime(2024, 3, 15, 10, 0, tzinfo=UTC)
    assert (b.bid_o, b.bid_h, b.bid_l, b.bid_c) == (
        Decimal("1.04500"), Decimal("1.04530"), Decimal("1.04480"), Decimal("1.04510")
    )
    assert (b.ask_o, b.ask_h, b.ask_l, b.ask_c) == (
        Decimal("1.04512"), Decimal("1.04541"), Decimal("1.04495"), Decimal("1.04520")
    )
    assert b.tick_volume == 4


def test_m5_from_ticks_spread_median():
    ticks = [
        _t(10, 0, 1, "1.00000", "1.00010"),  # 10 points
        _t(10, 1, 0, "1.00000", "1.00012"),  # 12
        _t(10, 2, 0, "1.00000", "1.00020"),  # 20
    ]
    b = m5_from_ticks(ticks)[0]
    assert b.spread_median == Decimal("0.00012")


def test_m5_from_ticks_splits_buckets_and_sorts_input():
    ticks = [
        _t(10, 6, 0, "1.1", "1.2"),
        _t(10, 1, 0, "1.0", "1.1"),  # out of order on purpose
        _t(10, 9, 59, "1.3", "1.4"),
    ]
    bars = m5_from_ticks(ticks)
    assert [b.ts_open_utc.minute for b in bars] == [0, 5]
    assert bars[1].bid_o == Decimal("1.1")
    assert bars[1].bid_c == Decimal("1.3")


def test_m5_from_ticks_empty():
    assert m5_from_ticks([]) == []


def test_m5_from_m1_aggregation():
    bid = [
        _c("bid", 10, 0, "1.0000", "1.0010", "0.9990", "1.0005"),
        _c("bid", 10, 1, "1.0005", "1.0030", "1.0000", "1.0020"),
        _c("bid", 10, 4, "1.0020", "1.0025", "0.9980", "1.0001"),
    ]
    ask = [
        _c("ask", 10, 0, "1.0001", "1.0012", "0.9991", "1.0006"),
        _c("ask", 10, 1, "1.0006", "1.0031", "1.0001", "1.0021"),
        _c("ask", 10, 4, "1.0021", "1.0026", "0.9981", "1.0002"),
    ]
    bars = m5_from_m1(bid, ask)
    assert len(bars) == 1
    b = bars[0]
    assert b.bid_o == Decimal("1.0000")
    assert b.bid_h == Decimal("1.0030")
    assert b.bid_l == Decimal("0.9980")
    assert b.bid_c == Decimal("1.0001")
    assert b.ask_c == Decimal("1.0002")
    # per-minute open spreads are all 0.0001 -> median 0.0001
    assert b.spread_median == Decimal("0.0001")
    assert b.tick_volume == 3


def test_m5_from_m1_skips_minutes_missing_on_either_side():
    bid = [
        _c("bid", 10, 0, "1.0", "1.0", "1.0", "1.0"),
        _c("bid", 10, 1, "2.0", "2.0", "2.0", "2.0"),  # no ask twin -> dropped
    ]
    ask = [_c("ask", 10, 0, "1.1", "1.1", "1.1", "1.1")]
    bars = m5_from_m1(bid, ask)
    assert len(bars) == 1
    assert bars[0].bid_h == Decimal("1.0")  # the 10:01 bid never contributes


def test_m5_from_m1_multiple_buckets():
    bid = [_c("bid", 10, m, "1.0", "1.0", "1.0", "1.0") for m in (0, 3, 5, 9)]
    ask = [_c("ask", 10, m, "1.1", "1.1", "1.1", "1.1") for m in (0, 3, 5, 9)]
    bars = m5_from_m1(bid, ask)
    assert [b.ts_open_utc.minute for b in bars] == [0, 5]


def test_find_missing_m5():
    mk = lambda minute: m5_from_ticks([_t(10, minute, 0, "1.0", "1.1")])[0]
    bars = [mk(0), mk(10)]  # 10:05 missing
    start = datetime(2024, 3, 15, 10, 0, tzinfo=UTC)
    end = datetime(2024, 3, 15, 10, 15, tzinfo=UTC)
    missing = find_missing_m5(bars, start, end)
    assert missing == [datetime(2024, 3, 15, 10, 5, tzinfo=UTC)]
