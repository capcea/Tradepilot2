"""FastAPI control surface (SPEC.md S12, S17): localhost-bound, token auth.

Every mutating endpoint writes an audit row. The kill switch flattens, writes
the KILL file and halts; re-arming requires manually deleting the file -- an
API call alone can never un-kill the system.

Run:  python -m api.app  (binds 127.0.0.1 only; set API_TOKEN env var)
"""
from __future__ import annotations

import json
import os
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable

import yaml
from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from api.auth import make_token_dependency
from core.config_schema import (
    FirmProfile,
    InstrumentsConfig,
    StartupValidationError,
    StrategyConfig,
    risk_reduction_only,
    validate_startup,
)
from ports.store import ConfigVersionRow
from services.config_loader import checksum_text
from services.flattener import flatten_all


def _s(x):
    """JSON-safe scalar: Decimals/datetimes/dates as strings, exact."""
    if isinstance(x, Decimal):
        return str(x)
    if isinstance(x, (datetime, date)):
        return x.isoformat()
    return x


def _row(obj) -> dict:
    return {k: _s(v) for k, v in asdict(obj).items()}


@dataclass
class ApiContext:
    store: object
    execution: object | None
    calendar: object
    clock: object
    strategy: StrategyConfig
    firm: FirmProfile
    config_checksum: str
    kill_file: Path
    alerts: object
    status_provider: Callable[[], dict]
    strategy_yaml_path: Path = field(default_factory=lambda: Path("configs/strategy.yaml"))
    firm_yaml_path: Path = field(default_factory=lambda: Path("configs/firm_profile.yaml"))
    instruments_yaml_path: Path = field(default_factory=lambda: Path("configs/instruments.yaml"))
    env_file_path: Path = field(default_factory=lambda: Path(".env"))
    paused: bool = False
    in_evaluation: bool = True
    log_buffer: deque = field(default_factory=lambda: deque(maxlen=2000))

    @property
    def killed(self) -> bool:
        return self.kill_file.exists()

    def log(self, level: str, message: str) -> None:
        self.log_buffer.append({
            "ts_utc": self.clock.now_utc().isoformat(), "level": level, "message": message,
        })

    def audit(self, actor: str, event: str, payload: dict | None = None) -> None:
        self.store.append_audit(
            self.clock.now_utc(), actor, event, json.dumps(payload or {}, default=_s)
        )


_ENV_KEYS = ["MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_TERMINAL_PATH", "LIVE_TRADING", "STATE_DB"]
_SECRET_KEYS = {"MT5_PASSWORD"}


