"""CalendarPort (SPEC.md §10.3)."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from core.events import EconEvent


@runtime_checkable
class CalendarPort(Protocol):
    def events_between(self, start_utc: datetime, end_utc: datetime) -> Sequence[EconEvent]:
        """All known events with ts_utc in [start_utc, end_utc), any impact."""
        ...
