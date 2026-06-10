"""Dukascopy historical data downloader (SPEC.md §13.1).

Two acquisition paths share one cache:
- hourly tick archives (the spec's primary, tick-grade path — used for fill-model
  validation slices and spot cross-checks);
- daily BID/ASK one-minute candle files (bulk history: ~1/1000th the requests,
  same feed) which bar_builder aggregates to M5.

bi5 payloads are LZMA "alone"-format streams of big-endian records:
  ticks:   >IIIff   ms-in-hour, ASK points, BID points, ask_vol, bid_vol
  candles: >IIIIIf  secs-in-day, OPEN, CLOSE, LOW, HIGH, volume  (OCLH order)
Integer prices are multiples of the instrument point. Dukascopy hour/day files
are UTC-aligned; the URL month is zero-indexed.

HTTP 404 means "no data for that period" (weekends/holidays) and is cached as an
empty file so re-runs are cheap and offline-reproducible.
"""
from __future__ import annotations

import lzma
import struct
import time as _time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx

from core.events import M1Candle, M5Bar, Tick

BASE = "https://datafeed.dukascopy.com/datafeed"
_TICK = struct.Struct(">IIIff")
_CANDLE = struct.Struct(">IIIIIf")
UTC = timezone.utc


def tick_url(symbol: str, hour_utc: datetime) -> str:
    return (
        f"{BASE}/{symbol}/{hour_utc.year}/{hour_utc.month - 1:02d}/"
        f"{hour_utc.day:02d}/{hour_utc.hour:02d}h_ticks.bi5"
    )


def candle_url(symbol: str, day: date, side: Literal["bid", "ask"]) -> str:
    return (
        f"{BASE}/{symbol}/{day.year}/{day.month - 1:02d}/{day.day:02d}/"
        f"{side.upper()}_candles_min_1.bi5"
    )


def decode_bi5(data: bytes) -> bytes:
    if not data:
        return b""
    return lzma.decompress(data)


def parse_tick_records(
    decoded: bytes, symbol: str, hour_start_utc: datetime, point: Decimal
) -> list[Tick]:
    if len(decoded) % _TICK.size != 0:
        raise ValueError(f"tick payload not a multiple of {_TICK.size} bytes")
    ticks = [
        Tick(
            symbol=symbol,
            ts_utc=hour_start_utc + timedelta(milliseconds=ms),
            bid=Decimal(bid_pts) * point,
            ask=Decimal(ask_pts) * point,
            bid_vol=bid_vol,
            ask_vol=ask_vol,
        )
        for ms, ask_pts, bid_pts, ask_vol, bid_vol in _TICK.iter_unpack(decoded)
    ]
    ticks.sort(key=lambda t: t.ts_utc)
    return ticks


def parse_candle_records(
    decoded: bytes,
    symbol: str,
    side: Literal["bid", "ask"],
    day_start_utc: datetime,
    point: Decimal,
) -> list[M1Candle]:
    if len(decoded) % _CANDLE.size != 0:
        raise ValueError(f"candle payload not a multiple of {_CANDLE.size} bytes")
    candles = [
        M1Candle(
            symbol=symbol,
            side=side,
            ts_open_utc=day_start_utc + timedelta(seconds=secs),
            o=Decimal(o) * point,
            h=Decimal(h) * point,
            l=Decimal(l) * point,
            c=Decimal(c) * point,
            volume=vol,
        )
        for secs, o, c, l, h, vol in _CANDLE.iter_unpack(decoded)
    ]
    candles.sort(key=lambda x: x.ts_open_utc)
    return candles


