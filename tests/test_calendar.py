"""M1 economic-calendar tests: FF JSON parse, CSV fallback, windowing (SPEC.md §6.1)."""
from datetime import datetime, timezone

import pytest

from adapters.ff_calendar import FileCalendar, parse_calendar_csv, parse_ff_json

UTC = timezone.utc

FF_JSON = """
[
  {"title": "Non-Farm Employment Change", "country": "USD",
   "date": "2026-06-05T08:30:00-04:00", "impact": "High",
   "forecast": "180K", "previous": "175K"},
  {"title": "German Factory Orders", "country": "EUR",
   "date": "2026-06-05T02:00:00-04:00", "impact": "Medium",
   "forecast": "", "previous": ""},
  {"title": "Bank Holiday", "country": "GBP",
   "date": "2026-06-08T00:00:00-04:00", "impact": "Holiday",
   "forecast": "", "previous": ""}
]
"""

CSV = """ts_utc,currency,impact,title
2026-06-05T12:30:00+00:00,USD,high,Non-Farm Employment Change
2026-06-05T06:00:00Z,EUR,medium,German Factory Orders
"""


def test_parse_ff_json_converts_to_utc():
    events = parse_ff_json(FF_JSON)
    nfp = next(e for e in events if "Non-Farm" in e.title)
    assert nfp.ts_utc == datetime(2026, 6, 5, 12, 30, tzinfo=UTC)
    assert nfp.currency == "USD"
    assert nfp.impact == "high"
    assert nfp.source == "forexfactory"


def test_parse_ff_json_impact_normalization():
    events = parse_ff_json(FF_JSON)
    impacts = {e.title: e.impact for e in events}
    assert impacts["German Factory Orders"] == "medium"
    assert impacts["Bank Holiday"] == "holiday"


def test_event_ids_are_stable_across_parses():
    a = parse_ff_json(FF_JSON)
    b = parse_ff_json(FF_JSON)
    assert [e.id for e in a] == [e.id for e in b]
    assert len({e.id for e in a}) == len(a)


def test_parse_csv_fallback():
    events = parse_calendar_csv(CSV)
    assert len(events) == 2
    # parser contract: events come back sorted by ts_utc regardless of file order
    assert events[0].ts_utc == datetime(2026, 6, 5, 6, 0, tzinfo=UTC)
    assert events[0].impact == "medium"
    assert events[1].ts_utc == datetime(2026, 6, 5, 12, 30, tzinfo=UTC)
    assert events[1].impact == "high"


def test_parse_csv_rejects_bad_header():
    with pytest.raises(ValueError):
        parse_calendar_csv("when,what\n2026-01-01,thing\n")


def test_parse_csv_rejects_naive_timestamps():
    with pytest.raises(ValueError):
        parse_calendar_csv("ts_utc,currency,impact,title\n2026-06-05T12:30:00,USD,high,NFP\n")


def test_file_calendar_events_between():
    cal = FileCalendar(parse_ff_json(FF_JSON))
    window = cal.events_between(
        datetime(2026, 6, 5, 0, 0, tzinfo=UTC), datetime(2026, 6, 6, 0, 0, tzinfo=UTC)
    )
    assert {e.currency for e in window} == {"USD", "EUR"}
    none = cal.events_between(
        datetime(2026, 6, 9, 0, 0, tzinfo=UTC), datetime(2026, 6, 10, 0, 0, tzinfo=UTC)
    )
    assert list(none) == []
