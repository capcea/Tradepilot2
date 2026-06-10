"""M0 timebase tests (SPEC.md §3, §6.9, §13.2, M0 exit gate).

Pinned IANA-tzdb facts used below:
  US DST: second Sunday of March -> first Sunday of November (02:00 local NY).
  UK BST: last Sunday of March -> last Sunday of October (01:00 UTC).
  Mismatch windows (clocks out of sync):
    2024: Mar 10 - Mar 30 inclusive; Oct 27 - Nov 2 inclusive.
    2025: Mar 9  - Mar 29 inclusive; Oct 26 - Nov 1 inclusive.
    2026: Mar 8  - Mar 28 inclusive.
"""
from datetime import date, datetime, time, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.timebase import (
    DEFAULT_SESSION_TIMES,
    LONDON,
    NEW_YORK,
    SessionTimesConfig,
    is_us_uk_dst_mismatch,
    self_check,
    session_windows,
)

UTC = timezone.utc


def u(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=UTC)


def _is_london_transition_day(d: date) -> bool:
    early = datetime(d.year, d.month, d.day, 0, 0, tzinfo=LONDON).utcoffset()
    late = datetime(d.year, d.month, d.day, 23, 0, tzinfo=LONDON).utcoffset()
    return early != late


# ---------------------------------------------------------------------------
# Pinned dates: winter (GMT/EST), summer (BST/EDT), and both mismatch windows
# ---------------------------------------------------------------------------

class TestWinterPinned:
    """2025-01-15 (Wed): London=UTC+0, NY=UTC-5. Both on standard time."""

    w = None

    @classmethod
    def setup_class(cls):
        cls.w = session_windows(date(2025, 1, 15))

    def test_asian_window_utc(self):
        assert self.w.asian_start_utc == u(2025, 1, 15, 0, 0)
        assert self.w.asian_end_utc == u(2025, 1, 15, 6, 55)

    def test_entry_window_utc(self):
        assert self.w.entry_start_utc == u(2025, 1, 15, 7, 5)
        assert self.w.entry_end_utc == u(2025, 1, 15, 10, 30)

    def test_forced_exit_utc(self):
        assert self.w.forced_exit_utc == u(2025, 1, 15, 19, 30)

    def test_rollover_and_reset_utc(self):
        assert self.w.rollover_start_utc == u(2025, 1, 15, 21, 45)
        assert self.w.rollover_end_utc == u(2025, 1, 15, 22, 15)
        assert self.w.daily_reset_utc == u(2025, 1, 15, 22, 0)

    def test_not_friday(self):
        assert self.w.friday_no_new_after_utc is None

    def test_no_mismatch_and_self_check_clean(self):
        assert is_us_uk_dst_mismatch(date(2025, 1, 15)) is False
        assert self_check(self.w) == ()


class TestSummerPinned:
    """2025-07-16 (Wed): London=UTC+1 (BST), NY=UTC-4 (EDT). Both on DST."""

    w = None

    @classmethod
    def setup_class(cls):
        cls.w = session_windows(date(2025, 7, 16))

    def test_asian_window_utc_rolls_to_previous_utc_day(self):
        assert self.w.asian_start_utc == u(2025, 7, 15, 23, 0)
        assert self.w.asian_end_utc == u(2025, 7, 16, 5, 55)

    def test_entry_window_utc(self):
        assert self.w.entry_start_utc == u(2025, 7, 16, 6, 5)
        assert self.w.entry_end_utc == u(2025, 7, 16, 9, 30)

    def test_forced_exit_utc(self):
        assert self.w.forced_exit_utc == u(2025, 7, 16, 18, 30)

    def test_rollover_and_reset_utc(self):
        assert self.w.rollover_start_utc == u(2025, 7, 16, 20, 45)
        assert self.w.rollover_end_utc == u(2025, 7, 16, 21, 15)
        assert self.w.daily_reset_utc == u(2025, 7, 16, 21, 0)

    def test_no_mismatch_and_self_check_clean(self):
        assert is_us_uk_dst_mismatch(date(2025, 7, 16)) is False
        assert self_check(self.w) == ()


