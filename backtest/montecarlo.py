"""Evaluation-pass Monte Carlo (SPEC.md §13.6).

Resamples the backtest's trade-level R distribution with a block bootstrap
(5-trade blocks preserve short-range autocorrelation) into simulated
evaluations of the actual race: +target vs trailing floor vs the internal
daily halts (soft stop, consecutive-loss halt). The honest summary statistic
for a prop context is the pass probability per attempt, not the equity curve.

Deterministic for a given seed (random.Random; no global state).
"""
from __future__ import annotations

import random
import statistics
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterator, Sequence

D = Decimal


@dataclass(frozen=True, slots=True)
class EvalRace:
    target_usd: Decimal
    trailing_dd_usd: Decimal
    risk_usd: Decimal
    day_soft_stop: Decimal  # negative
    consec_loss_halt: int
    max_days: int
    starting_equity: Decimal = D(50000)


@dataclass(frozen=True)
class MCOutcome:
    n_sims: int
    passes: int
    busts: int
    timeouts: int
    pass_probability: Decimal
    expected_attempts: Decimal | None
    median_days_to_pass: float | None
    p25_days: float | None
    p75_days: float | None


def _block_stream(rs: Sequence[Decimal], rng: random.Random, block: int) -> Iterator[Decimal]:
    n = len(rs)
    while True:
        start = rng.randrange(n)
        for k in range(block):
            yield rs[(start + k) % n]


def run_eval_monte_carlo(
    trade_rs: Sequence[Decimal],
    trades_per_day: Sequence[int],
    race: EvalRace,
    n_sims: int = 10000,
    seed: int = 0,
    block: int = 5,
) -> MCOutcome:
    if not trade_rs:
        raise ValueError("cannot bootstrap an empty trade distribution")
    rng = random.Random(seed)
    day_sizes = list(trades_per_day) or [0]

    passes = busts = timeouts = 0
    days_to_pass: list[int] = []

    for _ in range(n_sims):
        equity = race.starting_equity
        hwm = equity
        floor = hwm - race.trailing_dd_usd
        stream = _block_stream(trade_rs, rng, block)
        outcome = "timeout"
        days = 0
        while days < race.max_days:
            days += 1
            k = rng.choice(day_sizes)
            day_pnl = D(0)
            consec = 0
            for _t in range(k):
                pnl = next(stream) * race.risk_usd
                equity += pnl
                day_pnl += pnl
                hwm = max(hwm, equity)
                floor = max(floor, hwm - race.trailing_dd_usd)
                if equity <= floor:
                    outcome = "bust"
                    break
                if equity - race.starting_equity >= race.target_usd:
                    outcome = "pass"
                    break
                consec = consec + 1 if pnl < 0 else 0
                if day_pnl <= race.day_soft_stop or consec >= race.consec_loss_halt:
                    break  # internal halt: day over
            if outcome != "timeout":
                break
        if outcome == "pass":
            passes += 1
            days_to_pass.append(days)
        elif outcome == "bust":
            busts += 1
        else:
            timeouts += 1

    p = D(passes) / D(n_sims)
    return MCOutcome(
        n_sims=n_sims,
        passes=passes,
        busts=busts,
        timeouts=timeouts,
        pass_probability=p,
        expected_attempts=(D(1) / p) if p > 0 else None,
        median_days_to_pass=statistics.median(days_to_pass) if days_to_pass else None,
        p25_days=statistics.quantiles(days_to_pass, n=4)[0] if len(days_to_pass) >= 4 else None,
        p75_days=statistics.quantiles(days_to_pass, n=4)[2] if len(days_to_pass) >= 4 else None,
    )
