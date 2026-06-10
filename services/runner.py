"""Paper/live trading session (SPEC.md §10.2): the same pure core driven by
ports, an order manager, and native broker brackets.

One PaperSession instance covers one symbol-day in v1 (max_concurrent=1 and
0-3 trades/day keep this simple); a thin outer loop constructs sessions per
day and polls. The asyncio wrapper is deliberately trivial: poll quotes every
250-500 ms, process closed bars as they complete, let the watchdog gate
entries via OpsHealth.

Division of labor with the broker:
- SL and TP2 ride ON the broker as a native bracket from the moment of entry;
- the session handles TP1 partial close + breakeven move, the time stop, the
  forced exit, and the daily hard-stop flatten — via core.manage_on_quote.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from core.compliance import entry_blocked, initial_compliance, on_daily_reset, on_equity
from core.config_schema import FirmProfile, InstrumentsConfig, StrategyConfig
from core.events import M5Bar
from core.filters import FilterFailure, FilterInputs, OpsHealth, evaluate_no_trade
from core.reasons import ReasonCode
from core.risk import (
    entry_halts,
    initial_risk_state,
    latch_hard_stop,
    on_entry_opened,
    on_trade_closed,
    per_trade_risk,
    should_flatten_day,
)
from core.sizing import pip_value_per_lot, size_lots
from core.strategy_ssr import (
    AttemptEnded,
    DayInputs,
    NoTradeDay,
    SetupDetected,
    SSREngine,
    manage_on_quote,
)
from core.timebase import session_windows, self_check
from ports.store import DecisionRow
from services.order_manager import EntryRequest, OrderManager

D = Decimal
ZERO = D(0)


@dataclass
class _LiveTrade:
    intent_id: str
    ticket: str
    pos: object  # core OpenPosition
    risk_usd: Decimal
    entry_fill: Decimal


class PaperSession:
    """One trading day for one symbol against an ExecutionPort with quotes."""

    def __init__(
        self,
        symbol: str,
        day: date,
        strategy: StrategyConfig,
        firm: FirmProfile,
        instruments: InstrumentsConfig,
        broker,  # ExecutionPort + latest_quote(); PaperBroker also has check_brackets()
        order_manager: OrderManager,
        clock,
        store,
        calendar,
        adr20_pips: Decimal | None,
        adr20_pctile: Decimal | None,
        starting_equity: Decimal,
        ops: OpsHealth = OpsHealth(),
    ):
        self.symbol = symbol
        self.day = day
        self.strategy = strategy
        self.firm = firm
        self.spec = instruments.instruments[symbol]
        self.broker = broker
        self.om = order_manager
        self.clock = clock
        self.store = store
        self.calendar = calendar
        self.adr20_pctile = adr20_pctile
        self.ops = ops
        self.windows = session_windows(day)
        self.violations = self_check(self.windows)
        self.engine = SSREngine(
            symbol=symbol, pair=strategy.pairs[symbol], shared=strategy.shared,
            pip=self.spec.pip, day=DayInputs(day, self.windows, self.violations, adr20_pips),
        )
        self.risk = initial_risk_state(day)
        self.comp = on_daily_reset(
            initial_compliance(firm, starting_equity), starting_equity
        )
        self.open: _LiveTrade | None = None
        self.closed_trades: list[dict] = []
        self._spread_hist: list[Decimal] = []
        self._decision_seq = 0

    # -- decision trail -------------------------------------------------------

    def _decision(self, setup_id, stage, passed, code, detail=""):
        self._decision_seq += 1
        self.store.insert_decision(DecisionRow(
            id=f"{self.day}|{self.symbol}|{self._decision_seq}",
            setup_id=setup_id, ts_utc=self.clock.now_utc(), stage=stage,
            passed=passed, reason_code=code, details_json=detail and f'{{"detail": "{detail}"}}' or "{}",
        ))

    # -- closed bars ------------------------------------------------------------

    def process_bar(self, bar: M5Bar) -> None:
        self._spread_hist.append(bar.spread_median)
        if len(self._spread_hist) > 12:
            self._spread_hist.pop(0)
        for out in self.engine.on_bar(bar):
            if isinstance(out, NoTradeDay):
                for f in out.failures:
                    self._decision(None, "day_gate", False, f.code.value, f.detail)
            elif isinstance(out, AttemptEnded):
                self._decision(f"{self.day}|{self.symbol}|{out.direction}", "structure",
                               False, out.code.value, out.detail)
            elif isinstance(out, SetupDetected):
                self._try_enter(out.candidate)

    # -- quote polling -----------------------------------------------------------

    def poll_quotes(self) -> None:
        # 1. native brackets first (what the broker would have done already)
        if hasattr(self.broker, "check_brackets"):
            for c in self.broker.check_brackets():
                if self.open is not None and c.ticket == self.open.ticket:
                    self._realize_bracket(c)
        if self.open is None:
            return
        q = self.broker.latest_quote(self.symbol)
        if q is None:
            return
        # 2. equity mark + hard-stop flatten (§7)
        acct = self.broker.account()
        self.comp = on_equity(self.comp, acct.equity, self.firm)
        floating = acct.equity - acct.balance
        if should_flatten_day(self.risk, self.strategy.risk, floating):
            self.risk = latch_hard_stop(self.risk)
            self._close_all("forced")
            return
        # 3. TP1 partial / time stop / forced exit
        new_pos, signals = manage_on_quote(
            self.open.pos, q.bid, self.clock.now_utc(),
            forced_exit_utc=self.windows.forced_exit_utc,
            time_stop_min=self.strategy.shared.time_stop_min,
            time_stop_threshold_r=D("0.5"),
            tp1_fraction=self.strategy.shared.tp1_close,
        )
        for s in signals:
            if s.kind == "tp1":
                self.om.close_position(self.open.ticket, s.lots, "tp1", self.open.intent_id)
                self.om.modify_sl(self.open.ticket, self.open.pos.entry_price)
            else:
                self._close_all(s.kind)
                return
        if new_pos is not None:
            self.open.pos = new_pos

    # -- helpers --------------------------------------------------------------------

    def _realize_bracket(self, c) -> None:
        entry = self.open.entry_fill
        move = (
            c.price - entry if self.open.pos.direction == "long" else entry - c.price
        )
        pnl = move * c.lots * self.spec.contract_size
        self._finalize(pnl, c.kind)

    def _close_all(self, kind: str) -> None:
        status, result = self.om.close_position(
            self.open.ticket, None, kind, self.open.intent_id
        )
        if status != "closed":
            return  # alert already fired; flattener is the backstop
        entry = self.open.entry_fill
        move = (
            result.fill_price - entry
            if self.open.pos.direction == "long" else entry - result.fill_price
        )
        self._finalize(move * (result.filled_lots or ZERO) * self.spec.contract_size, kind)

    def _finalize(self, last_leg_pnl: Decimal, kind: str) -> None:
        acct = self.broker.account()
        trade = {
            "intent_id": self.open.intent_id, "final_kind": kind,
            "risk_usd": self.open.risk_usd, "balance_after": acct.balance,
        }
        # realized day P&L comes from the broker balance, the source of truth
        realized = acct.balance - self._balance_at_entry
        self.risk = on_trade_closed(self.risk, realized)
        trade["pnl_usd"] = realized
        self.closed_trades.append(trade)
        self.store.update_order_intent(self.open.intent_id, "closed")
        self.open = None

    def _try_enter(self, cand) -> None:
        if self.open is not None:
            self._decision(cand.setup_id, "filters", False,
                           ReasonCode.POSITION_OPEN.value, "")
            return
        halt_codes = list(entry_halts(self.risk, self.strategy.risk))
        acct = self.broker.account()
        block = entry_blocked(self.comp, acct.equity, self.strategy.risk.floor_buffer)
        if block is not None:
            halt_codes.append(block)
        hist = sorted(self._spread_hist)
        median = hist[len(hist) // 2] if hist else None
        pair = self.strategy.pairs[self.symbol]
        news = self.strategy.news
        inputs = FilterInputs(
            ts_utc=cand.ts_utc, windows=self.windows, window_violations=self.violations,
            events=tuple(self.calendar.events_between(
                cand.ts_utc - timedelta(hours=12), cand.ts_utc + timedelta(hours=12))),
            currencies=tuple(news.currencies[self.symbol]),
            news_pre_min=news.pre_min, news_post_min=news.post_min,
            news_lookahead_min=news.lookahead_block_min,
            spread=cand.decision_spread,
            spread_abs_cap=pair.spread_abs_cap * self.spec.pip,
            spread_median_60m=median,
            spread_median_mult=self.strategy.shared.spread_median_mult,
            adr20_pctile=self.adr20_pctile,
            adr_skip_low=self.strategy.shared.adr_pctile_skip[0],
            adr_skip_high=self.strategy.shared.adr_pctile_skip[1],
            bank_holiday=False, monday_gap=False,
            halt_reasons=tuple(halt_codes), ops=self.ops,
            symbol_valid=True, position_open_in_risk_unit=False,
        )
        # spread re-check AT SEND uses the live quote, not just decision time (§3.1)
        q = self.broker.latest_quote(self.symbol)
        fails = list(evaluate_no_trade(inputs))
        if q is not None and q.spread > pair.spread_abs_cap * self.spec.pip:
            fails.append(FilterFailure(ReasonCode.SPREAD_GATE, f"at-send spread={q.spread}"))
        if fails:
            for f in fails:
                self._decision(cand.setup_id, "filters", False, f.code.value, f.detail)
            return
        self._decision(cand.setup_id, "filters", True, "PASS")

        risk_usd = per_trade_risk(self.risk, self.strategy.risk)
        pip_val = pip_value_per_lot(self.spec.contract_size, self.spec.pip)
        lots, why = size_lots(risk_usd, cand.stop_pips, pip_val, self.spec, self.firm.max_lots)
        if lots is None:
            self._decision(cand.setup_id, "sizing", False, why.value, "")
            return

        req = EntryRequest(
            intent_id=cand.setup_id, setup_id=cand.setup_id, symbol=self.symbol,
            side=cand.direction, lots=lots, entry_ref=cand.entry_ref,
            sl=cand.sl, tp=cand.tp2, max_deviation=pair.max_deviation * self.spec.pip,
            pip=self.spec.pip,
        )
        self._balance_at_entry = self.broker.account().balance
        status, result = self.om.submit_entry(req)
        if status != "filled":
            self._decision(cand.setup_id, "entry", False,
                           ReasonCode.ORDER_REJECTED.value, status)
            return
        pos = self.om.position_from_entry(
            req, result, entry_ts=self.clock.now_utc(), tp1=cand.tp1, tp2=cand.tp2
        )
        self.risk = on_entry_opened(self.risk)
        self.open = _LiveTrade(
            intent_id=cand.setup_id, ticket=result.broker_ticket, pos=pos,
            risk_usd=lots * cand.stop_pips * pip_val, entry_fill=pos.entry_price,
        )
        self._decision(cand.setup_id, "entry", True, "PASS", f"ticket={result.broker_ticket}")
