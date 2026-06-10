"""Config schemas and the startup validator (SPEC.md §3.1, §8, Appendix A).

Pure module: pydantic models and deterministic validation only — no file I/O
(loading lives in services.config_loader). All money and price fields are
Decimal; floats coming from YAML are converted through ``str`` so no binary
float artifact ever reaches money math.

Validation philosophy (§8a): refuse to construct anything incomplete or
internally inconsistent. ``validate_startup`` additionally proves the internal
risk buffers sit strictly inside the firm's limits — the engine must not start
otherwise.
"""
from __future__ import annotations

from datetime import time
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from core.timebase import SessionTimesConfig


def _to_decimal(v: object) -> object:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool):
        raise ValueError("boolean is not a valid numeric value")
    if isinstance(v, (int, float, str)):
        try:
            return Decimal(str(v))
        except InvalidOperation as exc:
            raise ValueError(f"not a valid decimal: {v!r}") from exc
    return v


# Money amounts and price/pip quantities. Same representation, two names for intent.
Money = Annotated[Decimal, BeforeValidator(_to_decimal)]
Px = Annotated[Decimal, BeforeValidator(_to_decimal)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------
# Strategy config (Appendix A)
# ---------------------------------------------------------------------------

class PairParams(_Model):
    range_min_pips: Px = Field(gt=0)
    sweep_min_pips: Px = Field(gt=0)
    stop_min: Px = Field(gt=0)
    stop_max: Px = Field(gt=0)
    spread_abs_cap: Px = Field(gt=0)
    max_deviation: Px = Field(gt=0)
    stop_buffer_pips: Px = Field(gt=0)  # §3.1: stop = sweep extreme ± (buffer + spread)

    @model_validator(mode="after")
    def _stop_bounds_ordered(self) -> "PairParams":
        if not self.stop_min < self.stop_max:
            raise ValueError("stop_min must be strictly below stop_max")
        return self


class SharedParams(_Model):
    range_max_adr_mult: Px = Field(gt=0, le=1)
    sweep_max_range_mult: Px = Field(gt=0, le=1)
    reclaim_bars: int = Field(ge=1)
    reclaim_quality_pct: Px = Field(gt=0, lt=1)
    tp1_r: Px = Field(gt=0)
    tp1_close: Px = Field(gt=0, le=1)
    tp2_r_cap: Px = Field(gt=0)
    time_stop_min: int = Field(gt=0)
    forced_exit_london: time
    entry_window_london: tuple[time, time]
    asian_window_london: tuple[time, time]
    spread_median_mult: Px = Field(ge=1)
    adr_pctile_skip: tuple[int, int]

    @model_validator(mode="after")
    def _windows_coherent(self) -> "SharedParams":
        asian_start, asian_end = self.asian_window_london
        entry_start, entry_end = self.entry_window_london
        if not asian_start < asian_end:
            raise ValueError("asian window must not wrap midnight (start < end)")
        if not entry_start < entry_end:
            raise ValueError("entry window start must precede end")
        if not asian_end <= entry_start:
            raise ValueError("asian window must close before the entry window opens")
        if not entry_end < self.forced_exit_london:
            raise ValueError("entry window must close before forced exit")
        if not self.tp2_r_cap > self.tp1_r:
            raise ValueError("tp2_r_cap must exceed tp1_r")
        lo, hi = self.adr_pctile_skip
        if not (0 <= lo < hi <= 100):
            raise ValueError("adr_pctile_skip must be ordered percentiles in [0, 100]")
        return self


class RiskConfig(_Model):
    # §7: 1R default $175, allowed range $150-$250; never increases after losses.
    per_trade_usd: Money = Field(ge=150, le=250)
    cooldown_usd: Money = Field(gt=0)
    day_soft_stop: Money = Field(lt=0)
    day_hard_stop: Money = Field(lt=0)
    week_stop: Money = Field(lt=0)
    consec_loss_halt: int = Field(ge=2, le=3)
    max_entries_day: int = Field(ge=1, le=3)
    max_concurrent: int = Field(ge=1, le=1)  # EU/GU are one risk unit in v1 (§2.1)
    consistency_day_cap: Money = Field(gt=0)
    floor_buffer: Money = Field(gt=0)

    @model_validator(mode="after")
    def _stops_nested(self) -> "RiskConfig":
        if not self.cooldown_usd <= self.per_trade_usd:
            raise ValueError("cooldown risk must not exceed per-trade risk (no martingale)")
        if not self.day_hard_stop < self.day_soft_stop:
            raise ValueError("day_hard_stop must be strictly beyond day_soft_stop")
        if not self.week_stop < self.day_hard_stop:
            raise ValueError("week_stop must be strictly beyond day_hard_stop")
        return self


class NewsConfig(_Model):
    pre_min: int = Field(ge=0)
    post_min: int = Field(ge=0)
    lookahead_block_min: int = Field(ge=0)
    currencies: dict[str, list[str]]

    @field_validator("currencies")
    @classmethod
    def _iso_currencies(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for pair, ccys in v.items():
            if not ccys:
                raise ValueError(f"no currencies listed for {pair}")
            for ccy in ccys:
                if len(ccy) != 3 or not ccy.isalpha() or not ccy.isupper():
                    raise ValueError(f"invalid currency code {ccy!r} for {pair}")
        return v


class StrategyConfig(_Model):
    strategy: Literal["ssr_v1"]
    pairs: dict[str, PairParams] = Field(min_length=1)
    shared: SharedParams
    risk: RiskConfig
    news: NewsConfig

    @model_validator(mode="after")
    def _news_covers_all_pairs(self) -> "StrategyConfig":
        missing = set(self.pairs) - set(self.news.currencies)
        if missing:
            raise ValueError(f"news.currencies missing for pairs: {sorted(missing)}")
        return self

    def session_times(self) -> SessionTimesConfig:
        """Wall-time anchors for core.timebase; non-strategy anchors keep spec defaults."""
        return SessionTimesConfig(
            asian_start=self.shared.asian_window_london[0],
            asian_end=self.shared.asian_window_london[1],
            entry_start=self.shared.entry_window_london[0],
            entry_end=self.shared.entry_window_london[1],
            forced_exit=self.shared.forced_exit_london,
        )


# ---------------------------------------------------------------------------
# Firm profile (§8)
# ---------------------------------------------------------------------------

class TrailingDrawdown(_Model):
    amount: Money = Field(gt=0)
    mode: Literal["intraday_equity", "eod_balance", "static"]


class DailyLoss(_Model):
    amount: Money = Field(gt=0)
    # v1 models only the harsher equity-including-floating basis; anything else
    # is refused rather than silently treated as the modeled variant.
    basis: Literal["equity_incl_floating"]
    reset: str  # "HH:MM <IANA zone>", e.g. "17:00 America/New_York"

    @field_validator("reset")
    @classmethod
    def _parseable_reset(cls, v: str) -> str:
        clock, _, zone = v.partition(" ")
        try:
            time.fromisoformat(clock)
            ZoneInfo(zone)
        except Exception as exc:
            raise ValueError(
                f"reset must be 'HH:MM <IANA zone>' (got {v!r})"
            ) from exc
        return v

    @property
    def reset_time(self) -> time:
        return time.fromisoformat(self.reset.partition(" ")[0])

    @property
    def reset_zone(self) -> str:
        return self.reset.partition(" ")[2]


class NewsRule(_Model):
    blackout_pre_min: int = Field(ge=0)
    blackout_post_min: int = Field(ge=0)
    profits_voided: bool


class EAPolicy(_Model):
    allowed: bool
    requires_disclosure: bool
    copy_trading_banned: bool

    @field_validator("allowed")
    @classmethod
    def _automation_must_be_allowed(cls, v: bool) -> bool:
        if not v:
            raise ValueError("ea_policy.allowed is false: this system may not run (§8a)")
        return v


class Payout(_Model):
    first_after_days: int = Field(ge=0)
    split_pct: int = Field(gt=0, le=100)


class FirmProfile(_Model):
    name: str = Field(min_length=1)
    account_size: Money = Field(gt=0)
    profit_target: Money = Field(gt=0)
    trailing_dd: TrailingDrawdown
    daily_loss: DailyLoss
    consistency_pct: int | None = Field(default=None, gt=0, le=100)
    min_trading_days: int = Field(ge=0)
    news_rule: NewsRule
    overnight_allowed: bool
    weekend_allowed: bool
    max_lots: Money = Field(gt=0)
    ea_policy: EAPolicy
    payout: Payout

    @model_validator(mode="after")
    def _limits_inside_account(self) -> "FirmProfile":
        if not self.trailing_dd.amount < self.account_size:
            raise ValueError("trailing drawdown must be strictly inside account size")
        if not self.daily_loss.amount < self.account_size:
            raise ValueError("daily loss limit must be strictly inside account size")
        return self


# ---------------------------------------------------------------------------
# Instruments (§10.4, §11)
# ---------------------------------------------------------------------------

class InstrumentSpec(_Model):
    broker_symbols: list[str] = Field(min_length=1)
    digits: int = Field(ge=1, le=6)
    point: Px = Field(gt=0)
    pip: Px = Field(gt=0)
    contract_size: Px = Field(gt=0)
    quote_ccy: str = Field(pattern=r"^[A-Z]{3}$")
    min_lot: Px = Field(gt=0)
    lot_step: Px = Field(gt=0)
    max_lot: Px = Field(gt=0)

    @field_validator("broker_symbols")
    @classmethod
    def _non_empty_symbols(cls, v: list[str]) -> list[str]:
        if any(not s.strip() for s in v):
            raise ValueError("broker symbol variants must be non-empty strings")
        return v

    @model_validator(mode="after")
    def _internally_consistent(self) -> "InstrumentSpec":
        # v1 instruments are 10-points-per-pip FX symbols; anything else (metals,
        # indices) needs an explicit model extension, not a config tweak.
        if self.point != Decimal(1).scaleb(-self.digits):
            raise ValueError("point must equal 10^-digits")
        if self.pip != self.point * 10:
            raise ValueError("pip must equal 10 x point")
        if not self.min_lot <= self.max_lot:
            raise ValueError("min_lot must not exceed max_lot")
        if not self.lot_step <= self.min_lot:
            raise ValueError("lot_step must not exceed min_lot")
        if self.min_lot % self.lot_step != 0 or self.max_lot % self.lot_step != 0:
            raise ValueError("lot bounds must align to lot_step")
        return self


class InstrumentsConfig(_Model):
    instruments: dict[str, InstrumentSpec] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Startup cross-validation (§8a/b): buffers strictly inside firm limits
# ---------------------------------------------------------------------------

def risk_reduction_only(old: "StrategyConfig", new: "StrategyConfig") -> tuple[bool, list[str]]:
    """§8c: mid-evaluation config changes may only REDUCE risk. Non-risk sections
    must be byte-identical; every risk field must be equal or strictly safer."""
    problems: list[str] = []
    if old.model_dump(exclude={"risk"}) != new.model_dump(exclude={"risk"}):
        problems.append("non-risk sections changed (blocked mid-evaluation)")
    o, n = old.risk, new.risk
    checks = [
        (n.per_trade_usd <= o.per_trade_usd, "per_trade_usd increased"),
        (n.cooldown_usd <= o.cooldown_usd, "cooldown_usd increased"),
        (n.day_soft_stop >= o.day_soft_stop, "day_soft_stop loosened"),
        (n.day_hard_stop >= o.day_hard_stop, "day_hard_stop loosened"),
        (n.week_stop >= o.week_stop, "week_stop loosened"),
        (n.consec_loss_halt <= o.consec_loss_halt, "consec_loss_halt loosened"),
        (n.max_entries_day <= o.max_entries_day, "max_entries_day increased"),
        (n.max_concurrent <= o.max_concurrent, "max_concurrent increased"),
        (n.consistency_day_cap <= o.consistency_day_cap, "consistency_day_cap increased"),
        (n.floor_buffer >= o.floor_buffer, "floor_buffer reduced"),
    ]
    problems.extend(msg for ok, msg in checks if not ok)
    return (not problems, problems)


class StartupValidationError(ValueError):
    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("startup validation failed: " + ", ".join(violations))


def validate_startup(
    strategy: StrategyConfig,
    firm: FirmProfile,
    instruments: InstrumentsConfig | None = None,
) -> None:
    """Raise StartupValidationError unless every internal buffer sits strictly
    inside the corresponding firm limit. The engine must not start on failure."""
    v: list[str] = []
    risk = strategy.risk

    if abs(risk.day_hard_stop) >= firm.daily_loss.amount:
        v.append("DAY_HARD_STOP_NOT_INSIDE_FIRM_DAILY_LIMIT")
    if risk.floor_buffer >= firm.trailing_dd.amount:
        v.append("FLOOR_BUFFER_NOT_INSIDE_TRAILING_DD")
    if abs(risk.week_stop) >= firm.trailing_dd.amount:
        v.append("WEEK_STOP_NOT_INSIDE_TRAILING_DD")
    if firm.consistency_pct is not None:
        firm_day_cap = firm.profit_target * Decimal(firm.consistency_pct) / Decimal(100)
        if risk.consistency_day_cap >= firm_day_cap:
            v.append("CONSISTENCY_CAP_NOT_INSIDE_FIRM_RULE")
    if strategy.news.pre_min < firm.news_rule.blackout_pre_min:
        v.append("NEWS_PRE_BLACKOUT_NARROWER_THAN_FIRM")
    if strategy.news.post_min < firm.news_rule.blackout_post_min:
        v.append("NEWS_POST_BLACKOUT_NARROWER_THAN_FIRM")
    if not firm.ea_policy.allowed:  # unreachable via model validation; defense in depth
        v.append("EA_POLICY_FORBIDS_AUTOMATION")
    if instruments is not None:
        for pair in strategy.pairs:
            if pair not in instruments.instruments:
                v.append(f"SYMBOL_MISSING_INSTRUMENT_SPEC:{pair}")

    if v:
        raise StartupValidationError(v)