class TestMarchMismatchPinned:
    """2025-03-12 (Wed): US already on EDT (from Mar 9), UK still on GMT (until Mar 30).
    London-NY gap is 4h instead of the usual 5h."""

    w = None

    @classmethod
    def setup_class(cls):
        cls.w = session_windows(date(2025, 3, 12))

    def test_range_lock_unmoved_in_utc(self):
        # London still GMT -> 06:55 London == 06:55 UTC even though NY shifted.
        assert self.w.asian_end_utc == u(2025, 3, 12, 6, 55)
        assert self.w.entry_start_utc == u(2025, 3, 12, 7, 5)
        assert self.w.forced_exit_utc == u(2025, 3, 12, 19, 30)

    def test_ny_anchored_times_shifted(self):
        assert self.w.rollover_start_utc == u(2025, 3, 12, 20, 45)
        assert self.w.rollover_end_utc == u(2025, 3, 12, 21, 15)
        assert self.w.daily_reset_utc == u(2025, 3, 12, 21, 0)

    def test_mismatch_flag_and_self_check_clean(self):
        # Mismatch is flagged (§6.9) but window math is still valid -> self-check clean.
        assert is_us_uk_dst_mismatch(date(2025, 3, 12)) is True
        assert self_check(self.w) == ()


class TestAutumnMismatchPinned:
    """2025-10-29 (Wed): UK back on GMT (from Oct 26), US still on EDT (until Nov 2)."""

    w = None

    @classmethod
    def setup_class(cls):
        cls.w = session_windows(date(2025, 10, 29))

    def test_range_lock_unmoved_in_utc(self):
        assert self.w.asian_end_utc == u(2025, 10, 29, 6, 55)
        assert self.w.forced_exit_utc == u(2025, 10, 29, 19, 30)

    def test_ny_anchored_times_shifted(self):
        assert self.w.daily_reset_utc == u(2025, 10, 29, 21, 0)

    def test_mismatch_flag_and_self_check_clean(self):
        assert is_us_uk_dst_mismatch(date(2025, 10, 29)) is True
        assert self_check(self.w) == ()


class TestMismatch2024Pinned:
    def test_march_2024(self):
        w = session_windows(date(2024, 3, 20))
        assert w.asian_end_utc == u(2024, 3, 20, 6, 55)
        assert w.daily_reset_utc == u(2024, 3, 20, 21, 0)
        assert is_us_uk_dst_mismatch(date(2024, 3, 20)) is True

    def test_autumn_2024(self):
        w = session_windows(date(2024, 10, 30))
        assert w.asian_end_utc == u(2024, 10, 30, 6, 55)
        assert w.daily_reset_utc == u(2024, 10, 30, 21, 0)
        assert is_us_uk_dst_mismatch(date(2024, 10, 30)) is True


# ---------------------------------------------------------------------------
# Mismatch-detector boundaries (first/last day of each out-of-sync window)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2025, 3, 8), False), (date(2025, 3, 9), True),
        (date(2025, 3, 29), True), (date(2025, 3, 30), False),
        (date(2025, 10, 25), False), (date(2025, 10, 26), True),
        (date(2025, 11, 1), True), (date(2025, 11, 2), False),
        (date(2024, 3, 9), False), (date(2024, 3, 10), True),
        (date(2024, 3, 30), True), (date(2024, 3, 31), False),
        (date(2024, 10, 26), False), (date(2024, 10, 27), True),
        (date(2024, 11, 2), True), (date(2024, 11, 3), False),
        (date(2026, 3, 7), False), (date(2026, 3, 8), True),
        (date(2026, 3, 28), True), (date(2026, 3, 29), False),
    ],
)
def test_mismatch_window_boundaries(day, expected):
    assert is_us_uk_dst_mismatch(day) is expected


# ---------------------------------------------------------------------------
# Friday cutoff (§6.7: no new entries after 16:00 London on Fridays)
# ---------------------------------------------------------------------------

def test_friday_cutoff_winter():
    w = session_windows(date(2025, 1, 17))  # Friday, GMT
    assert w.friday_no_new_after_utc == u(2025, 1, 17, 16, 0)


def test_friday_cutoff_summer():
    w = session_windows(date(2025, 7, 18))  # Friday, BST
    assert w.friday_no_new_after_utc == u(2025, 7, 18, 15, 0)


