"""M1 Dukascopy downloader tests: URL layout, bi5 decode, record parsing (SPEC.md §13.1).

Network is never touched here; payloads are synthesized with struct+lzma and must
round-trip to exact Decimals.
"""
import lzma
import struct
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from services.data_downloader import (
    candle_url,
    decode_bi5,
    parse_candle_records,
    parse_tick_records,
    tick_url,
)

UTC = timezone.utc
POINT = Decimal("0.00001")


def _pack_ticks(*records: tuple[int, int, int, float, float]) -> bytes:
    raw = b"".join(struct.pack(">IIIff", *r) for r in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def _pack_candles(*records: tuple[int, int, int, int, int, float]) -> bytes:
    raw = b"".join(struct.pack(">IIIIIf", *r) for r in records)
    return lzma.compress(raw, format=lzma.FORMAT_ALONE)


def test_tick_url_month_is_zero_indexed():
    url = tick_url("EURUSD", datetime(2024, 3, 15, 10, tzinfo=UTC))
    assert url == "https://datafeed.dukascopy.com/datafeed/EURUSD/2024/02/15/10h_ticks.bi5"


def test_tick_url_january_pads_to_00():
    url = tick_url("GBPUSD", datetime(2025, 1, 2, 7, tzinfo=UTC))
    assert "/GBPUSD/2025/00/02/07h_ticks.bi5" in url


def test_candle_url_sides():
    bid = candle_url("EURUSD", date(2024, 3, 15), "bid")
    ask = candle_url("EURUSD", date(2024, 3, 15), "ask")
    assert bid.endswith("/EURUSD/2024/02/15/BID_candles_min_1.bi5")
    assert ask.endswith("/EURUSD/2024/02/15/ASK_candles_min_1.bi5")


def test_decode_bi5_roundtrip():
    raw = b"hello-bytes"
    assert decode_bi5(lzma.compress(raw, format=lzma.FORMAT_ALONE)) == raw


def test_decode_bi5_empty_is_empty():
    assert decode_bi5(b"") == b""


def test_parse_tick_records_exact_decimals():
    hour_start = datetime(2024, 3, 15, 10, tzinfo=UTC)
    # Dukascopy tick record: ms-in-hour, ASK points, BID points, ask_vol, bid_vol
    payload = decode_bi5(_pack_ticks((1500, 104523, 104511, 1.25, 0.75)))
    ticks = parse_tick_records(payload, "EURUSD", hour_start, POINT)
    assert len(ticks) == 1
    t = ticks[0]
    assert t.ts_utc == hour_start + timedelta(milliseconds=1500)
    assert t.ask == Decimal("1.04523")
    assert t.bid == Decimal("1.04511")
    assert t.spread == Decimal("0.00012")
    assert t.symbol == "EURUSD"


def test_parse_tick_records_ordering_and_count():
    hour_start = datetime(2024, 3, 15, 10, tzinfo=UTC)
    payload = decode_bi5(
        _pack_ticks(
            (10, 104500, 104490, 1.0, 1.0),
            (2000, 104510, 104500, 1.0, 1.0),
            (3_599_999, 104520, 104505, 1.0, 1.0),
        )
    )
    ticks = parse_tick_records(payload, "EURUSD", hour_start, POINT)
    assert [t.ts_utc for t in ticks] == sorted(t.ts_utc for t in ticks)
    assert len(ticks) == 3
    assert ticks[-1].ts_utc == hour_start + timedelta(milliseconds=3_599_999)


def test_parse_tick_records_empty():
    assert parse_tick_records(b"", "EURUSD", datetime(2024, 3, 15, tzinfo=UTC), POINT) == []


def test_parse_tick_records_rejects_misaligned_payload():
    with pytest.raises(ValueError):
        parse_tick_records(b"\x00" * 19, "EURUSD", datetime(2024, 3, 15, tzinfo=UTC), POINT)


def test_parse_candle_records_oclh_order():
    day_start = datetime(2024, 3, 15, tzinfo=UTC)
    # Dukascopy candle record: secs-in-day, OPEN, CLOSE, LOW, HIGH, volume
    payload = decode_bi5(_pack_candles((600, 104500, 104520, 104490, 104530, 12.5)))
    candles = parse_candle_records(payload, "EURUSD", "bid", day_start, POINT)
    assert len(candles) == 1
    c = candles[0]
    assert c.ts_open_utc == day_start + timedelta(seconds=600)
    assert c.o == Decimal("1.04500")
    assert c.c == Decimal("1.04520")
    assert c.l == Decimal("1.04490")
    assert c.h == Decimal("1.04530")
    assert c.h >= max(c.o, c.c) and c.l <= min(c.o, c.c)
    assert c.side == "bid"


def test_parse_candle_records_empty():
    assert parse_candle_records(b"", "EURUSD", "bid", datetime(2024, 3, 15, tzinfo=UTC), POINT) == []
