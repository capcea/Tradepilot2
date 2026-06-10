import copy
import pathlib

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIGS = ROOT / "configs"


def _load(name: str) -> dict:
    return yaml.safe_load((CONFIGS / name).read_text(encoding="utf-8"))


@pytest.fixture
def configs_dir() -> pathlib.Path:
    return CONFIGS


@pytest.fixture
def strategy_dict() -> dict:
    """Deep copy of the shipped strategy.yaml, safe to mutate per-test."""
    return copy.deepcopy(_load("strategy.yaml"))


@pytest.fixture
def firm_dict() -> dict:
    """Deep copy of the shipped firm_profile.yaml payload (root key stripped)."""
    return copy.deepcopy(_load("firm_profile.yaml")["firm_profile"])


@pytest.fixture
def instruments_dict() -> dict:
    return copy.deepcopy(_load("instruments.yaml"))


@pytest.fixture(scope="session")
def risk_cfg_session():
    """Session-scoped (immutable) risk config for hypothesis property tests."""
    from core.config_schema import StrategyConfig

    return StrategyConfig.model_validate(_load("strategy.yaml")).risk


@pytest.fixture(scope="session")
def firm_profile_session():
    from core.config_schema import FirmProfile

    return FirmProfile.model_validate(_load("firm_profile.yaml")["firm_profile"])
