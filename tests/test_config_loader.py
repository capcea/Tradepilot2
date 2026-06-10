"""M0 config-loader tests: YAML -> validated models + governance checksum (SPEC.md §8c)."""
from decimal import Decimal

import pytest

from core.config_schema import FirmProfile, InstrumentsConfig, StrategyConfig
from services.config_loader import (
    ConfigFileError,
    checksum_text,
    load_firm_profile,
    load_instruments,
    load_strategy_config,
)


def test_load_strategy_config(configs_dir):
    cfg, checksum = load_strategy_config(configs_dir / "strategy.yaml")
    assert isinstance(cfg, StrategyConfig)
    assert cfg.risk.per_trade_usd == Decimal("175")
    assert len(checksum) == 64  # sha256 hex


def test_load_firm_profile(configs_dir):
    firm, checksum = load_firm_profile(configs_dir / "firm_profile.yaml")
    assert isinstance(firm, FirmProfile)
    assert len(checksum) == 64


def test_load_instruments(configs_dir):
    instruments, checksum = load_instruments(configs_dir / "instruments.yaml")
    assert isinstance(instruments, InstrumentsConfig)
    assert len(checksum) == 64


def test_checksum_is_deterministic(configs_dir):
    _, a = load_strategy_config(configs_dir / "strategy.yaml")
    _, b = load_strategy_config(configs_dir / "strategy.yaml")
    assert a == b


def test_checksum_changes_with_content():
    assert checksum_text("a: 1\n") != checksum_text("a: 2\n")


def test_checksum_is_newline_invariant():
    # The same logical config must hash identically from Windows and Unix checkouts.
    assert checksum_text("a: 1\r\nb: 2\r\n") == checksum_text("a: 1\nb: 2\n")


def test_firm_profile_requires_root_key(tmp_path):
    p = tmp_path / "firm.yaml"
    p.write_text("name: x\n", encoding="utf-8")
    with pytest.raises(ConfigFileError):
        load_firm_profile(p)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigFileError):
        load_strategy_config(tmp_path / "nope.yaml")


def test_invalid_yaml_raises(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("strategy: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigFileError):
        load_strategy_config(p)
