"""UTC/session timebase (SPEC.md §3, §6.9, §13.2).

Pure module: no I/O, no clocks, no randomness. Every function is a deterministic
mapping from a calendar date (plus optional wall-time config) to UTC instants.

All session anchors are defined in local wall time — London for the trading
session, New York for rollover and the firm's daily reset — and converted to
UTC per date via the IANA database. Nothing here may assume a fixed UTC offset.

`self_check` exists because §6.9 demands NO-TRADE over guessing: any day whose
computed windows are not exactly the configured wall times and durations (DST
transition days, tz-database surprises) must be flagged, not repaired.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc

_FRIDAY = 4
# (London, New York) whole-hour UTC offsets when both regions are in sync.
_NORMAL_OFFSET_PAIRS = {(0, -5), (1, -4)}
_LONDON_OFFSETS = {0, 1}
_NEW_YORK_OFFSETS = {-5, -4}
_DUMMY = date(2001, 1, 1)


@dataclass(frozen=True)
class SessionTimesConfig:
    """Wall-time session anchors. London times unless suffixed _ny."""

    asian_start: time = time(0, 0)
    asian_end: time = time(6, 55)          # range lock (§3.1)
    entry_start: time = time(7, 5)
    entry_end: time = time(10, 30)
    forced_exit: time = time(19, 30)
    friday_no_new_after: time = time(16, 0)  # §6.7
    rollover_start_ny: time = time(16, 45)   # §6.7 rollover window
    rollover_end_ny: time = time(17, 15)
    daily_reset_ny: time = time(17, 0)       # firm daily-loss reset (§8)


DEFAULT_SESSION_TIMES = SessionTimesConfig()


@dataclass(frozen=True)
class SessionWindows:
    """All anchors for one London trading day, resolved to UTC."""

    trading_day: date
    asian_start_utc: datetime
    asian_end_utc: datetime
    entry_start_utc: datetime
    entry_end_utc: datetime
    forced_exit_utc: datetime
    friday_no_new_after_utc: datetime | None
    rollover_start_utc: datetime
    rollover_end_utc: datetime
    daily_reset_utc: datetime


def _wall_to_utc(day: date, t: time, tz: ZoneInfo) -> datetime:
    return datetime(day.year, day.month, day.day, t.hour, t.minute, tzinfo=tz).astimezone(UTC)


def _naive_delta(start: time, end: time) -> timedelta:
    return datetime.combine(_DUMMY, end) - datetime.combine(_DUMMY, start)


def _offset_hours(dt_utc: datetime, tz: ZoneInfo) -> int:
    return int(dt_utc.astimezone(tz).utcoffset().total_seconds() // 3600)


def session_windows(day: date, times: SessionTimesConfig = DEFAULT_SESSION_TIMES) -> SessionWindows:
    friday_cutoff = (
        _wall_to_utc(day, times.friday_no_new_after, LONDON) if day.weekday() == _FRIDAY else None
    )
    return SessionWindows(
        trading_day=day,
        asian_start_utc=_wall_to_utc(day, times.asian_start, LONDON),
        asian_end_utc=_wall_to_utc(day, times.asian_end, LONDON),
        entry_start_utc=_wall_to_utc(day, times.entry_start, LONDON),
        entry_end_utc=_wall_to_utc(day, times.entry_end, LONDON),
        forced_exit_utc=_wall_to_utc(day, times.forced_exit, LONDON),
        friday_no_new_after_utc=friday_cutoff,
        rollover_start_utc=_wall_to_utc(day, times.rollover_start_ny, NEW_YORK),
        rollover_end_utc=_wall_to_utc(day, times.rollover_end_ny, NEW_YORK),
        daily_reset_utc=_wall_to_utc(day, times.daily_reset_ny, NEW_YORK),
    )


def self_check(
    w: SessionWindows, times: SessionTimesConfig = DEFAULT_SESSION_TIMES
) -> tuple[str, ...]:
    """Return violation codes; empty tuple means the day's windows are trustworthy.

    Any non-empty result must be treated as a NO-TRADE day (§6.9).
    """
    violations: list[str] = []

    ordering = (
        ("asian_start", w.asian_start_utc),
        ("asian_end", w.asian_end_utc),
        ("entry_start", w.entry_start_utc),
        ("entry_end", w.entry_end_utc),
        ("forced_exit", w.forced_exit_utc),
        ("rollover_start", w.rollover_start_utc),
        ("daily_reset", w.daily_reset_utc),
        ("rollover_end", w.rollover_end_utc),
    )
    for (name_a, a), (name_b, b) in zip(ordering, ordering[1:]):
        # asian_end == entry_start and reset == rollover edge are legal; strict
        # ordering is only demanded where a gap is structural.
        if a > b or (a == b and (name_a, name_b) not in {
            ("asian_end", "entry_start"),
            ("rollover_start", "daily_reset"),
            ("daily_reset", "rollover_end"),
        }):
            violations.append(f"ORDERING_VIOLATION:{name_a}>={name_b}")

    durations = (
        ("ASIAN_WINDOW_DURATION_MISMATCH",
         _naive_delta(times.asian_start, times.asian_end),
         w.asian_end_utc - w.asian_start_utc),
        ("ENTRY_WINDOW_DURATION_MISMATCH",
         _naive_delta(times.entry_start, times.entry_end),
         w.entry_end_utc - w.entry_start_utc),
        ("ROLLOVER_WINDOW_DURATION_MISMATCH",
         _naive_delta(times.rollover_start_ny, times.rollover_end_ny),
         w.rollover_end_utc - w.rollover_start_utc),
    )
    for code, expected, actual in durations:
        if expected != actual:
            violations.append(f"{code}:expected={expected},actual={actual}")

    roundtrips = [
        ("asian_start", w.asian_start_utc, LONDON, times.asian_start),
        ("asian_end", w.asian_end_utc, LONDON, times.asian_end),
        ("entry_start", w.entry_start_utc, LONDON, times.entry_start),
        ("entry_end", w.entry_end_utc, LONDON, times.entry_end),
        ("forced_exit", w.forced_exit_utc, LONDON, times.forced_exit),
        ("rollover_start", w.rollover_start_utc, NEW_YORK, times.rollover_start_ny),
        ("rollover_end", w.rollover_end_utc, NEW_YORK, times.rollover_end_ny),
        ("daily_reset", w.daily_reset_utc, NEW_YORK, times.daily_reset_ny),
    ]
    if w.friday_no_new_after_utc is not None:
        roundtrips.append(
            ("friday_no_new_after", w.friday_no_new_after_utc, LONDON, times.friday_no_new_after)
        )
    for name, dt_utc, tz, expected_wall in roundtrips:
        local = dt_utc.astimezone(tz)
        if local.time() != expected_wall or local.date() != w.trading_day:
            violations.append(f"WALL_CLOCK_ROUNDTRIP_FAILED:{name}")

    for name, dt_utc, tz, _ in roundtrips:
        allowed = _LONDON_OFFSETS if tz is LONDON else _NEW_YORK_OFFSETS
        if _offset_hours(dt_utc, tz) not in allowed:
            violations.append(f"UTC_OFFSET_ANOMALY:{name}")

    return tuple(violations)


def is_us_uk_dst_mismatch(day: date) -> bool:
    """True during the weeks when US and UK clocks are out of sync (§6.9).

    Probed at noon London so the answer is stable for the whole trading session
    regardless of which side transitioned overnight.
    """
    probe = _wall_to_utc(day, time(12, 0), LONDON)
    pair = (_offset_hours(probe, LONDON), _offset_hours(probe, NEW_YORK))
    return pair not in _NORMAL_OFFSET_PAIRS
