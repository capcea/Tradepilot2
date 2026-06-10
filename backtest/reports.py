"""Backtest metrics + report rendering (SPEC.md §13.5, §18).

Metrics per spec: expectancy in R, profit factor, win rate, max intraday-equity
drawdown, time under water, daily P&L concentration (the consistency-rule
check), trades/day distribution, per-year breakdown. All money math Decimal.
"""
from __future__ import annotations

import html as _html
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Sequence

D = Decimal
ZERO = D(0)


def compute_metrics(trades, equity_curve, starting_equity: Decimal) -> dict:
    n = len(trades)
    m: dict = {"n_trades": n}
    if n == 0:
        m.update(win_rate=None, expectancy_r=None, profit_factor=None,
                 net_pnl_usd=ZERO, max_day_share_of_net=None,
                 max_drawdown_usd=_max_dd(equity_curve),
                 time_under_water_days=None, per_year={}, trades_per_day_hist={})
        return m

    wins = [t for t in trades if t.pnl_usd > 0]
    rs = [t.r_multiple for t in trades]
    gross_win_r = sum((r for r in rs if r > 0), ZERO)
    gross_loss_r = -sum((r for r in rs if r < 0), ZERO)

    m["win_rate"] = D(len(wins)) / D(n)
    m["expectancy_r"] = sum(rs, ZERO) / D(n)
    m["profit_factor"] = (gross_win_r / gross_loss_r) if gross_loss_r > 0 else None
    m["net_pnl_usd"] = sum((t.pnl_usd for t in trades), ZERO)
    m["avg_win_r"] = (sum((r for r in rs if r > 0), ZERO) / D(len(wins))) if wins else None
    losses = [r for r in rs if r < 0]
    m["avg_loss_r"] = (sum(losses, ZERO) / D(len(losses))) if losses else None

    daily: dict = defaultdict(lambda: ZERO)
    for t in trades:
        daily[t.day] += t.pnl_usd
    m["daily_net"] = dict(daily)
    net = m["net_pnl_usd"]
    if net > 0:
        best = max(daily.values())
        m["max_day_share_of_net"] = best / net if best > 0 else ZERO
    else:
        m["max_day_share_of_net"] = None

    m["max_drawdown_usd"] = _max_dd(equity_curve)
    m["time_under_water_days"] = _time_under_water(equity_curve)

    per_year: dict = {}
    by_year: dict = defaultdict(list)
    for t in trades:
        by_year[t.day.year].append(t)
    for year, ts in sorted(by_year.items()):
        yr_rs = [t.r_multiple for t in ts]
        gw = sum((r for r in yr_rs if r > 0), ZERO)
        gl = -sum((r for r in yr_rs if r < 0), ZERO)
        per_year[year] = {
            "n": len(ts),
            "net_usd": sum((t.pnl_usd for t in ts), ZERO),
            "expectancy_r": sum(yr_rs, ZERO) / D(len(ts)),
            "profit_factor": (gw / gl) if gl > 0 else None,
            "win_rate": D(sum(1 for t in ts if t.pnl_usd > 0)) / D(len(ts)),
        }
    m["per_year"] = per_year
    m["trades_per_day_hist"] = dict(Counter(sum(1 for t in trades if t.day == d) for d in daily))
    return m


def _max_dd(curve: Sequence[tuple[datetime, Decimal]]) -> Decimal:
    peak = None
    dd = ZERO
    for _, e in curve:
        peak = e if peak is None else max(peak, e)
        dd = max(dd, peak - e)
    return dd


def _time_under_water_days(curve):  # pragma: no cover - alias kept private
    return _time_under_water(curve)


def _time_under_water(curve: Sequence[tuple[datetime, Decimal]]) -> int | None:
    if not curve:
        return None
    peak = curve[0][1]
    peak_ts = curve[0][0]
    worst = 0
    for ts, e in curve:
        if e >= peak:
            peak, peak_ts = e, ts
        else:
            worst = max(worst, (ts - peak_ts).days)
    return worst


def _fmt(x, places: str = "0.0001") -> str:
    if x is None:
        return "n/a"
    if isinstance(x, Decimal):
        return str(x.quantize(D(places)))
    return str(x)


def render_markdown(metrics: dict, mc, params: dict, caveats: list[str]) -> str:
    lines = ["# SSR v1 Backtest Report", ""]
    lines += [f"- **{k}**: {v}" for k, v in params.items()]
    lines += ["", "## Headline metrics", ""]
    lines.append(f"- Trades: {metrics['n_trades']}")
    lines.append(f"- Win rate: {_fmt(metrics.get('win_rate'))}")
    lines.append(f"- Expectancy (R): {_fmt(metrics.get('expectancy_r'))}")
    lines.append(f"- Profit factor (R, after costs): {_fmt(metrics.get('profit_factor'))}")
    lines.append(f"- Avg win (R): {_fmt(metrics.get('avg_win_r'))}")
    lines.append(f"- Avg loss (R): {_fmt(metrics.get('avg_loss_r'))}")
    lines.append(f"- Net P&L: ${_fmt(metrics.get('net_pnl_usd'), '0.01')}")
    lines.append(f"- Max equity drawdown: ${_fmt(metrics.get('max_drawdown_usd'), '0.01')}")
    lines.append(f"- Time under water (days): {metrics.get('time_under_water_days')}")
    lines.append(
        f"- Daily concentration (max day / net): {_fmt(metrics.get('max_day_share_of_net'))}"
    )
    hist = metrics.get("trades_per_day_hist") or {}
    if hist:
        lines.append(f"- Trades/active-day histogram: {dict(sorted(hist.items()))}")

    per_year = metrics.get("per_year") or {}
    if per_year:
        lines += ["", "## Per-year breakdown", "",
                  "| Year | Trades | Net $ | Expectancy R | PF | Win rate |",
                  "|---|---|---|---|---|---|"]
        for year, y in per_year.items():
            lines.append(
                f"| {year} | {y['n']} | {_fmt(y['net_usd'], '0.01')} | "
                f"{_fmt(y['expectancy_r'])} | {_fmt(y['profit_factor'])} | {_fmt(y['win_rate'])} |"
            )

    if mc is not None:
        lines += ["", "## Evaluation-pass Monte Carlo (§13.6)", ""]
        lines.append(f"- Simulations: {mc.n_sims}")
        lines.append(f"- Pass probability per attempt: {_fmt(mc.pass_probability)}")
        lines.append(f"- Busts: {mc.busts}  |  Timeouts: {mc.timeouts}")
        lines.append(f"- Expected attempts: {_fmt(mc.expected_attempts, '0.01')}")
        lines.append(
            f"- Days to pass (p25/median/p75): {mc.p25_days} / {mc.median_days_to_pass} / {mc.p75_days}"
        )

    lines += ["", "## Caveats (read before believing anything above)", ""]
    lines += [f"- {c}" for c in caveats]
    lines.append("")
    return "\n".join(lines)


def render_html(md_text: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>SSR v1 Backtest Report</title>"
        "<style>body{font-family:monospace;max-width:900px;margin:2em auto;"
        "white-space:pre-wrap}</style></head><body>"
        + _html.escape(md_text)
        + "</body></html>"
    )
