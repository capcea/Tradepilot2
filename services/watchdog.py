"""Watchdog (SPEC.md §19 rows 1, 10, 16): staleness + clock-skew assessment.

Produces the core's OpsHealth flags; the filter pipeline turns them into
NO-TRADE reason codes. A failed NTP query is alert-worthy but does NOT set the
skew flag by itself (unknown is not the same as wrong; halting on every NTP
timeout would be a self-inflicted outage).
"""
from __future__ import annotations

import socket
import struct
from dataclasses import dataclass
from datetime import datetime

from core.filters import OpsHealth
from ports.clock import ClockPort

_NTP_EPOCH_OFFSET = 2208988800  # 1900-01-01 -> 1970-01-01 in seconds


@dataclass(frozen=True, slots=True)
class WatchdogConfig:
    tick_stale_s: float = 10.0   # §6.10
    bar_stale_s: float = 420.0   # one M5 bar + 2 min grace
    clock_skew_s: float = 2.0    # §6.10


class Watchdog:
    def __init__(self, clock: ClockPort, cfg: WatchdogConfig = WatchdogConfig()):
        self.clock = clock
        self.cfg = cfg

    def assess(
        self,
        last_tick_ts: datetime | None,
        last_bar_close_ts: datetime | None,
        ntp_offset_s: float | None,
        reconnecting: bool = False,
        kill: bool = False,
        pause: bool = False,
    ) -> OpsHealth:
        now = self.clock.now_utc()
        stale = (
            last_tick_ts is None
            or (now - last_tick_ts).total_seconds() > self.cfg.tick_stale_s
            or last_bar_close_ts is None
            or (now - last_bar_close_ts).total_seconds() > self.cfg.bar_stale_s
        )
        skew = ntp_offset_s is not None and abs(ntp_offset_s) > self.cfg.clock_skew_s
        return OpsHealth(
            stale_tick=stale, clock_skew=skew,
            reconnecting=reconnecting, kill=kill, pause=pause,
        )


def sntp_offset(server: str = "time.windows.com", timeout: float = 2.0) -> float | None:
    """Local-clock offset vs an SNTP server in seconds; None if unreachable.
    Stdlib-only on purpose (no new dependency for 20 lines of protocol)."""
    packet = b"\x1b" + 47 * b"\0"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            import time as _t

            t0 = _t.time()
            s.sendto(packet, (server, 123))
            data, _ = s.recvfrom(48)
            t3 = _t.time()
    except OSError:
        return None
    if len(data) < 48:
        return None
    tx_secs, tx_frac = struct.unpack("!II", data[40:48])
    server_time = tx_secs - _NTP_EPOCH_OFFSET + tx_frac / 2**32
    midpoint = (t0 + t3) / 2
    return server_time - midpoint
