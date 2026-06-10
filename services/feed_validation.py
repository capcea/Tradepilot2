"""Feed validation utility (SPEC.md §13.1, M1 exit gate).

Cross-checks M5 bars built from Dukascopy TICKS against M5 bars built from
Dukascopy M1 CANDLES for the same day. Agreement here validates both bi5
parsers empirically; disagreement beyond tolerance means a format assumption
is wrong and the data layer must not be trusted.

Run:  python -m services.feed_validation EURUSD 2024-03-15 [--cache data/dukascopy]
"""
from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path

from services.bar_builder import find_missing_m5
from services.data_downloader import DukascopyClient


def cross_check_day(client: DukascopyClient, symbol: str, day: date, point: Decimal) -> dict:
    from_ticks = client.day_m5_from_ticks(symbol, day, point)
    from_candles = client.day_m5_from_candles(symbol, day, point)

    by_ts_t = {b.ts_open_utc: b for b in from_ticks}
    by_ts_c = {b.ts_open_utc: b for b in from_candles}
    common = sorted(by_ts_t.keys() & by_ts_c.keys())

    exact = 0
    max_diff_points = Decimal(0)
    sane = 0
    for ts in common:
        t, c = by_ts_t[ts], by_ts_c[ts]
        diffs = [abs(t.bid_o - c.bid_o), abs(t.bid_h - c.bid_h),
                 abs(t.bid_l - c.bid_l), abs(t.bid_c - c.bid_c),
                 abs(t.ask_c - c.ask_c)]
        worst = max(diffs) / point
        max_diff_points = max(max_diff_points, worst)
        if worst == 0:
            exact += 1
        ok_t = t.bid_h >= max(t.bid_o, t.bid_c) and t.bid_l <= min(t.bid_o, t.bid_c)
        ok_c = c.bid_h >= max(c.bid_o, c.bid_c) and c.bid_l <= min(c.bid_o, c.bid_c)
        if ok_t and ok_c:
            sane += 1

    return {
        "symbol": symbol,
        "day": day.isoformat(),
        "bars_from_ticks": len(from_ticks),
        "bars_from_candles": len(from_candles),
        "common_bars": len(common),
        "exact_match_bars": exact,
        "ohlc_sane_bars": sane,
        "max_abs_diff_points": str(max_diff_points),
        "tick_only_bars": len(by_ts_t.keys() - by_ts_c.keys()),
        "candle_only_bars": len(by_ts_c.keys() - by_ts_t.keys()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("day", type=date.fromisoformat)
    ap.add_argument("--cache", default="data/dukascopy")
    ap.add_argument("--point", default="0.00001")
    args = ap.parse_args()

    client = DukascopyClient(Path(args.cache))
    report = cross_check_day(client, args.symbol, args.day, Decimal(args.point))
    for k, v in report.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
