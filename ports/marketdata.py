"""MarketDataPort (SPEC.md §10.3)."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from core.events import M5Bar, TickBatch


@runtime_checkable
class MarketDataPort(Protocol):
    def get_m5_bars(self, symbol: str, start_utc: datetime, end_utc: datetime) -> Sequence[M5Bar]:
        """Closed M5 bars with ts_open_utc in [start_utc, end_utc)."""
        ...

    def latest_quote(self, symbol: str) -> TickBatch | None:
        """Most recent bid/ask, or None if no quote is available yet."""
        ...
