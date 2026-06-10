"""Config file loading: YAML -> validated models + governance checksum (SPEC.md §8c).

This is the I/O shell around the pure schemas in core.config_schema. Checksums
are sha256 over newline-normalized text so the same logical config hashes
identically from Windows and Unix checkouts; the checksum is what gets pinned
in the config_version DB row and re-verified by the live-trading arming gate.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from core.config_schema import FirmProfile, InstrumentsConfig, StrategyConfig


class ConfigFileError(ValueError):
    """File missing/unreadable/unparseable — distinct from schema validation errors."""


def checksum_text(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _read_yaml(path: Path | str) -> tuple[dict, str]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigFileError(f"cannot read config file {p}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigFileError(f"invalid YAML in {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigFileError(f"config root must be a mapping in {p}")
    return data, checksum_text(text)


def load_strategy_config(path: Path | str) -> tuple[StrategyConfig, str]:
    data, checksum = _read_yaml(path)
    return StrategyConfig.model_validate(data), checksum


def load_firm_profile(path: Path | str) -> tuple[FirmProfile, str]:
    data, checksum = _read_yaml(path)
    if "firm_profile" not in data:
        raise ConfigFileError(f"missing 'firm_profile' root key in {path}")
    return FirmProfile.model_validate(data["firm_profile"]), checksum


def load_instruments(path: Path | str) -> tuple[InstrumentsConfig, str]:
    data, checksum = _read_yaml(path)
    return InstrumentsConfig.model_validate(data), checksum