def test_friday_cutoff_absent_midweek():
    assert session_windows(date(2025, 7, 16)).friday_no_new_after_utc is None


# ---------------------------------------------------------------------------
# Self-check: DST-transition Sundays must FAIL (-> NO-TRADE day per §6.9),
# normal days must pass.
# ---------------------------------------------------------------------------

def test_self_check_fails_on_spring_forward_sunday():
    # 2025-03-30: London jumps 01:00 GMT -> 02:00 BST inside the Asian window;
    # the 00:00-06:55 window is only 5h55m of real time.
    w = session_windows(date(2025, 3, 30))
    violations = self_check(w)
    assert violations, "spring-forward Sunday must fail self-check"
    assert any(v.startswith("ASIAN_WINDOW_DURATION_MISMATCH") for v in violations)


def test_self_check_fails_on_fall_back_sunday():
    # 2025-10-26: the 01:00-02:00 BST hour repeats; Asian window is 7h55m.
    w = session_windows(date(2025, 10, 26))
    violations = self_check(w)
    assert violations, "fall-back Sunday must fail self-check"
    assert any(v.startswith("ASIAN_WINDOW_DURATION_MISMATCH") for v in violations)


# ---------------------------------------------------------------------------
# Range lock at 06:55 London YEAR-ROUND (M0 exit gate)
# ---------------------------------------------------------------------------

def test_range_lock_0655_london_every_day_2024_2025():
    d = date(2024, 1, 1)
    while d <= date(2025, 12, 31):
        w = session_windows(d)
        local = w.asian_end_utc.astimezone(LONDON)
        assert local.time() == time(6, 55), f"range lock drifted on {d}: {local}"
        assert local.date() == d
        d += timedelta(days=1)


@settings(deadline=None, max_examples=200)
@given(st.dates(min_value=date(2018, 1, 1), max_value=date(2030, 12, 31)))
def test_range_lock_wall_time_property(d):
    w = session_windows(d)
    local = w.asian_end_utc.astimezone(LONDON)
    assert local.time() == time(6, 55)
    assert local.date() == d


@settings(deadline=None, max_examples=200)
@given(st.dates(min_value=date(2018, 1, 1), max_value=date(2030, 12, 31)))
def test_daily_reset_wall_time_property(d):
    w = session_windows(d)
    local = w.daily_reset_utc.astimezone(NEW_YORK)
    assert local.time() == time(17, 0)
    assert local.date() == d


@settings(deadline=None, max_examples=200)
@given(st.dates(min_value=date(2018, 1, 1), max_value=date(2030, 12, 31)))
def test_self_check_clean_iff_not_london_transition_day(d):
    w = session_windows(d)
    if _is_london_transition_day(d):
        assert self_check(w) != ()
    else:
        assert self_check(w) == ()


# ---------------------------------------------------------------------------
# Ordering invariants and config plumbing
# ---------------------------------------------------------------------------

@settings(deadline=None, max_examples=100)
@given(st.dates(min_value=date(2018, 1, 1), max_value=date(2030, 12, 31)))
def test_window_ordering_property(d):
    w = session_windows(d)
    assert (
        w.asian_start_utc < w.asian_end_utc <= w.entry_start_utc
        < w.entry_end_utc < w.forced_exit_utc
        < w.rollover_start_utc <= w.daily_reset_utc <= w.rollover_end_utc
    )


def test_custom_session_times_respected():
    times = SessionTimesConfig(asian_end=time(6, 0), entry_start=time(6, 10))
    w = session_windows(date(2025, 1, 15), times)
    assert w.asian_end_utc == u(2025, 1, 15, 6, 0)
    assert w.entry_start_utc == u(2025, 1, 15, 6, 10)


def test_default_times_match_spec():
    t = DEFAULT_SESSION_TIMES
    assert t.asian_start == time(0, 0)
    assert t.asian_end == time(6, 55)
    assert t.entry_start == time(7, 5)
    assert t.entry_end == time(10, 30)
    assert t.forced_exit == time(19, 30)
    assert t.friday_no_new_after == time(16, 0)
    assert t.rollover_start_ny == time(16, 45)
    assert t.rollover_end_ny == time(17, 15)
    assert t.daily_reset_ny == time(17, 0)
