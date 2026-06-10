"""Independent flattener (SPEC.md §10.2, §19 row 15) — separate entrypoint.

Closes every open position, VERIFIES flat by re-querying the broker, retries,
and escalates with a critical alert if anything refuses to close. Runs as its
own process with its own broker session at 19:10/19:20/19:30 London and the
firm-cutoff T-20/T-10/T-5 checks (scheduling via OS scheduler or APScheduler —
see RUNBOOK).

Run once:  python -m services.flattener --mode paper
"""
from __future__ import annotations

import time as _time
from dataclasses import dataclass
from typing import Callable

from ports.execution import ExecutionPort


@dataclass(frozen=True, slots=True)
class FlattenReport:
    rounds: int
    closed_tickets: tuple[str, ...]
    remaining: tuple[str, ...]
    ok: bool
    escalated: bool


def flatten_all(
    execution: ExecutionPort,
    alerts,
    max_rounds: int = 3,
    sleep: Callable[[float], None] = _time.sleep,
    pause_s: float = 2.0,
) -> FlattenReport:
    closed: list[str] = []
    rounds = 0
    for rounds in range(1, max_rounds + 1):
        positions = execution.positions()
        if not positions:
            return FlattenReport(rounds, tuple(closed), (), ok=True, escalated=False)
        for p in positions:
            result = execution.close_position(p.ticket)
            if result.ok:
                closed.append(p.ticket)
        if not execution.positions():  # verify flat against the broker, not our intent
            return FlattenReport(rounds, tuple(closed), (), ok=True, escalated=False)
        sleep(pause_s)

    remaining = tuple(p.ticket for p in execution.positions())
    alerts.alert(
        "critical",
        f"FLATTENER FAILED after {max_rounds} rounds; positions remain: {remaining}. "
        "Manual intervention required NOW (close at any spread / call broker).",
    )
    return FlattenReport(max_rounds, tuple(closed), remaining, ok=False, escalated=True)


def main() -> None:  # pragma: no cover - thin process wrapper around flatten_all
    import argparse

    from services.alerts import AlertService, LogSink

    ap = argparse.ArgumentParser(description="Independent position flattener")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    args = ap.parse_args()

    alerts = AlertService([LogSink()])
    if args.mode == "paper":
        print("paper mode: no standalone paper broker state to flatten; "
              "the trading process owns the paper book. Exiting OK.")
        return
    from adapters.mt5.adapter import MT5Adapter, connect_from_env

    adapter = connect_from_env(MT5Adapter)
    report = flatten_all(adapter, alerts)
    print(report)
    raise SystemExit(0 if report.ok else 2)


if __name__ == "__main__":  # pragma: no cover
    main()
