"""Alert service (SPEC.md §10.2): fan-out to sinks; Telegram + structured log.

Sink failures are swallowed after logging — an alert pipeline that can crash
the trading process is worse than a missed message.
"""
from __future__ import annotations

from typing import Protocol, Sequence

import structlog

LEVELS = {"info": 0, "warning": 1, "error": 2, "critical": 3}
_log = structlog.get_logger("alerts")


class AlertSink(Protocol):
    def send(self, level: str, message: str) -> None: ...


class LogSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def send(self, level: str, message: str) -> None:
        self.records.append((level, message))
        _log.warning("alert", level=level, message=message)


def telegram_payload(token: str, chat_id: str, level: str, message: str) -> tuple[str, dict]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    return url, {"chat_id": chat_id, "text": f"[{level.upper()}] {message}"}


class TelegramSink:
    def __init__(self, token: str, chat_id: str, http=None):
        import httpx

        self.token = token
        self.chat_id = chat_id
        self.http = http or httpx.Client(timeout=10.0)

    def send(self, level: str, message: str) -> None:
        url, data = telegram_payload(self.token, self.chat_id, level, message)
        self.http.post(url, data=data)


class AlertService:
    def __init__(self, sinks: Sequence[AlertSink], min_level: str = "info"):
        self.sinks = list(sinks)
        self.min_level = min_level

    def alert(self, level: str, message: str) -> None:
        if LEVELS.get(level, 0) < LEVELS[self.min_level]:
            return
        for sink in self.sinks:
            try:
                sink.send(level, message)
            except Exception as exc:  # noqa: BLE001 - alerting must never crash trading
                _log.error("alert_sink_failed", sink=type(sink).__name__, error=str(exc))
