"""ClockPort (SPEC.md §10.3). The pure core never reads a clock; shells inject time."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class ClockPort(Protocol):
    def now_utc(self) -> datetime:
        """Current time as an aware UTC datetime."""
        ...
