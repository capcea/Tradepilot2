"""FastAPI control surface (SPEC.md S12, S17): localhost-bound, token auth.

Every mutating endpoint writes an audit row. The kill switch flattens, writes
the KILL file and halts; re-arming requires manually deleting the file -- an
API call alone can never un-kill the system.

Run:  python -m api.app  (binds 127.0.0.1 only; set API_TOKEN env var)
"""
from __future__ import annotations

import json
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
<html><head><meta charset="utf-8"><title>TradePilot - SSR v1</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111;color:#ddd;margin:0;padding:1em}
 .zone{border:1px solid #333;border-radius:6px;padding:1em;margin-bottom:1em}
 .lights span{display:inline-block;margin-right:1.2em}
 .ok{color:#4c4}.bad{color:#e55}
 button{background:#222;color:#ddd;border:1px solid #555;padding:.5em 1em;margin-right:.6em;cursor:pointer}
 button.danger{border-color:#a33;color:#f88}
 table{border-collapse:collapse;width:100%}td,th{border:1px solid #333;padding:.3em .6em;font-size:.85em}
 h1{font-size:1.2em}h2{font-size:1em;color:#9ab}
</style></head><body>
<h1>TradePilot SSR v1 <small id="cfg"></small></h1>
<div class="zone"><h2>Status</h2><div class="lights" id="lights">loading...</div>
 <p>
  <button onclick="act('pause')">Pause</button>
  <button onclick="act('resume')">Resume</button>
  <button class="danger" onclick="if(confirm('Flatten ALL positions now?'))act('flat-all')">Flat all</button>
  <button class="danger" onclick="if(confirm('KILL: flatten, halt, manual re-arm. Sure?'))act('kill')">KILL</button>
 </p></div>
<div class="zone"><h2>Risk</h2><div id="risk">...</div></div>
<div class="zone"><h2>Open position</h2><div id="pos">...</div></div>
<div class="zone"><h2>Today's setups &amp; decisions</h2><div id="setups">...</div></div>
<div class="zone"><h2>Upcoming events (12h)</h2><div id="cal">...</div></div>
<script>
const tok = localStorage.token || (localStorage.token = prompt('API token'));
const H = {headers: {Authorization: 'Bearer ' + tok}};
const get = p => fetch(p, H).then(r => r.json());
async function act(p){ await fetch('/'+p, {...H, method:'POST'}); refresh(); }
function table(rows, cols){ if(!rows.length) return '<i>none</i>';
 return '<table><tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+'</tr>'+
  rows.map(r=>'<tr>'+cols.map(c=>'<td>'+(r[c]??'')+'</td>').join('')+'</tr>').join('')+'</table>';}
async function refresh(){
 try{
  const [h, s, r, pos, cal] = await Promise.all([
    get('/health'), get('/state'), get('/risk'), get('/positions'), get('/calendar/next')]);
  document.getElementById('cfg').textContent = 'config ' + (s.config_checksum||'').slice(0,12);
  const light=(name, ok)=>'<span class="'+(ok?'ok':'bad')+'">&#9679; '+name+'</span>';
  document.getElementById('lights').innerHTML =
    light('broker', h.broker_connected) + light('tick '+h.tick_age_s+'s', h.tick_age_s < 10) +
    light('bar '+h.bar_age_s+'s', h.bar_age_s < 420) + light('ntp', Math.abs(h.ntp_offset_s||0) < 2) +
    light('flattener', h.flattener_heartbeat_age_s < 1200) +
    light(s.paused ? 'PAUSED' : 'active', !s.paused) + light(s.killed ? 'KILLED' : 'armed-ok', !s.killed) +
    '<span>phase: '+s.phase+'</span>';
  document.getElementById('risk').innerHTML =
    'day P&amp;L <b>'+r.day_realized+'</b> (soft '+r.internal_day_soft_stop+' / hard '+r.internal_day_hard_stop+')'+
    ' | trades '+r.trades_today+' | consec losses '+r.consec_losses+
    ' | headroom '+r.consistency_headroom+' | equity '+(r.equity||'n/a');
  document.getElementById('pos').innerHTML = table(pos, ['ticket','symbol','side','lots','entry_price','sl','tp','unrealized_pnl']);
  document.getElementById('cal').innerHTML = table(cal, ['ts_utc','currency','impact','title']);
  const today = new Date().toISOString().slice(0,10);
  const setups = await get('/setups?date='+today);
  let html = table(setups, ['id','direction','status']);
  for(const s2 of setups){
    const ds = await get('/decisions?setup_id='+encodeURIComponent(s2.id));
    html += table(ds, ['stage','passed','reason_code']);
  }
  document.getElementById('setups').innerHTML = html;
 }catch(e){ document.getElementById('lights').innerHTML='<span class="bad">API error: '+e+'</span>'; }
}
refresh(); setInterval(refresh, 5000);
</script></body></html>
"""


def main() -> None:  # pragma: no cover - process entrypoint
    import os

    import uvicorn

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