class DukascopyClient:
    """Cached, retrying fetcher. All parsing stays in the pure functions above."""

    def __init__(
        self,
        cache_dir: Path | str,
        http: httpx.Client | None = None,
        max_retries: int = 4,
        backoff_s: float = 1.5,
    ):
        self.cache_dir = Path(cache_dir)
        self.http = http or httpx.Client(timeout=30.0, follow_redirects=True)
        self.max_retries = max_retries
        self.backoff_s = backoff_s

    def _cached_fetch(self, url: str, cache_path: Path) -> bytes:
        if cache_path.exists():
            return cache_path.read_bytes()
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self.http.get(url)
                if resp.status_code == 404:
                    data = b""
                elif resp.status_code >= 400:
                    raise httpx.HTTPStatusError(
                        f"{resp.status_code} for {url}", request=resp.request, response=resp
                    )
                else:
                    data = resp.content
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_bytes(data)
                return data
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_err = exc
                _time.sleep(self.backoff_s * (2**attempt))
        raise RuntimeError(f"download failed after {self.max_retries} tries: {url}") from last_err

    def hour_ticks(self, symbol: str, hour_utc: datetime, point: Decimal) -> list[Tick]:
        hour_utc = hour_utc.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
        cache = (
            self.cache_dir / symbol / f"{hour_utc.year}" / f"{hour_utc.month:02d}"
            / f"{hour_utc.day:02d}" / f"{hour_utc.hour:02d}h_ticks.bi5"
        )
        raw = self._cached_fetch(tick_url(symbol, hour_utc), cache)
        return parse_tick_records(decode_bi5(raw), symbol, hour_utc, point)

    def day_candles(
        self, symbol: str, day: date, side: Literal["bid", "ask"], point: Decimal
    ) -> list[M1Candle]:
        cache = (
            self.cache_dir / symbol / f"{day.year}" / f"{day.month:02d}"
            / f"{day.day:02d}" / f"{side.upper()}_candles_min_1.bi5"
        )
        raw = self._cached_fetch(candle_url(symbol, day, side), cache)
        day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        return parse_candle_records(decode_bi5(raw), symbol, side, day_start, point)

    def day_m5_from_candles(self, symbol: str, day: date, point: Decimal) -> list[M5Bar]:
        from services.bar_builder import m5_from_m1

        bid = self.day_candles(symbol, day, "bid", point)
        ask = self.day_candles(symbol, day, "ask", point)
        return m5_from_m1(bid, ask)

    def day_m5_from_ticks(self, symbol: str, day: date, point: Decimal) -> list[M5Bar]:
        from services.bar_builder import m5_from_ticks

        ticks: list[Tick] = []
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        for h in range(24):
            ticks.extend(self.hour_ticks(symbol, start + timedelta(hours=h), point))
        return m5_from_ticks(ticks)


def _is_dead_bar(bar: M5Bar) -> bool:
    """Zero-volume flat padding candle (e.g. after Friday close) — not market data."""
    return bar.tick_volume == 0 and bar.bid_h == bar.bid_l


def build_m5_database(
    symbols: list[str],
    start_day: date,
    end_day: date,
    db_path: Path | str,
    cache_dir: Path | str,
    points: dict[str, Decimal],
    progress_every: int = 50,
) -> dict[str, int]:
    """Download M1 candles, aggregate to M5, persist via SqliteStore. Resumable:
    both HTTP cache and candle upsert are idempotent."""
    from adapters.sqlite_store import SqliteStore

    client = DukascopyClient(cache_dir)
    store = SqliteStore(db_path, points=points)
    written = {s: 0 for s in symbols}
    day = start_day
    n_days = 0
    while day <= end_day:
        if day.weekday() != 5:  # Dukascopy Saturdays are empty
            for symbol in symbols:
                bars = [
                    b for b in client.day_m5_from_candles(symbol, day, points[symbol])
                    if not _is_dead_bar(b)
                ]
                written[symbol] += store.upsert_candles(bars)
        n_days += 1
        if progress_every and n_days % progress_every == 0:
            print(f"...{day.isoformat()} done; bars so far: {written}", flush=True)
        day = date.fromordinal(day.toordinal() + 1)
    return written


def main() -> None:
    import argparse

    import yaml

    ap = argparse.ArgumentParser(description="Dukascopy -> M5 sqlite builder")
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--db", default="data/market.sqlite")
    ap.add_argument("--cache", default="data/dukascopy")
    ap.add_argument("--instruments", default="configs/instruments.yaml")
    args = ap.parse_args()

    spec = yaml.safe_load(Path(args.instruments).read_text(encoding="utf-8"))["instruments"]
    points = {s: Decimal(str(spec[s]["point"])) for s in args.symbols}
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    written = build_m5_database(args.symbols, args.start, args.end, args.db, args.cache, points)
    print(f"done: {written}")


if __name__ == "__main__":
    main()
