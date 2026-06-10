"""M4 report metrics tests (SPEC.md §13.5)."""
from datetime import date, datetime, timezone
from decimal import Decimal

from backtest.reports import compute_metrics, render_markdown

D = Decimal
UTC = timezone.utc


def _trade(day, r, pnl, symbol="EURUSD"):
    from backtest.engine import Trade

    return Trade(
        setup_id=f"{day}|{symbol}|long", symbol=symbol, direction="long",
        day=day, entry_ts=datetime(day.year, day.month, day.day, 8, tzinfo=UTC),
        entry_price=D("1.05"), lots=D("1"), risk_usd=D("175"),
        stop_pips=D("10"), exit_fills=(), pnl_usd=pnl, r_multiple=r,
    )


TRADES = [
    _trade(date(2024, 1, 2), D("1.0"), D("175")),
    _trade(date(2024, 1, 3), D("-1.0"), D("-175")),
    _trade(date(2024, 1, 4), D("2.0"), D("350")),
    _trade(date(2024, 1, 4), D("-1.0"), D("-175")),
]


def test_metrics_basics():
    m = compute_metrics(TRADES, equity_curve=[], starting_equity=D("50000"))
    assert m["n_trades"] == 4
    assert m["win_rate"] == D("0.5")
    assert m["expectancy_r"] == D("0.25")
    assert m["profit_factor"] == D("1.5")  # gross win 3R / gross loss 2R
    assert m["net_pnl_usd"] == D("175")


def test_daily_concentration():
    m = compute_metrics(TRADES, equity_curve=[], starting_equity=D("50000"))
    # daily nets: +175, -175, +175; max positive day / net total = 175/175
    assert m["max_day_share_of_net"] == D("1")


def test_max_drawdown_from_curve():
    curve = [
        (datetime(2024, 1, 2, tzinfo=UTC), D("50000")),
        (datetime(2024, 1, 3, tzinfo=UTC), D("50500")),
        (datetime(2024, 1, 4, tzinfo=UTC), D("49800")),
        (datetime(2024, 1, 5, tzinfo=UTC), D("50100")),
    ]
    m = compute_metrics(TRADES, equity_curve=curve, starting_equity=D("50000"))
    assert m["max_drawdown_usd"] == D("700")


def test_render_markdown_contains_key_sections(tmp_path):
    m = compute_metrics(TRADES, equity_curve=[], starting_equity=D("50000"))
    text = render_markdown(
        metrics=m, mc=None, params={"symbols": "EURUSD", "period": "2024"},
        caveats=["test caveat line"],
    )
    assert "Expectancy" in text
    assert "Profit factor" in text
    assert "test caveat line" in text
    out = tmp_path / "report.md"
    out.write_text(text, encoding="utf-8")
    assert out.exists()
