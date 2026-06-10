"""Live-trading arming gate (build-brief non-negotiable).

Live orders are refused unless ALL THREE hold:
  1. env LIVE_TRADING == "1"
  2. the file ./ARM_LIVE exists
  3. the loaded config's checksum matches the DB's active config_version row
Default mode everywhere is paper; this gate exists so going live is always a
deliberate, three-factor act that survives accidental restarts and stale configs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class LiveGateResult:
    allowed: bool
    reasons: tuple[str, ...]


class LiveGate:
    def __init__(
        self,
        store,
        expected_checksum: str,
        arm_file: Path | str = Path("ARM_LIVE"),
        env: Mapping[str, str] | None = None,
    ):
        self.store = store
        self.expected_checksum = expected_checksum
        self.arm_file = Path(arm_file)
        self.env = env if env is not None else os.environ

    def check(self) -> LiveGateResult:
        reasons: list[str] = []
        if self.env.get("LIVE_TRADING") != "1":
            reasons.append("LIVE_TRADING env var is not '1'")
        if not self.arm_file.exists():
            reasons.append(f"ARM_LIVE file missing ({self.arm_file})")
        active = self.store.get_active_config()
        if active is None or active.checksum != self.expected_checksum:
            reasons.append("config checksum does not match DB active config_version row")
        return LiveGateResult(allowed=not reasons, reasons=tuple(reasons))