def _write_env_file(env_file: Path, updates: dict) -> None:
    existing: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    for k, v in updates.items():
        if v:
            existing[k] = v
        else:
            existing.pop(k, None)
    lines = ["# TradePilot environment — managed by the settings UI"]
    for k in _ENV_KEYS:
        if k in existing:
            lines.append(f"{k}={existing[k]}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_app(ctx: ApiContext, token: str) -> FastAPI:
    app = FastAPI(title="TradePilot SSR v1", docs_url=None, redoc_url=None)
    auth = Depends(make_token_dependency(token))

    # -- dashboard (no data inline; JS calls the token-guarded API) -------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASHBOARD_HTML

    # -- read endpoints -----------------------------------------------------------

    @app.get("/health", dependencies=[auth])
    def health():
        return ctx.status_provider()

    @app.get("/state", dependencies=[auth])
    def state():
        sp = ctx.status_provider()
        return {
            "phase": sp.get("phase"),
            "config_checksum": ctx.config_checksum,
            "paused": ctx.paused,
            "killed": ctx.killed,
            "in_evaluation": ctx.in_evaluation,
            "model_version": None,  # deterministic v1: no ML model in the loop
        }

    @app.get("/positions", dependencies=[auth])
    def positions():
        if ctx.execution is None:
            return []
        return [_row(p) for p in ctx.execution.positions()]

    @app.get("/orders", dependencies=[auth])
    def orders():
        return []  # v1 trades market-in/bracket-out; no resting orders to list

    @app.get("/pnl/today", dependencies=[auth])
    def pnl_today():
        return _risk_day_dict(ctx, ctx.clock.now_utc().date())

    @app.get("/pnl/range", dependencies=[auth])
    def pnl_range(start: date = Query(...), end: date = Query(...)):
        out = []
        d = start
        while d <= end:
            row = ctx.store.get_risk_day(d)
            if row is not None:
                out.append(_risk_day_dict(ctx, d))
            d = date.fromordinal(d.toordinal() + 1)
        return out

    @app.get("/risk", dependencies=[auth])
    def risk():
        today = _risk_day_dict(ctx, ctx.clock.now_utc().date())
        acct = ctx.execution.account() if ctx.execution is not None else None
        return {
            "day_realized": today["realized"],
            "day_fees": today["fees"],
            "trades_today": today["trades"],
            "consec_losses": today["consec_losses"],
            "halted": today["halted"],
            "halt_reason": today["halt_reason"],
            "consistency_headroom": today["consistency_headroom"],
            "internal_day_soft_stop": _s(ctx.strategy.risk.day_soft_stop),
            "internal_day_hard_stop": _s(ctx.strategy.risk.day_hard_stop),
            "internal_week_stop": _s(ctx.strategy.risk.week_stop),
            "consistency_day_cap": _s(ctx.strategy.risk.consistency_day_cap),
            "floor_buffer": _s(ctx.strategy.risk.floor_buffer),
            "firm_daily_loss_limit": _s(ctx.firm.daily_loss.amount),
            "firm_trailing_dd": _s(ctx.firm.trailing_dd.amount),
            "equity": _s(acct.equity) if acct else None,
            "balance": _s(acct.balance) if acct else None,
        }

    @app.get("/setups", dependencies=[auth])
    def setups(date_: date = Query(..., alias="date")):
        return [_row(s) for s in ctx.store.get_setups_on(date_)]

    @app.get("/decisions", dependencies=[auth])
    def decisions(setup_id: str = Query(...)):
        return [_row(d) for d in ctx.store.get_decisions(setup_id)]

    @app.get("/calendar/next", dependencies=[auth])
    def calendar_next(hours: int = 12):
        now = ctx.clock.now_utc()
        return [
            {"ts_utc": _s(e.ts_utc), "currency": e.currency, "impact": e.impact,
             "title": e.title}
            for e in ctx.calendar.events_between(now, now + timedelta(hours=hours))
        ]

    @app.get("/logs", dependencies=[auth])
    def logs(level: str | None = None, since: datetime | None = None):
        out = list(ctx.log_buffer)
        if level:
            out = [r for r in out if r["level"] == level]
        if since:
            out = [r for r in out if datetime.fromisoformat(r["ts_utc"]) >= since]
        return out[-500:]

    @app.get("/reports/daily", dependencies=[auth])
    def daily_report(date_: date = Query(..., alias="date")):
        setups_rows = ctx.store.get_setups_on(date_)
        decision_rows = ctx.store.get_decisions_on(date_)
        risk_day = _risk_day_dict(ctx, date_)
        reason_counts: dict[str, int] = {}
        for d in decision_rows:
            if not d.passed:
                reason_counts[d.reason_code] = reason_counts.get(d.reason_code, 0) + 1
        summary = (
            f"{date_}: {len(setups_rows)} setups, {len(decision_rows)} decisions, "
            f"realized {risk_day['realized']}, trades {risk_day['trades']}. "
            f"Skip reasons: {reason_counts or 'none'}."
        )
        return {
            "date": date_.isoformat(),
            "risk_day": risk_day,
            "setups": [_row(s) for s in setups_rows],
            "decisions": [_row(d) for d in decision_rows],
            "summary": summary,
        }

    # -- control endpoints -----------------------------------------------------------

    @app.post("/pause", dependencies=[auth])
    def pause():
        ctx.paused = True
        ctx.audit("api", "pause")
        return {"paused": True}

    @app.post("/resume", dependencies=[auth])
    def resume():
        if ctx.killed:
            raise HTTPException(
                status_code=409,
                detail="kill switch engaged; delete the KILL file to re-arm manually",
            )
        ctx.paused = False
        ctx.audit("api", "resume")
        return {"paused": False}

    @app.post("/flat-all", dependencies=[auth])
    def flat_all():
        report = _flatten(ctx)
        ctx.audit("api", "flat_all", {"ok": report.ok, "closed": list(report.closed_tickets)})
        return {"ok": report.ok, "closed": list(report.closed_tickets),
                "remaining": list(report.remaining)}

    @app.post("/kill", dependencies=[auth])
    def kill():
        report = _flatten(ctx)
        ctx.kill_file.write_text(
            f"killed via api at {ctx.clock.now_utc().isoformat()}\n", encoding="utf-8"
        )
        ctx.paused = True
        ctx.audit("api", "kill", {"flatten_ok": report.ok})
        ctx.alerts.alert("critical", "KILL SWITCH fired via API; manual re-arm required")
        return {"killed": True, "flatten_ok": report.ok,
                "remaining": list(report.remaining)}

    @app.post("/config", dependencies=[auth])
    def post_config(payload: dict = Body(...)):
        raw = payload.get("yaml", "")
        author = payload.get("author", "unknown")
        try:
            data = yaml.safe_load(raw)
            new_cfg = StrategyConfig.model_validate(data)
            validate_startup(new_cfg, ctx.firm)
        except (yaml.YAMLError, ValueError, StartupValidationError) as exc:
            raise HTTPException(status_code=422, detail=f"config rejected: {exc}") from exc
        if ctx.in_evaluation:
            ok, problems = risk_reduction_only(ctx.strategy, new_cfg)
            if not ok:
                raise HTTPException(
                    status_code=422,
                    detail="mid-evaluation changes must be risk reductions only: "
                    + "; ".join(problems),
                )
        checksum = checksum_text(raw)
        ctx.store.insert_config_version(ConfigVersionRow(
            id=checksum[:12], ts_utc=ctx.clock.now_utc(), author=author,
            yaml=raw, checksum=checksum, active=False,
        ))
        ctx.store.activate_config(checksum[:12])
        ctx.strategy = new_cfg
        ctx.config_checksum = checksum
        ctx.audit(author, "config_activated", {"checksum": checksum})
        return {"checksum": checksum}

    @app.get("/config/strategy", dependencies=[auth])
    def get_strategy_yaml():
        try:
            return {"yaml": ctx.strategy_yaml_path.read_text(encoding="utf-8")}
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.get("/config/firm", dependencies=[auth])
    def get_firm_yaml():
        try:
            return {"yaml": ctx.firm_yaml_path.read_text(encoding="utf-8")}
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/config/firm", dependencies=[auth])
    def post_firm_config(payload: dict = Body(...)):
        raw = payload.get("yaml", "")
        author = payload.get("author", "unknown")
        try:
            data = yaml.safe_load(raw)
            if not isinstance(data, dict) or "firm_profile" not in data:
                raise ValueError("missing 'firm_profile' root key")
            new_firm = FirmProfile.model_validate(data["firm_profile"])
            validate_startup(ctx.strategy, new_firm)
        except (yaml.YAMLError, ValueError, StartupValidationError) as exc:
            raise HTTPException(status_code=422, detail=f"firm profile rejected: {exc}") from exc
        ctx.firm_yaml_path.write_text(raw, encoding="utf-8")
        ctx.firm = new_firm
        ctx.audit(author, "firm_config_saved")
        return {"ok": True}

    @app.get("/config/instruments", dependencies=[auth])
    def get_instruments_yaml():
        try:
            return {"yaml": ctx.instruments_yaml_path.read_text(encoding="utf-8")}
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/config/instruments", dependencies=[auth])
    def post_instruments_config(payload: dict = Body(...)):
        raw = payload.get("yaml", "")
        author = payload.get("author", "unknown")
        try:
            data = yaml.safe_load(raw)
            new_instruments = InstrumentsConfig.model_validate(data)
            validate_startup(ctx.strategy, ctx.firm, new_instruments)
        except (yaml.YAMLError, ValueError, StartupValidationError) as exc:
            raise HTTPException(status_code=422, detail=f"instruments config rejected: {exc}") from exc
        ctx.instruments_yaml_path.write_text(raw, encoding="utf-8")
        ctx.audit(author, "instruments_config_saved")
        return {"ok": True, "restart_required": True}

    @app.get("/config/env", dependencies=[auth])
    def get_env_config():
        result = {}
        for k in _ENV_KEYS:
            v = os.environ.get(k)
            if v is not None and k in _SECRET_KEYS:
                result[k] = {"set": True, "value": None}
            else:
                result[k] = {"set": v is not None, "value": v}
        return result

    @app.post("/config/env", dependencies=[auth])
    def post_env_config(payload: dict = Body(...)):
        author = payload.get("author", "unknown")
        updates = {k: payload[k] for k in _ENV_KEYS if k in payload}
        _write_env_file(ctx.env_file_path, updates)
        ctx.audit(author, "env_saved", {"keys": [k for k in updates if k not in _SECRET_KEYS]})
        return {"ok": True, "restart_required": True}

    return app


def _flatten(ctx: ApiContext):
    if ctx.execution is None:
        from services.flattener import FlattenReport

        return FlattenReport(0, (), (), ok=True, escalated=False)
    return flatten_all(ctx.execution, ctx.alerts, sleep=lambda s: None)


def _risk_day_dict(ctx: ApiContext, d: date) -> dict:
    row = ctx.store.get_risk_day(d)
    if row is None:
        return {"d": d.isoformat(), "realized": "0", "fees": "0", "trades": 0,
                "consec_losses": 0, "halted": False, "halt_reason": None,
                "consistency_headroom": _s(ctx.strategy.risk.consistency_day_cap)}
    return _row(row)


DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TradePilot SSR v1 - Control Panel</title>
<style>
 :root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2230; --line:#2a313c; --ink:#e6edf3;
  --mut:#8b949e; --ok:#3fb950; --bad:#f85149; --warn:#d29922; --idle:#6e7681;
  --accent:#58a6ff; --accentbg:#1f6feb;
 }
 *{box-sizing:border-box}
 body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);
   color:var(--ink);margin:0;font-size:14px;line-height:1.5}
 a{color:var(--accent)}
 .mut{color:var(--mut)}
 .small{font-size:12px}
 code{background:#010409;border:1px solid var(--line);border-radius:4px;padding:1px 5px;font-size:12px}
 /* header */
 header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
   padding:12px 18px;background:var(--panel);border-bottom:1px solid var(--line);
   position:sticky;top:0;z-index:5}
 header .brand{font-weight:700;font-size:16px}
 header .brand span{color:var(--mut);font-weight:400;font-size:12px;margin-left:6px}
 header .spacer{flex:1}
 .conn{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--mut)}
 .hbtn{background:var(--panel2);color:var(--ink);border:1px solid var(--line);
   border-radius:6px;padding:6px 12px;cursor:pointer;font-size:13px}
 .hbtn:hover{border-color:var(--accent)}
 /* layout */
 main{padding:18px;max-width:1180px;margin:0 auto;
   display:grid;grid-template-columns:repeat(2,1fr);gap:16px}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
 .card.full{grid-column:1 / -1}
 @media(max-width:780px){main{grid-template-columns:1fr}}
 .card h2{font-size:13px;margin:0 0 4px;text-transform:uppercase;letter-spacing:.04em;color:var(--accent)}
 .card .sub{margin:0 0 14px;font-size:12px;color:var(--mut)}
 /* status rows */
 .srow{display:flex;align-items:flex-start;gap:11px;padding:9px 0;border-top:1px solid var(--line)}
 .srow:first-of-type{border-top:none}
 .dot{width:11px;height:11px;border-radius:50%;margin-top:5px;flex:none;background:var(--idle)}
 .dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
 .dot.warn{background:var(--warn)}.dot.idle{background:var(--idle)}
 .srow .body{flex:1;min-width:0}
 .srow .ttl{font-weight:600}
 .srow .val{font-weight:600}
 .srow .val.ok{color:var(--ok)}.srow .val.bad{color:var(--bad)}
 .srow .val.warn{color:var(--warn)}.srow .val.idle{color:var(--mut)}
 .srow .mn{font-size:12px;color:var(--mut);margin-top:1px}
 .srow .top{display:flex;justify-content:space-between;gap:10px}
 /* controls */
 .ctl{display:flex;gap:12px;align-items:flex-start;padding:11px 0;border-top:1px solid var(--line)}
 .ctl:first-of-type{border-top:none}
 .ctl .txt{flex:1}
 .ctl .txt b{display:block}
 .ctl .txt small{color:var(--mut)}
 button.act{border:1px solid var(--line);background:var(--panel2);color:var(--ink);
   border-radius:6px;padding:8px 16px;cursor:pointer;font-size:13px;font-weight:600;min-width:104px}
 button.act:hover:not(:disabled){border-color:var(--accent)}
 button.act:disabled{opacity:.4;cursor:not-allowed}
 button.act.danger{border-color:#7d2622;color:#ff9a93}
 button.act.danger:hover:not(:disabled){background:#3d1513;border-color:var(--bad)}
 /* risk */
 .big{font-size:30px;font-weight:700;line-height:1.1}
 .bar{height:9px;border-radius:5px;background:var(--panel2);overflow:hidden;margin:10px 0 4px;position:relative}
 .bar > i{display:block;height:100%;border-radius:5px}
 .chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
 .chip{background:var(--panel2);border:1px solid var(--line);border-radius:6px;padding:7px 11px;font-size:12px}
 .chip b{font-size:15px;display:block}
 .kv{display:flex;justify-content:space-between;font-size:12px;color:var(--mut);margin-top:12px;
   border-top:1px solid var(--line);padding-top:10px}
 /* tables */
 table{border-collapse:collapse;width:100%;font-size:12.5px}
 th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--line)}
 th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.03em}
 tbody tr:hover{background:var(--panel2)}
 .badge{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11px;font-weight:600;border:1px solid}
 .badge.long{color:var(--ok);border-color:#1f7a36;background:#0f2417}
 .badge.short{color:var(--bad);border-color:#7d2622;background:#2a100e}
 .badge.neu{color:var(--mut);border-color:var(--line)}
 .pass{color:var(--ok);font-weight:700}.fail{color:var(--bad);font-weight:700}
 .empty{color:var(--mut);font-size:13px;padding:10px 0;text-align:center;
   border:1px dashed var(--line);border-radius:8px}
 .pos-num{color:var(--ok)}.neg-num{color:var(--bad)}
 .lvl-error{color:var(--bad)}.lvl-warn,.lvl-warning{color:var(--warn)}.lvl-info{color:var(--mut)}
 /* overlay (login + confirm + help) */
 .overlay{position:fixed;inset:0;background:rgba(2,6,12,.82);display:flex;
   align-items:center;justify-content:center;z-index:50;padding:18px}
 .overlay[hidden]{display:none}
 .modal{background:var(--panel);border:1px solid var(--line);border-radius:12px;
   padding:26px;max-width:430px;width:100%}
 .modal h2{margin:0 0 6px;font-size:18px;color:var(--ink);text-transform:none;letter-spacing:0}
 .modal p{color:var(--mut);font-size:13px}
 .modal input[type=password],.modal input[type=text]{width:100%;background:#010409;border:1px solid var(--line);
   color:var(--ink);border-radius:7px;padding:11px 12px;font-size:14px;margin:6px 0}
 .modal label.rm{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--mut);margin:6px 0 16px}
 .primary{background:var(--accentbg);border:1px solid var(--accentbg);color:#fff;font-weight:600;
   border-radius:7px;padding:11px 16px;cursor:pointer;font-size:14px;width:100%}
 .primary:hover{filter:brightness(1.1)}
 .row-btns{display:flex;gap:10px;margin-top:20px}
 .row-btns button{flex:1}
 .ghost{background:var(--panel2);border:1px solid var(--line);color:var(--ink);
   border-radius:7px;padding:11px 16px;cursor:pointer;font-size:14px}
 .err{color:var(--bad);font-size:13px;min-height:18px;margin-top:8px}
 /* help drawer */
 #help{position:fixed;top:0;right:0;height:100%;width:380px;max-width:92vw;background:var(--panel);
   border-left:1px solid var(--line);z-index:40;padding:22px;overflow:auto;
   transform:translateX(100%);transition:transform .2s ease}
 #help.open{transform:translateX(0)}
 #help h2{margin-top:0}
 #help dt{font-weight:600;margin-top:14px}
 #help dd{margin:2px 0 0;color:var(--mut);font-size:13px}
 .banner{grid-column:1/-1;background:#3d1513;border:1px solid var(--bad);color:#ffb3ad;
   border-radius:8px;padding:11px 14px;font-size:13px;display:none}
 .banner.show{display:block}
 .updated{font-size:11px;color:var(--idle)}
 /* settings modal */
 #sm-ov{position:fixed;inset:0;background:rgba(2,6,12,.82);display:flex;
   align-items:center;justify-content:center;z-index:50;padding:18px}
 #sm-ov[hidden]{display:none}
 .sm{background:var(--panel);border:1px solid var(--line);border-radius:12px;
   max-width:720px;width:100%;max-height:92vh;display:flex;flex-direction:column;overflow:hidden}
 .sm-hdr{padding:16px 20px 0;border-bottom:1px solid var(--line)}
 .sm-hdr h2{margin:0 0 12px;font-size:16px;text-transform:none;letter-spacing:0;color:var(--ink)}
 .sm-tabs{display:flex;margin:0 -20px;padding:0 20px}
 .sm-tab{background:none;border:none;color:var(--mut);padding:8px 13px;cursor:pointer;font-size:12px;
   border-bottom:2px solid transparent;margin-bottom:-1px;font-weight:500;letter-spacing:.02em}
 .sm-tab.on{color:var(--ink);border-bottom-color:var(--accent)}
 .sm-body{padding:18px 20px;overflow-y:auto;flex:1}
 .sm-foot{padding:12px 20px;border-top:1px solid var(--line);display:flex;gap:10px;align-items:center}
 .tp{display:none}.tp.on{display:block}
 .fld{margin-bottom:14px}
 .fld label{display:block;font-size:11px;color:var(--mut);margin-bottom:4px;font-weight:600;
   text-transform:uppercase;letter-spacing:.05em}
 .fld input[type=text],.fld input[type=password]{width:100%;background:#010409;
   border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:8px 11px;font-size:13px}
 .fld input:focus{border-color:var(--accent);outline:none}
 .fld .ht{font-size:11px;color:var(--idle);margin-top:2px}
 .fld-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
 .ya{width:100%;background:#010409;border:1px solid var(--line);color:var(--ink);
   border-radius:6px;padding:10px 12px;font-size:12px;font-family:monospace;
   min-height:260px;resize:vertical}
 .ya:focus{border-color:var(--accent);outline:none}
 .tgl-row{display:flex;align-items:center;justify-content:space-between;
   padding:10px 0;border-top:1px solid var(--line)}
 .tgl-row .ti b{display:block;font-size:13px}
 .tgl-row .ti small{color:var(--mut);font-size:11px}
 .tgl{position:relative;display:inline-block;width:40px;height:22px}
 .tgl input{opacity:0;width:0;height:0}
 .sl{position:absolute;cursor:pointer;inset:0;background:#2a313c;border-radius:22px;transition:.2s}
 .sl:before{position:absolute;content:"";height:16px;width:16px;left:3px;bottom:3px;
   background:var(--mut);border-radius:50%;transition:.2s}
 input:checked+.sl{background:var(--accentbg)}
 input:checked+.sl:before{transform:translateX(18px);background:#fff}
 .smsg{font-size:12px;flex:1}
 .smsg.ok{color:var(--ok)}.smsg.bad{color:var(--bad)}
 .rn{background:#1c2a3a;border:1px solid #2d4a6b;color:#89b4e0;border-radius:6px;
   padding:7px 11px;font-size:12px;margin-bottom:14px}
</style></head><body>

<!-- ====================== LOGIN ====================== -->
<div class="overlay" id="login">
 <form class="modal" id="loginForm">
  <h2>TradePilot SSR v1</h2>
  <p>Autonomous FX prop-trading control panel. Enter your API access token to continue.</p>
  <input type="password" id="tokenInput" placeholder="API token" autocomplete="off" autofocus>
  <label class="rm"><input type="checkbox" id="remember" checked> Remember on this device</label>
  <button type="submit" class="primary">Connect</button>
  <div class="err" id="loginErr"></div>
  <p class="small" style="margin-top:16px">The token is the <code>API_TOKEN</code> value set when the
   server was started (see <code>RUNBOOK.md</code> &sect;4). It is stored only in this browser.</p>
 </form>
</div>

<!-- ====================== APP ====================== -->
<div id="app" hidden>
 <header>
  <div class="brand">TradePilot <span>SSR v1 - control panel</span></div>
  <div class="conn"><span class="dot" id="connDot"></span><span id="connTxt">connecting...</span></div>
  <div class="spacer"></div>
  <span class="updated" id="updated"></span>
  <button class="hbtn" id="settingsBtn">&#9881; Settings</button>
  <button class="hbtn" id="helpBtn">? Help</button>
  <button class="hbtn" id="signout">Sign out</button>
 </header>

 <main>
  <div class="banner" id="banner"></div>

  <!-- SYSTEM HEALTH -->
  <div class="card">
   <h2>System health</h2>
   <p class="sub">Live readiness checks. Green means healthy; grey means no live data
     (normal for a paper / local install with no broker feed).</p>
   <div id="status"></div>
  </div>

  <!-- CONTROLS -->
  <div class="card">
   <h2>Controls</h2>
   <p class="sub">Manual overrides. Every action is logged in the audit trail.</p>
   <div class="ctl">
    <div class="txt"><b>Pause trading</b><small>Stop opening new trades. Open positions keep their stops &amp; targets.</small></div>
    <button class="act" id="btnPause">Pause</button>
   </div>
   <div class="ctl">
    <div class="txt"><b>Resume trading</b><small>Allow new trades again. Blocked while the kill switch is engaged.</small></div>
    <button class="act" id="btnResume">Resume</button>
   </div>
   <div class="ctl">
    <div class="txt"><b>Flatten all</b><small>Immediately close every open position at market. Trading continues.</small></div>
    <button class="act danger" id="btnFlat">Flat all</button>
   </div>
   <div class="ctl">
    <div class="txt"><b>Kill switch</b><small>Emergency stop: close everything, halt, and lock the system.
      Re-arming requires deleting the <code>KILL</code> file on the server.</small></div>
    <button class="act danger" id="btnKill">KILL</button>
   </div>
  </div>

  <!-- RISK TODAY -->
  <div class="card">
   <h2>Risk today</h2>
   <p class="sub">Profit / loss so far today against the safety limits that halt trading automatically.</p>
   <div id="risk">loading...</div>
  </div>

  <!-- OPEN POSITIONS -->
  <div class="card">
   <h2>Open positions</h2>
   <p class="sub">Trades the system currently holds in the market.</p>
   <div id="pos">loading...</div>
  </div>

  <!-- SETUPS -->
  <div class="card full">
   <h2>Today's setups &amp; decisions</h2>
   <p class="sub">A <b>setup</b> is a potential trade the strategy spotted. Each one passes through
     a chain of <b>decision</b> checks (risk, news, spread...); it only becomes an order if every check passes.</p>
   <div id="setups">loading...</div>
  </div>

  <!-- NEWS -->
  <div class="card">
   <h2>Upcoming high-impact news (12h)</h2>
   <p class="sub">New entries pause automatically inside a blackout window around these events.</p>
   <div id="cal">loading...</div>
  </div>

  <!-- LOGS -->
  <div class="card">
   <h2>Recent activity</h2>
   <p class="sub">Latest events from the engine log.</p>
   <div id="logs">loading...</div>
  </div>
 </main>
</div>

<!-- ====================== HELP DRAWER ====================== -->
<aside id="help">
 <h2>Glossary</h2>
 <p class="mut small">Plain-English meaning of the terms used on this panel.</p>
 <dl>
  <dt>SSR v1</dt><dd>"Sweep / Sweep-Reclaim" strategy, version 1 - the rule set this system trades.</dd>
  <dt>Phase</dt><dd>What the trading loop is doing right now. <code>IDLE</code> means it is not actively trading.</dd>
  <dt>Setup</dt><dd>A pattern the strategy detected that could become a trade. Still has to pass every risk check first.</dd>
  <dt>Decision</dt><dd>One check applied to a setup (risk budget, news blackout, spread, time window...). All must pass to place an order.</dd>
  <dt>Paused vs Killed</dt><dd><b>Paused</b> = no new trades, existing ones still managed (reversible from here).
    <b>Killed</b> = emergency stop, everything closed and locked; only un-locked by deleting the KILL file on the server.</dd>
  <dt>Soft / hard stop</dt><dd>Internal daily loss limits. The soft stop slows trading; the hard stop halts it for the day - both well inside the firm's limit.</dd>
  <dt>Consistency headroom</dt><dd>The most additional profit you may book today without breaking the firm's "no single big day" consistency rule.</dd>
  <dt>Trailing drawdown</dt><dd>The firm's moving loss limit measured from your highest equity. Breaching it fails the account.</dd>
  <dt>Daily loss limit</dt><dd>The firm's hard cap on how much you may lose in one day.</dd>
  <dt>Flattener</dt><dd>An independent watchdog process that force-closes positions at session end, even if the main engine is down.</dd>
  <dt>M5 candle</dt><dd>A 5-minute price bar - the timeframe the strategy works on.</dd>
 </dl>
 <button class="ghost" id="helpClose" style="width:100%;margin-top:18px">Close</button>
</aside>

<!-- ====================== CONFIRM MODAL ====================== -->
<div class="overlay" id="confirm" hidden>
 <div class="modal">
  <h2 id="cfTitle"></h2>
  <p id="cfBody"></p>
  <div class="row-btns">
   <button class="ghost" id="cfCancel">Cancel</button>
   <button class="primary" id="cfOk" style="background:var(--bad);border-color:var(--bad)">Confirm</button>
  </div>
 </div>
</div>

<!-- ==================== SETTINGS ==================== -->
<div id="sm-ov" hidden>
 <div class="sm">
  <div class="sm-hdr">
   <h2>Configuration</h2>
   <div class="sm-tabs">
    <button class="sm-tab on" data-t="env">Credentials &amp; Env</button>
    <button class="sm-tab" data-t="strategy">Strategy</button>
    <button class="sm-tab" data-t="firm">Firm Profile</button>
    <button class="sm-tab" data-t="instruments">Instruments</button>
   </div>
  </div>
  <div class="sm-body">
   <!-- credentials -->
   <div class="tp on" id="tp-env">
    <div class="rn">Changes are saved to <code>.env</code> in the server directory and take effect on the <b>next restart</b>. Shell environment variables take precedence over this file.</div>
    <div class="fld-row">
     <div class="fld"><label>MT5_LOGIN</label>
      <input type="text" id="ev-MT5_LOGIN" placeholder="12345678" autocomplete="off">
      <div class="ht">MT5 account login number</div></div>
     <div class="fld"><label>MT5_SERVER</label>
      <input type="text" id="ev-MT5_SERVER" placeholder="ICMarkets-Demo01" autocomplete="off">
      <div class="ht">Broker server name</div></div>
    </div>
    <div class="fld"><label>MT5_PASSWORD</label>
     <input type="password" id="ev-MT5_PASSWORD" placeholder="Leave blank to keep existing" autocomplete="new-password">
     <div class="ht">MT5 account password — leave blank to keep the current value</div></div>
    <div class="fld"><label>MT5_TERMINAL_PATH <span style="text-transform:none;font-weight:400">(optional)</span></label>
     <input type="text" id="ev-MT5_TERMINAL_PATH" placeholder='C:\\Program Files\\MetaTrader 5\\terminal64.exe' autocomplete="off">
     <div class="ht">Custom path to MT5 terminal executable (Windows only)</div></div>
    <div class="fld"><label>STATE_DB</label>
     <input type="text" id="ev-STATE_DB" placeholder="data/state.sqlite" autocomplete="off">
     <div class="ht">SQLite state database path (default: data/state.sqlite)</div></div>
    <div class="tgl-row">
     <div class="ti"><b>LIVE_TRADING</b>
      <small> — three-factor arming gate (this env var + ARM_LIVE file + config checksum must all match)</small></div>
     <label class="tgl"><input type="checkbox" id="ev-LIVE_TRADING"><span class="sl"></span></label>
    </div>
    <p class="small mut" style="margin-top:12px"><code>API_TOKEN</code> must be set as a shell env var before starting the server — it is the auth token for this panel and cannot be changed here.</p>
   </div>
   <!-- strategy -->
   <div class="tp" id="tp-strategy">
    <p class="small mut" style="margin:0 0 10px">Edit the strategy YAML. Validation runs before saving. Mid-session changes are limited to risk reductions only.</p>
    <textarea class="ya" id="ya-strategy" spellcheck="false"></textarea>
   </div>
   <!-- firm profile -->
   <div class="tp" id="tp-firm">
    <p class="small mut" style="margin:0 0 10px"><b>Verify every field against your firm's current Terms of Service</b> before saving. The engine refuses to start if internal buffers fall outside the firm limits.</p>
    <textarea class="ya" id="ya-firm" spellcheck="false"></textarea>
   </div>
   <!-- instruments -->
   <div class="tp" id="tp-instruments">
    <div class="rn">Instrument spec changes take effect on the <b>next server restart</b>. Boot validation compares these specs against the live broker symbol info and refuses mismatches.</div>
    <textarea class="ya" id="ya-instruments" spellcheck="false"></textarea>
   </div>
  </div>
  <div class="sm-foot">
   <button class="primary" id="smSave" style="width:auto;padding:9px 20px">Save</button>
   <button class="ghost" id="smClose">Close</button>
   <span class="smsg" id="smMsg"></span>
  </div>
 </div>
</div>

<script>
const $ = id => document.getElementById(id);
let token = localStorage.token || '';

// ---- API helper: adds auth, distinguishes auth vs network errors -------------
async function api(path, opts){
 const o = Object.assign({}, opts);
 o.headers = Object.assign({Authorization: 'Bearer ' + token}, o.headers || {});
 let res;
 try{ res = await fetch(path, o); }
 catch(e){ const err = new Error('network'); err.kind = 'network'; throw err; }
 if(res.status === 401){ const err = new Error('auth'); err.kind = 'auth'; throw err; }
 if(!res.ok){ let d='http '+res.status;
  try{const j=await res.json();d=j.detail||d;}catch(e){}
  const err=new Error(d);err.kind='http';err.detail=d;throw err; }
 return res.json();
}

// ---- login -------------------------------------------------------------------
$('loginForm').addEventListener('submit', async e => {
 e.preventDefault();
 const v = $('tokenInput').value.trim();
 if(!v){ $('loginErr').textContent = 'Enter a token.'; return; }
 token = v; $('loginErr').textContent = 'Checking...';
 try{
  await api('/state');
  if($('remember').checked) localStorage.token = token; else localStorage.removeItem('token');
  $('login').style.display = 'none'; $('app').hidden = false;
  refresh(); if(!timer) timer = setInterval(refresh, 5000);
 }catch(err){
  $('loginErr').textContent = err.kind === 'auth'
    ? 'Token rejected. Check the API_TOKEN on the server.'
    : 'Cannot reach the server. Is it running?';
 }
});
function showLogin(msg){
 $('app').hidden = true; $('login').style.display = 'flex';
 $('tokenInput').value = ''; $('loginErr').textContent = msg || '';
 if(timer){ clearInterval(timer); timer = null; }
}
$('signout').onclick = () => { localStorage.removeItem('token'); token = ''; showLogin(''); };

// ---- help drawer & confirm modal --------------------------------------------
$('helpBtn').onclick = () => $('help').classList.add('open');
$('helpClose').onclick = () => $('help').classList.remove('open');
let pending = null;
function ask(title, body, fn){
 $('cfTitle').textContent = title; $('cfBody').textContent = body;
 pending = fn; $('confirm').hidden = false;
}
$('cfCancel').onclick = () => { $('confirm').hidden = true; pending = null; };
$('cfOk').onclick = async () => { $('confirm').hidden = true; const f = pending; pending = null; if(f) await f(); };

async function post(path){
 try{ await api('/' + path, {method:'POST'}); }
 catch(err){ if(err.kind === 'auth') return showLogin('Session expired - sign in again.'); }
 refresh();
}
$('btnPause').onclick  = () => post('pause');
$('btnResume').onclick = () => post('resume');
$('btnFlat').onclick   = () => ask('Flatten all positions?',
  'This immediately closes every open position at market price. The system keeps trading afterwards.',
  () => post('flat-all'));
$('btnKill').onclick   = () => ask('Engage the kill switch?',
  'Emergency stop: closes everything, halts trading, and locks the system. Re-arming is deliberate - '
  + 'someone must delete the KILL file on the server and then press Resume. An API call alone cannot undo this.',
  () => post('kill'));

// ---- formatting helpers ------------------------------------------------------
const num = v => v == null ? null : Number(v);
const money = v => { const n = num(v); if(n == null || isNaN(n)) return 'n/a';
 return (n < 0 ? '-$' : '$') + Math.abs(n).toLocaleString(undefined,{maximumFractionDigits:2}); };
const age = v => v == null ? '-' : v < 60 ? Math.round(v) + 's'
 : v < 3600 ? (v/60).toFixed(1) + 'm' : (v/3600).toFixed(1) + 'h';
const esc = s => String(s == null ? '' : s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const shortId = s => s ? esc(String(s).slice(0,8)) : '-';

// ---- system health -----------------------------------------------------------
function renderStatus(h, s){
 const ok = b => b ? 'ok' : 'bad';
 const items = [
  ['Engine phase', esc(s.phase || '-'), s.phase && s.phase !== 'IDLE' ? 'ok' : 'idle',
   'The high-level stage of the trading loop. IDLE means it is not actively trading right now.'],
  ['Broker feed', h.broker_connected ? 'Connected' : 'Disconnected', ok(h.broker_connected),
   'Live price &amp; execution link to the broker terminal (MT5). Required before any real trade.'],
  ['Price ticks', age(h.tick_age_s), h.tick_age_s == null ? 'idle' : h.tick_age_s < 10 ? 'ok' : 'bad',
   'Time since the last price update. Healthy when under 10 seconds.'],
  ['M5 candles', age(h.bar_age_s), h.bar_age_s == null ? 'idle' : h.bar_age_s < 420 ? 'ok' : 'bad',
   'Time since the last 5-minute candle closed. Healthy when under 7 minutes.'],
  ['Clock sync', h.ntp_offset_s == null ? '-' : h.ntp_offset_s + 's',
   h.ntp_offset_s == null ? 'idle' : Math.abs(h.ntp_offset_s) < 2 ? 'ok' : 'bad',
   'System clock drift versus internet time. Must stay within 2 seconds for accurate timing.'],
  ['Safety flattener', age(h.flattener_heartbeat_age_s),
   h.flattener_heartbeat_age_s == null ? 'idle' : h.flattener_heartbeat_age_s < 1200 ? 'ok' : 'bad',
   'Heartbeat of the independent watchdog that force-closes positions at session end. Under 20 minutes is healthy.'],
  ['Trading state', s.paused ? 'Paused' : 'Active', s.paused ? 'warn' : 'ok',
   s.paused ? 'New entries are paused; open positions are still managed.'
            : 'The system may open new trades when a valid setup appears.'],
  ['Kill switch', s.killed ? 'ENGAGED' : 'Armed - OK', s.killed ? 'bad' : 'ok',
   s.killed ? 'Emergency stop is active: flat and halted. Delete the KILL file on the server, then Resume.'
            : 'Not engaged. The system is allowed to run.'],
 ];
 $('status').innerHTML = items.map(([t, v, st, mn]) =>
  '<div class="srow"><span class="dot ' + st + '"></span><div class="body">'
  + '<div class="top"><span class="ttl">' + t + '</span><span class="val ' + st + '">' + v + '</span></div>'
  + '<div class="mn">' + mn + '</div></div></div>').join('');
}

// ---- risk --------------------------------------------------------------------
function renderRisk(r){
 const realized = num(r.day_realized) || 0;
 const hard = Math.abs(num(r.internal_day_hard_stop) || 0);
 const soft = Math.abs(num(r.internal_day_soft_stop) || 0);
 const lossUsed = realized < 0 ? -realized : 0;
 const pct = hard ? Math.min(100, lossUsed / hard * 100) : 0;
 const softPct = hard ? Math.min(100, soft / hard * 100) : 0;
 const profit = realized >= 0;
 const barColor = pct >= 100 ? 'var(--bad)' : pct >= softPct ? 'var(--warn)' : 'var(--ok)';
 let bar = '';
 if(!profit && hard){
  bar = '<div class="bar"><i style="width:' + pct.toFixed(0) + '%;background:' + barColor + '"></i>'
   + '<span style="position:absolute;left:' + softPct.toFixed(0) + '%;top:-2px;bottom:-2px;width:2px;background:var(--ink);opacity:.5"></span>'
   + '</div><div class="small mut">Daily loss used: ' + money(-lossUsed) + ' of hard stop '
   + money(-hard) + ' (soft stop ' + money(-soft) + ' marked).</div>';
 } else {
  bar = '<div class="small mut" style="margin-top:8px">In profit. Internal daily safety stops: soft '
   + money(num(r.internal_day_soft_stop)) + ', hard ' + money(num(r.internal_day_hard_stop)) + '.</div>';
 }
 const halt = r.halted
  ? '<div class="chip" style="border-color:var(--bad);color:var(--bad)"><b>HALTED</b>' + esc(r.halt_reason || 'risk limit') + '</div>'
  : '';
 $('risk').innerHTML =
  '<div class="big ' + (realized < 0 ? 'neg-num' : 'pos-num') + '">' + money(realized) + '</div>'
  + '<div class="small mut">Realized P&amp;L today' + (num(r.day_fees) ? ' (fees ' + money(r.day_fees) + ')' : '') + '</div>'
  + bar
  + '<div class="chips">'
  + '<div class="chip">Trades today<b>' + (r.trades_today ?? 0) + '</b></div>'
  + '<div class="chip">Consecutive losses<b>' + (r.consec_losses ?? 0) + '</b></div>'
  + '<div class="chip" title="Most additional profit allowed today under the firm consistency rule.">Consistency headroom<b>'
  + money(r.consistency_headroom) + '</b></div>'
  + '<div class="chip">Account equity<b>' + money(r.equity) + '</b></div>'
  + halt + '</div>'
  + '<div class="kv"><span>Firm daily-loss limit ' + money(r.firm_daily_loss_limit) + '</span>'
  + '<span>Firm trailing drawdown ' + money(r.firm_trailing_dd) + '</span></div>';
}

// ---- positions ---------------------------------------------------------------
function renderPositions(rows){
 if(!rows.length){ $('pos').innerHTML = '<div class="empty">No open positions - the system is flat.</div>'; return; }
 $('pos').innerHTML = '<table><thead><tr>'
  + '<th>Symbol</th><th>Side</th><th>Lots</th><th>Entry</th><th>Stop</th><th>Target</th><th>Unrealized</th></tr></thead><tbody>'
  + rows.map(p => { const u = num(p.unrealized_pnl);
   return '<tr><td>' + esc(p.symbol) + '</td>'
    + '<td><span class="badge ' + (p.side === 'long' ? 'long' : 'short') + '">' + esc(p.side) + '</span></td>'
    + '<td>' + esc(p.lots) + '</td><td>' + esc(p.entry_price) + '</td>'
    + '<td>' + esc(p.sl) + '</td><td>' + esc(p.tp) + '</td>'
    + '<td class="' + (u < 0 ? 'neg-num' : 'pos-num') + '">' + money(p.unrealized_pnl) + '</td></tr>'; }).join('')
  + '</tbody></table>';
}

// ---- setups & decisions ------------------------------------------------------
async function renderSetups(){
 const today = new Date().toISOString().slice(0,10);
 const setups = await api('/setups?date=' + today);
 if(!setups.length){
  $('setups').innerHTML = '<div class="empty">No setups detected today yet.</div>'; return;
 }
 const parts = [];
 for(const s of setups){
  const ds = await api('/decisions?setup_id=' + encodeURIComponent(s.id)).catch(() => []);
  const dir = s.direction === 'long' ? 'long' : s.direction === 'short' ? 'short' : 'neu';
  parts.push('<div style="margin-bottom:14px">'
   + '<div style="margin-bottom:6px"><code>' + shortId(s.id) + '</code> '
   + esc(s.symbol || '') + ' <span class="badge ' + dir + '">' + esc(s.direction || '-') + '</span> '
   + '<span class="badge neu">' + esc(s.status || '') + '</span></div>'
   + (ds.length
      ? '<table><thead><tr><th>Check</th><th>Result</th><th>Reason</th></tr></thead><tbody>'
        + ds.map(d => '<tr><td>' + esc(d.stage) + '</td>'
          + '<td class="' + (d.passed ? 'pass' : 'fail') + '">' + (d.passed ? 'pass' : 'fail') + '</td>'
          + '<td class="mut">' + esc(d.reason_code) + '</td></tr>').join('')
        + '</tbody></table>'
      : '<div class="small mut">No decision checks recorded yet.</div>')
   + '</div>');
 }
 $('setups').innerHTML = parts.join('');
}

// ---- news --------------------------------------------------------------------
function renderCalendar(rows){
 if(!rows.length){ $('cal').innerHTML = '<div class="empty">No high-impact news in the next 12 hours.</div>'; return; }
 $('cal').innerHTML = '<table><thead><tr><th>Time (UTC)</th><th>Ccy</th><th>Impact</th><th>Event</th></tr></thead><tbody>'
  + rows.map(e => '<tr><td>' + esc(String(e.ts_utc).replace('T',' ').slice(0,16)) + '</td>'
    + '<td>' + esc(e.currency) + '</td><td>' + esc(e.impact) + '</td><td>' + esc(e.title) + '</td></tr>').join('')
  + '</tbody></table>';
}

// ---- logs --------------------------------------------------------------------
function renderLogs(rows){
 if(!rows.length){ $('logs').innerHTML = '<div class="empty">No recent activity.</div>'; return; }
 $('logs').innerHTML = '<table><tbody>'
  + rows.slice(-12).reverse().map(l => '<tr><td class="mut" style="white-space:nowrap">'
    + esc(String(l.ts_utc).replace('T',' ').slice(11,19)) + '</td>'
    + '<td class="lvl-' + esc((l.level||'').toLowerCase()) + '" style="text-transform:uppercase">' + esc(l.level) + '</td>'
    + '<td>' + esc(l.message) + '</td></tr>').join('')
  + '</tbody></table>';
}

// ---- buttons enable/disable per state ----------------------------------------
function syncControls(s){
 $('btnPause').disabled  = s.paused || s.killed;
 $('btnResume').disabled = (!s.paused && !s.killed) || s.killed;
 $('btnResume').title = s.killed ? 'Blocked: delete the KILL file on the server first.' : '';
}

// ---- main refresh ------------------------------------------------------------
let timer = null;
async function refresh(){
 try{
  const [h, s, r, pos, cal, logs] = await Promise.all([
   api('/health'), api('/state'), api('/risk'), api('/positions'),
   api('/calendar/next'), api('/logs')]);
  $('banner').classList.remove('show');
  $('connDot').className = 'dot ok'; $('connTxt').textContent =
   'Connected - config ' + (s.config_checksum || '').slice(0,12);
  $('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
  renderStatus(h, s); renderRisk(r); renderPositions(pos);
  renderCalendar(cal); renderLogs(logs); syncControls(s);
  await renderSetups();
 }catch(err){
  if(err.kind === 'auth') return showLogin('Session expired - sign in again.');
  $('connDot').className = 'dot bad'; $('connTxt').textContent = 'Disconnected';
  $('banner').textContent = 'Cannot reach the server. Retrying every 5 seconds...';
  $('banner').classList.add('show');
 }
}

// ---- boot --------------------------------------------------------------------
if(token){ $('login').style.display = 'none'; $('app').hidden = false;
 refresh(); timer = setInterval(refresh, 5000); }

// ---- settings ---------------------------------------------------------------
let smTab = 'env';

function smTabSwitch(t){
 smTab = t;
 document.querySelectorAll('.sm-tab').forEach(b => b.classList.toggle('on', b.dataset.t === t));
 document.querySelectorAll('.tp').forEach(p => p.classList.toggle('on', p.id === 'tp-' + t));
 if(t === 'strategy') smLoadYaml('strategy');
 else if(t === 'firm') smLoadYaml('firm');
 else if(t === 'instruments') smLoadYaml('instruments');
}
document.querySelectorAll('.sm-tab').forEach(b => b.onclick = () => smTabSwitch(b.dataset.t));

$('settingsBtn').onclick = async () => {
 $('sm-ov').hidden = false;
 smTabSwitch('env');
 await smLoadEnv();
};
$('smClose').onclick = () => { $('sm-ov').hidden = true; };

async function smLoadEnv(){
 try{
  const r = await api('/config/env');
  ['MT5_LOGIN','MT5_SERVER','MT5_TERMINAL_PATH','STATE_DB'].forEach(k => {
   const el = $('ev-' + k);
   if(el) el.value = (r[k] && r[k].value) ? r[k].value : '';
  });
  $('ev-MT5_PASSWORD').value = '';
  $('ev-LIVE_TRADING').checked = !!(r['LIVE_TRADING'] && r['LIVE_TRADING'].value === '1');
 }catch(e){ smMsg('Failed to load config: ' + (e.detail || e.message), 'bad'); }
}

async function smLoadYaml(name){
 const el = $('ya-' + name);
 if(!el) return;
 el.value = 'Loading...';
 try{ const r = await api('/config/' + name); el.value = r.yaml; }
 catch(e){ el.value = '# failed to load: ' + (e.detail || e.message); }
}

function smMsg(msg, cls){
 const el = $('smMsg');
 el.textContent = msg; el.className = 'smsg ' + (cls || '');
 if(cls) setTimeout(() => { el.textContent = ''; el.className = 'smsg'; }, 6000);
}

$('smSave').onclick = async () => {
 smMsg('Saving…', '');
 $('smSave').disabled = true;
 try{
  if(smTab === 'env'){
   const p = {author: 'dashboard'};
   ['MT5_LOGIN','MT5_SERVER','MT5_TERMINAL_PATH','STATE_DB'].forEach(k => {
    const v = $('ev-' + k).value.trim(); if(v) p[k] = v;
   });
   const pwd = $('ev-MT5_PASSWORD').value;
   if(pwd) p['MT5_PASSWORD'] = pwd;
   p['LIVE_TRADING'] = $('ev-LIVE_TRADING').checked ? '1' : '';
   await api('/config/env', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(p)});
   smMsg('Saved — restart server to apply', 'ok');
  } else if(smTab === 'strategy'){
   await api('/config', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({yaml: $('ya-strategy').value, author: 'dashboard'})});
   smMsg('Strategy config updated', 'ok');
   refresh();
  } else if(smTab === 'firm'){
   await api('/config/firm', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({yaml: $('ya-firm').value, author: 'dashboard'})});
   smMsg('Firm profile saved', 'ok');
  } else if(smTab === 'instruments'){
   await api('/config/instruments', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({yaml: $('ya-instruments').value, author: 'dashboard'})});
   smMsg('Instruments saved — restart required', 'ok');
  }
 }catch(err){
  if(err.kind === 'auth') return showLogin('Session expired — sign in again.');
  smMsg(err.detail || err.message || 'Save failed', 'bad');
 } finally { $('smSave').disabled = false; }
};
</script></body></html>
"""


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip()


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    _load_env_file(Path(".env"))

    from adapters.ff_calendar import FileCalendar
    from adapters.sqlite_store import SqliteStore
    from services.alerts import AlertService, LogSink
    from services.config_loader import (
        load_firm_profile,
        load_instruments,
        load_strategy_config,
    )

    token = os.environ.get("API_TOKEN")
    if not token:
        raise SystemExit("set API_TOKEN env var first")
    strategy, checksum = load_strategy_config("configs/strategy.yaml")
    firm, _ = load_firm_profile("configs/firm_profile.yaml")
    instruments, _ = load_instruments("configs/instruments.yaml")
    points = {s: spec.point for s, spec in instruments.instruments.items()}
    store = SqliteStore(os.environ.get("STATE_DB", "data/state.sqlite"), points=points)

    class _SystemClock:
        def now_utc(self):
            from datetime import timezone

            return datetime.now(timezone.utc)

    ctx = ApiContext(
        store=store, execution=None, calendar=FileCalendar(()), clock=_SystemClock(),
        strategy=strategy, firm=firm, config_checksum=checksum,
        kill_file=Path("data/KILL"), alerts=AlertService([LogSink()]),
        status_provider=lambda: {"phase": "IDLE", "tick_age_s": None, "bar_age_s": None,
                                 "ntp_offset_s": None, "broker_connected": False,
                                 "flattener_heartbeat_age_s": None},
    )
    uvicorn.run(create_app(ctx, token), host="127.0.0.1", port=8377)


if __name__ == "__main__":  # pragma: no cover
    main()
