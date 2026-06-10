"""Backtest engine (SPEC.md §13): drives the SAME pure core through backtest
adapters, day by day, with no look-ahead.

No-look-ahead enforcement: the core sees only closed bars in time order; ADR20
for day D is computed from days strictly before D; the calendar port serves
events by timestamp; fills derive from the bar being processed.

Run a campaign:
  python -m backtest.engine --db data/market.sqlite --start 2019-01-01 --end 2026-06-05
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable, Protocol, Sequence

from backtest.costs import CostModel, commission_usd, entry_fill_price
from backtest.fills import Fill, price_exit_signals
from core.compliance import (
    ComplianceState,
    entry_blocked,
    initial_compliance,
    on_daily_reset,
    on_equity,
)
from core.config_schema import (
    FirmProfile,
    InstrumentsConfig,
    StrategyConfig,
    validate_startup,
)
from core.events import M5Bar
from core.filters import FilterInputs, OpsHealth, evaluate_no_trade, monday_gap_flag
from core.filters import compute_adr, percentile_rank
from core.reasons import ReasonCode
from core.risk import (
    RiskState,
    entry_halts,
    initial_risk_state,
    latch_hard_stop,
    on_entry_opened,
    on_trade_closed,
    per_trade_risk,
    roll_to_day,
    should_flatten_day,
)
from core.sizing import margin_ok, margin_required, pip_value_per_lot, size_lots
from core.strategy_ssr import (
    AttemptEnded,
    DayInputs,
    ExitSignal,
    NoTradeDay,
    OpenPosition,
    RangeLocked,
    SetupCandidate,
    SetupDetected,
    SSREngine,
    manage_on_bar,
)
from core.timebase import LONDON, session_windows, self_check
from ports.calendar import CalendarPort

D = Decimal
ZERO = D(0)


# ---------------------------------------------------------------------------
# Feeds
# ---------------------------------------------------------------------------

class BarFeed(Protocol):
    def day_bars(self, symbol: str, day: date) -> Sequence[M5Bar]: ...


class DictFeed:
    def __init__(self, mapping: dict[tuple[str, date], list[M5Bar]]):
        self.mapping = mapping

    def day_bars(self, symbol: str, day: date) -> list[M5Bar]:
        return self.mapping.get((symbol, day), [])


class StoreFeed:
    def __init__(self, store):
        self.store = store

    def day_bars(self, symbol: str, day: date) -> list[M5Bar]:
        w = session_windows(day)
        return self.store.get_candles(
            symbol, w.asian_start_utc, w.forced_exit_utc + timedelta(hours=1)
        )


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Trade:
    setup_id: str
    symbol: str
    direction: str
    day: date
    entry_ts: datetime
    entry_price: Decimal
    lots: Decimal
    risk_usd: Decimal
    stop_pips: Decimal
    exit_fills: tuple[Fill, ...]
    pnl_usd: Decimal
    r_multiple: Decimal


@dataclass(frozen=True)
class DecisionRecord:
    ts_utc: datetime
    setup_id: str | None
    stage: str
    passed: bool
    reason_code: str
    detail: str = ""


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    daily_realized: dict[date, Decimal] = field(default_factory=dict)
    trades_per_day: list[int] = field(default_factory=list)
    days_run: int = 0
    params: dict = field(default_factory=dict)


class _OpenTrade:
    def __init__(self, cand: SetupCandidate, pos: OpenPosition, risk_usd: Decimal):
        self.cand = cand
        self.pos = pos
        self.risk_usd = risk_usd
        self.fills: list[Fill] = []
        self.net: Decimal = ZERO


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    def __init__(
        self,
        strategy: StrategyConfig,
        firm: FirmProfile,
        instruments: InstrumentsConfig,
        feed: BarFeed,
        calendar: CalendarPort,
        costs: CostModel,
        adr_provider: Callable[[str, date], tuple[Decimal | None, Decimal | None]] | None = None,
        starting_equity: Decimal = D(50000),
        leverage: Decimal = D(100),
    ):
        validate_startup(strategy, firm, instruments)
        self.strategy = strategy
        self.firm = firm
        self.instruments = instruments.instruments
        self.feed = feed
        self.calendar = calendar
        self.costs = costs
        self.adr_provider = adr_provider
        self.starting_equity = starting_equity
        self.leverage = leverage
        self._ranges: dict[str, list[Decimal]] = {}
        self._adr_series: dict[str, list[Decimal]] = {}

    # -- ADR bookkeeping (no look-ahead: uses prior days only) ------------------

    _MIN_PCTILE_HISTORY = 60  # a percentile over a handful of points is noise

    def _adr_for(self, symbol: str, day: date) -> tuple[Decimal | None, Decimal | None]:
        if self.adr_provider is not None:
            return self.adr_provider(symbol, day)
        adr = compute_adr(self._ranges.get(symbol, []), 20)
        if adr is None:
            return None, None
        series = self._adr_series.get(symbol, [])
        if len(series) < self._MIN_PCTILE_HISTORY:
            return adr, None  # filter pipeline turns this into DATA_INCOMPLETE (conservative)
        return adr, percentile_rank(series[-252:] + [adr], adr)

    def _close_day_adr(self, symbol: str, bars: Sequence[M5Bar], pip: Decimal) -> None:
        if self.adr_provider is not None or not bars:
            return
        day_range = (max(b.bid_h for b in bars) - min(b.bid_l for b in bars)) / pip
        self._ranges.setdefault(symbol, []).append(day_range)
        adr = compute_adr(self._ranges[symbol], 20)
        if adr is not None:
            self._adr_series.setdefault(symbol, []).append(adr)

    # -- main loop ---------------------------------------------------------------

    def run(self, symbols: list[str], days: list[date]) -> BacktestResult:
        res = BacktestResult(params={"symbols": symbols, "days": len(days)})
        risk = initial_risk_state(days[0])
        comp = initial_compliance(self.firm, self.starting_equity)
        balance = self.starting_equity
        open_trade: _OpenTrade | None = None
        prev_close: dict[str, Decimal] = {}
        first = True

        for day in days:
            if day.weekday() >= 5:
                continue
            windows = session_windows(day)
            violations = self_check(windows)
            if not first:
                risk = roll_to_day(risk, day)
            first = False
            # Re-anchor the modeled floor to current balance each day: the backtest
            # measures the strategy's trade distribution under the DAY-level rules;
            # the multi-day trailing-floor RACE is what the §13.6 Monte Carlo
            # simulates from that distribution (DECISIONS.md M4).
            comp = on_daily_reset(initial_compliance(self.firm, balance), balance)

            day_bars: dict[str, list[M5Bar]] = {
                s: list(self.feed.day_bars(s, day)) for s in symbols
            }
            if not any(day_bars.values()):
                continue
            res.days_run += 1
            trades_at_day_start = len(res.trades)

            engines: dict[str, SSREngine] = {}
            day_open: dict[str, Decimal] = {}
            adr_price: dict[str, Decimal | None] = {}
            adr_pct: dict[str, Decimal | None] = {}
            for s in symbols:
                pip = self.instruments[s].pip
                adr_pips, pctile = self._adr_for(s, day)
                adr_price[s] = None if adr_pips is None else adr_pips * pip
                adr_pct[s] = pctile
                engines[s] = SSREngine(
                    symbol=s,
                    pair=self.strategy.pairs[s],
                    shared=self.strategy.shared,
                    pip=pip,
                    day=DayInputs(day, windows, violations, adr_pips),
                )
                if day_bars[s]:
                    day_open[s] = day_bars[s][0].bid_o

            spread_hist: dict[str, list[Decimal]] = {s: [] for s in symbols}
            merged = sorted(
                (b for bars in day_bars.values() for b in bars), key=lambda b: b.ts_open_utc
            )

            for bar in merged:
                s = bar.symbol
                hist = spread_hist[s]
                hist.append(bar.spread_median)
                if len(hist) > 12:
                    hist.pop(0)

                # 1. manage the open position through this bar (intrabar events
                #    chronologically precede close-of-bar decisions)
                if open_trade is not None and open_trade.pos.symbol == s and bar.ts_open_utc >= open_trade.pos.entry_ts_utc:
                    open_trade, balance, risk = self._manage(
                        open_trade, bar, balance, risk, res, windows
                    )

                # 2. strategy core on the closed bar
                for out in engines[s].on_bar(bar):
                    if isinstance(out, NoTradeDay):
                        for f in out.failures:
                            res.decisions.append(DecisionRecord(
                                bar.ts_close_utc, None, "day_gate", False, f.code.value, f.detail))
                    elif isinstance(out, AttemptEnded):
                        res.decisions.append(DecisionRecord(
                            bar.ts_close_utc, f"{day}|{s}|{out.direction}", "structure",
                            False, out.code.value, out.detail))
                    elif isinstance(out, SetupDetected) and open_trade is None:
                        open_trade, balance, risk = self._try_enter(
                            out.candidate, bar, day, windows, violations, spread_hist[s],
                            adr_pct[s], adr_price[s], day_open.get(s), prev_close.get(s),
                            balance, risk, comp, res,
                        )
                    elif isinstance(out, SetupDetected):
                        res.decisions.append(DecisionRecord(
                            bar.ts_close_utc, out.candidate.setup_id, "filters", False,
                            ReasonCode.POSITION_OPEN.value, "risk unit busy"))

                # 3. mark equity, ratchet compliance, hard-stop check
                floating = self._floating(open_trade, bar) if (
                    open_trade is not None and open_trade.pos.symbol == s
                ) else (self._floating(open_trade, None) if open_trade else ZERO)
                equity = balance + floating
                comp = on_equity(comp, equity, self.firm)
                res.equity_curve.append((bar.ts_close_utc, equity))

                if open_trade is not None and should_flatten_day(
                    risk, self.strategy.risk, floating
                ) and open_trade.pos.symbol == s:
                    risk = latch_hard_stop(risk)
                    sig = ExitSignal("forced", open_trade.pos.lots_open, None, bar.ts_close_utc)
                    open_trade, balance, risk = self._apply_fills(
                        open_trade, [sig], bar, balance, risk, res, closed=True
                    )

            # end of day: a position must already be flat (forced 19:30); if the
            # feed ran out of bars early, close at last bar conservatively
            if open_trade is not None:
                last = max(
                    (b for b in day_bars[open_trade.pos.symbol]), key=lambda b: b.ts_open_utc
                )
                res.decisions.append(DecisionRecord(
                    last.ts_close_utc, open_trade.cand.setup_id, "ops", False,
                    ReasonCode.DATA_INCOMPLETE.value, "feed ended with open position"))
                sig = ExitSignal("forced", open_trade.pos.lots_open, None, last.ts_close_utc)
                open_trade, balance, risk = self._apply_fills(
                    open_trade, [sig], last, balance, risk, res, closed=True
                )

            for s in symbols:
                if day_bars[s]:
                    prev_close[s] = day_bars[s][-1].bid_c
                self._close_day_adr(s, day_bars[s], self.instruments[s].pip)

            res.daily_realized[day] = risk.day_realized
            res.trades_per_day.append(len(res.trades) - trades_at_day_start)

        return res

    # -- helpers -----------------------------------------------------------------

    def _floating(self, ot: _OpenTrade | None, bar: M5Bar | None) -> Decimal:
        if ot is None or bar is None:
            return ZERO
        spec = self.instruments[ot.pos.symbol]
        move = (
            bar.bid_c - ot.pos.entry_price
            if ot.pos.direction == "long"
            else ot.pos.entry_price - bar.bid_c
        )
        return move * ot.pos.lots_open * spec.contract_size

    def _manage(self, ot, bar, balance, risk, res, windows):
        pos = ot.pos
        new_pos, signals = manage_on_bar(
            pos, bar,
            forced_exit_utc=windows.forced_exit_utc,
            time_stop_min=self.strategy.shared.time_stop_min,
            time_stop_threshold_r=D("0.5"),
            tp1_fraction=self.strategy.shared.tp1_close,
        )
        if signals:
            ot.pos = new_pos if new_pos is not None else ot.pos
            ot, balance, risk = self._apply_fills(
                ot, signals, bar, balance, risk, res, closed=new_pos is None
            )
        elif new_pos is not None:
            ot.pos = new_pos
        return ot, balance, risk

    def _apply_fills(self, ot, signals, bar, balance, risk, res, closed: bool):
        spec = self.instruments[ot.pos.symbol]
        fills = price_exit_signals(
            signals, ot.pos.direction, bar, spec.contract_size, spec.pip,
            self.costs, entry_price=ot.pos.entry_price,
        )
        for f in fills:
            net = f.pnl_gross - commission_usd(f.lots, self.costs)
            balance += net
            ot.net += net
            ot.fills.append(f)
        if closed:
            trade = Trade(
                setup_id=ot.cand.setup_id, symbol=ot.pos.symbol,
                direction=ot.pos.direction, day=ot.cand.ts_utc.astimezone(LONDON).date(),
                entry_ts=ot.pos.entry_ts_utc, entry_price=ot.pos.entry_price,
                lots=ot.pos.lots_total, risk_usd=ot.risk_usd,
                stop_pips=ot.cand.stop_pips, exit_fills=tuple(ot.fills),
                pnl_usd=ot.net, r_multiple=ot.net / ot.risk_usd,
            )
            res.trades.append(trade)
            risk = on_trade_closed(risk, ot.net)
            return None, balance, risk
        return ot, balance, risk

    def _try_enter(
        self, cand, bar, day, windows, violations, spread_hist, adr_pctile, adr_price,
        day_open, prev_close, balance, risk, comp, res,
    ):
        s = cand.symbol
        spec = self.instruments[s]
        pair = self.strategy.pairs[s]
        news = self.strategy.news

        halt_codes = list(entry_halts(risk, self.strategy.risk))
        comp_block = entry_blocked(comp, balance, self.strategy.risk.floor_buffer)
        if comp_block is not None:
            halt_codes.append(comp_block)

        sorted_hist = sorted(spread_hist)
        median_spread = sorted_hist[len(sorted_hist) // 2] if sorted_hist else None
        gap = (
            day.weekday() == 0
            and prev_close is not None
            and day_open is not None
            and adr_price is not None
            and monday_gap_flag(prev_close, day_open, adr_price)
        )
        events = tuple(self.calendar.events_between(
            cand.ts_utc - timedelta(hours=12), cand.ts_utc + timedelta(hours=12)
        ))
        inputs = FilterInputs(
            ts_utc=cand.ts_utc, windows=windows, window_violations=violations,
            events=events, currencies=tuple(news.currencies[s]),
            news_pre_min=news.pre_min, news_post_min=news.post_min,
            news_lookahead_min=news.lookahead_block_min,
            spread=cand.decision_spread, spread_abs_cap=pair.spread_abs_cap * spec.pip,
            spread_median_60m=median_spread,
            spread_median_mult=self.strategy.shared.spread_median_mult,
            adr20_pctile=adr_pctile,
            adr_skip_low=self.strategy.shared.adr_pctile_skip[0],
            adr_skip_high=self.strategy.shared.adr_pctile_skip[1],
            bank_holiday=False,  # no historical holiday source; documented caveat
            monday_gap=bool(gap),
            halt_reasons=tuple(halt_codes),
            ops=OpsHealth(), symbol_valid=True,
            position_open_in_risk_unit=False,
        )
        fails = evaluate_no_trade(inputs)
        if fails:
            for f in fails:
                res.decisions.append(DecisionRecord(
                    cand.ts_utc, cand.setup_id, "filters", False, f.code.value, f.detail))
            return None, balance, risk
        res.decisions.append(DecisionRecord(
            cand.ts_utc, cand.setup_id, "filters", True, "PASS", ""))

        risk_usd_cfg = per_trade_risk(risk, self.strategy.risk)
        pip_val = pip_value_per_lot(spec.contract_size, spec.pip)
        lots, why = size_lots(risk_usd_cfg, cand.stop_pips, pip_val, spec, self.firm.max_lots)
        if lots is None:
            res.decisions.append(DecisionRecord(
                cand.ts_utc, cand.setup_id, "sizing", False, why.value, ""))
            return None, balance, risk
        margin = margin_required(lots, spec.contract_size, cand.entry_ref, self.leverage)
        if not margin_ok(balance, ZERO, margin):
            res.decisions.append(DecisionRecord(
                cand.ts_utc, cand.setup_id, "sizing", False,
                ReasonCode.MARGIN_INSUFFICIENT.value, f"margin={margin}"))
            return None, balance, risk

        entry = entry_fill_price(
            cand.direction, cand.entry_ref, cand.decision_spread, spec.pip, self.costs
        )
        pos = OpenPosition(
            symbol=s, direction=cand.direction, entry_price=entry,
            entry_ts_utc=cand.ts_utc, sl=cand.sl, tp1=cand.tp1, tp2=cand.tp2,
            lots_total=lots, lots_open=lots, tp1_done=False,
            r_price=cand.stop_pips * spec.pip,
        )
        risk = on_entry_opened(risk)
        actual_risk = lots * cand.stop_pips * pip_val
        res.decisions.append(DecisionRecord(
            cand.ts_utc, cand.setup_id, "entry", True, "PASS",
            f"lots={lots} entry={entry}"))
        return _OpenTrade(cand, pos, actual_risk), balance, risk


# ---------------------------------------------------------------------------
# Campaign CLI
# ---------------------------------------------------------------------------

def _trading_days(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def main() -> None:
    from adapters.ff_calendar import FileCalendar, parse_calendar_csv
    from adapters.sqlite_store import SqliteStore
    from backtest.montecarlo import EvalRace, run_eval_monte_carlo
    from backtest.reports import compute_metrics, render_html, render_markdown
    from services.config_loader import (
        load_firm_profile,
        load_instruments,
        load_strategy_config,
    )

    ap = argparse.ArgumentParser(description="SSR v1 backtest campaign")
    ap.add_argument("--db", default="data/market.sqlite")
    ap.add_argument("--start", type=date.fromisoformat, required=True)
    ap.add_argument("--end", type=date.fromisoformat, required=True)
    ap.add_argument("--symbols", nargs="+", default=["EURUSD", "GBPUSD"])
    ap.add_argument("--configs", default="configs")
    ap.add_argument("--calendar-csv", default=None)
    ap.add_argument("--slip-in", default=None, help="stress slippage-in pips")
    ap.add_argument("--slip-out", default=None, help="stress slippage-out pips")
    ap.add_argument("--report-dir", default="reports")
    ap.add_argument("--mc-sims", type=int, default=10000)
    ap.add_argument("--label", default="campaign")
    args = ap.parse_args()

    cfg_dir = Path(args.configs)
    strategy, _ = load_strategy_config(cfg_dir / "strategy.yaml")
    firm, _ = load_firm_profile(cfg_dir / "firm_profile.yaml")
    instruments, _ = load_instruments(cfg_dir / "instruments.yaml")
    points = {s: spec.point for s, spec in instruments.instruments.items()}
    store = SqliteStore(args.db, points=points)

    if args.calendar_csv:
        calendar = FileCalendar(
            parse_calendar_csv(Path(args.calendar_csv).read_text(encoding="utf-8"))
        )
        calendar_note = f"calendar CSV: {args.calendar_csv}"
    else:
        calendar = FileCalendar(())
        calendar_note = (
            "NEWS BLACKOUT INACTIVE: no historical high-impact calendar available; "
            "filter ran against an empty calendar. Treat results as un-news-filtered."
        )

    costs = CostModel(
        slippage_in_pips=D(args.slip_in) if args.slip_in else D("0.3"),
        slippage_out_pips=D(args.slip_out) if args.slip_out else D("0.4"),
    )

    engine = BacktestEngine(
        strategy=strategy, firm=firm, instruments=instruments,
        feed=StoreFeed(store), calendar=calendar, costs=costs,
    )
    days = _trading_days(args.start, args.end)
    print(f"running {len(days)} trading days on {args.symbols} ...", flush=True)
    result = engine.run(args.symbols, days)

    metrics = compute_metrics(result.trades, result.equity_curve, D(50000))
    mc = None
    if result.trades:
        mc = run_eval_monte_carlo(
            [t.r_multiple for t in result.trades],
            trades_per_day=result.trades_per_day or [0],
            race=EvalRace(
                target_usd=firm.profit_target,
                trailing_dd_usd=firm.trailing_dd.amount,
                risk_usd=strategy.risk.per_trade_usd,
                day_soft_stop=strategy.risk.day_soft_stop,
                consec_loss_halt=strategy.risk.consec_loss_halt,
                max_days=120,
            ),
            n_sims=args.mc_sims,
            seed=20260610,
        )

    caveats = [
        calendar_note,
        "Bank-holiday filter inactive (no historical holiday source); thin-range "
        "days are partially caught by the range filters.",
        "Dukascopy M1-candle-built M5 bars; wicks differ from any specific broker "
        "feed (SPEC §13.1 caveat). Re-run signal match on broker M1 before live.",
        f"Costs: slip_in={costs.slippage_in_pips}p slip_out={costs.slippage_out_pips}p "
        f"commission=${costs.commission_per_lot_rt}/lot RT, variable spread from data.",
    ]
    md = render_markdown(
        metrics=metrics, mc=mc,
        params={
            "symbols": " ".join(args.symbols),
            "period": f"{args.start} .. {args.end}",
            "days_run": result.days_run,
            "label": args.label,
        },
        caveats=caveats,
    )
    outdir = Path(args.report_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    md_path = outdir / f"{args.label}.md"
    md_path.write_text(md, encoding="utf-8")
    (outdir / f"{args.label}.html").write_text(render_html(md), encoding="utf-8")
    print(md)
    print(f"\nreport written to {md_path}")


if __name__ == "__main__":
    main()
