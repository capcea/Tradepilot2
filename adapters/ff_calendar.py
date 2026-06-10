"""Economic calendar adapter: ForexFactory weekly JSON + manual CSV fallback (SPEC.md §6.1).

The blackout logic itself is pure and lives in core.filters (M2); this adapter
only acquires and normalizes events. Event ids are content-derived so repeated
fetches upsert instead of duplicating.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import httpx

from core.events import EconEvent

FF_THISWEEK_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
UTC = timezone.utc

_IMPACTS = {"high": "high", "medium": "medium", "low": "low", "holiday": "holiday"}


def _event_id(ts_utc: datetime, currency: str, title: str) -> str:
    key = f"{ts_utc.isoformat()}|{currency}|{title}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _to_utc(raw: str) -> datetime:
    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError(f"naive timestamp in calendar data: {raw!r}")
    return ts.astimezone(UTC)


def parse_ff_json(text: str, source: str = "forexfactory") -> tuple[EconEvent, ...]:
    events = []
    for item in json.loads(text):
        ts = _to_utc(item["date"])
        currency = item["country"].strip().upper()
        title = item["title"].strip()
        impact = _IMPACTS.get(item.get("impact", "").strip().lower(), "unknown")
        events.append(
            EconEvent(
                id=_event_id(ts, currency, title),
                ts_utc=ts,
                currency=currency,
                impact=impact,
                title=title,
                source=source,
            )
        )
    events.sort(key=lambda e: e.ts_utc)
    return tuple(events)


def parse_calendar_csv(text: str, source: str = "csv") -> tuple[EconEvent, ...]:
    reader = csv.DictReader(io.StringIO(text))
    required = {"ts_utc", "currency", "impact", "title"}
    if reader.fieldnames is None or not required.issubset(set(reader.fieldnames)):
        raise ValueError(f"calendar CSV must have columns {sorted(required)}")
    events = []
    for row in reader:
        if not any(v.strip() for v in row.values() if v):
            continue
        ts = _to_utc(row["ts_utc"].strip())
        currency = row["currency"].strip().upper()
        title = row["title"].strip()
        impact = _IMPACTS.get(row["impact"].strip().lower(), "unknown")
        events.append(
            EconEvent(
                id=_event_id(ts, currency, title),
                ts_utc=ts,
                currency=currency,
                impact=impact,
                title=title,
                source=source,
            )
        )
    events.sort(key=lambda e: e.ts_utc)
    return tuple(events)


class FileCalendar:
    """CalendarPort over a fixed event list (CSV fallback / backtests)."""

    def __init__(self, events: Sequence[EconEvent]):
        self._events = tuple(sorted(events, key=lambda e: e.ts_utc))

    @classmethod
    def from_csv(cls, path: Path | str) -> "FileCalendar":
        return cls(parse_calendar_csv(Path(path).read_text(encoding="utf-8")))

    def events_between(self, start_utc: datetime, end_utc: datetime) -> Sequence[EconEvent]:
        return [e for e in self._events if start_utc <= e.ts_utc < end_utc]


class FFCalendarHTTP:
    """CalendarPort over the ForexFactory weekly feed; refresh() is explicit so
    the scheduling/caching policy (06:00 London fetch, hourly re-check, §6.1)
    stays in the service layer."""

    def __init__(self, http: httpx.Client | None = None, url: str = FF_THISWEEK_URL):
        self.http = http or httpx.Client(timeout=30.0, follow_redirects=True)
        self.url = url
        self._events: tuple[EconEvent, ...] = ()

    def refresh(self) -> tuple[EconEvent, ...]:
        resp = self.http.get(self.url)
        resp.raise_for_status()
        self._events = parse_ff_json(resp.text)
        return self._events

    def events_between(self, start_utc: datetime, end_utc: datetime) -> Sequence[EconEvent]:
        return [e for e in self._events if start_utc <= e.ts_utc < end_utc]
