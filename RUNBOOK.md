# RUNBOOK — TradePilot SSR v1

Operational procedures. The firm's current written rules supersede every default here.

## 1. Development setup (Windows)

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest          # full suite must be green before anything else
```

Linux/CI: identical except `python3.11 -m venv`; the `MetaTrader5` dependency is
Windows-only by marker and all MT5 imports are guarded.

## 2. Configuration

| File | Contents | Notes |
|---|---|---|
| `configs/strategy.yaml` | SSR v1 parameters (SPEC Appendix A + `stop_buffer_pips`) | any edit changes the checksum; mid-evaluation only risk REDUCTIONS activate (enforced by `POST /config`) |
| `configs/firm_profile.yaml` | firm rules as data (SPEC §8) | verify EVERY field against the firm's current ToS, in writing, before each evaluation |
| `configs/instruments.yaml` | canonical symbols + broker variants | boot validation refuses any symbol whose live digits/point/contract size disagree |

The engine refuses to start when: profile incomplete, `ea_policy.allowed: false`,
or internal buffers not strictly inside firm limits (`validate_startup`).

## 3. Data + backtest

```powershell
# download/refresh history (resumable; 404s cached)
.venv\Scripts\python -m services.data_downloader EURUSD GBPUSD --start 2018-01-01 --end 2026-06-05 --db data/market.sqlite --cache data/dukascopy

# verify the feed parsers against live Dukascopy (ticks vs candles must agree)
.venv\Scripts\python -m services.feed_validation EURUSD 2024-03-15

# run the campaign + stress variant; reports land in ./reports/
.venv\Scripts\python -m backtest.engine --db data/market.sqlite --start 2018-01-01 --end 2026-06-05 --label campaign
.venv\Scripts\python -m backtest.engine --db data/market.sqlite --start 2018-01-01 --end 2026-06-05 --slip-in 1.0 --slip-out 1.5 --label campaign_stress
```

Optional: `--calendar-csv path.csv` feeds a historical high-impact calendar
(columns `ts_utc,currency,impact,title`); without it the news blackout is
INACTIVE in backtests and the report says so.

## 4. API + dashboard

```powershell
$env:API_TOKEN = "<long random string>"
.venv\Scripts\python -m api.app          # binds 127.0.0.1:8377 only
```

Dashboard: http://127.0.0.1:8377/ (asks for the token once, stores in localStorage).
Remote access: SSH tunnel only — never expose the port.

Endpoints per SPEC §12. Control: `POST /pause`, `/resume`, `/flat-all`, `/kill`.

## 5. Paper trading (M5 bring-up on a real MT5 demo)

Prerequisites: Windows VPS, MT5 terminal installed and logged into the prop
firm's DEMO server, env vars `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`
(optional `MT5_TERMINAL_PATH`).

1. Boot canary: construct the adapter — `connect_from_env()` initializes the
   terminal and runs `boot_validate()`; ANY symbol mismatch refuses to start.
2. The trading loop composes `PaperSession` (services/runner.py) per symbol-day:
   MT5 supplies quotes/bars, `PaperBroker` simulates execution, the order
   manager enforces idempotency + rate limits. SL/TP2 ride as native brackets;
   TP1 partial, time stop and forced exit are handled on the quote poll.
3. Schedule the INDEPENDENT flattener (separate process, own session):
   Task Scheduler at 19:10 / 19:20 / 19:30 London plus firm-cutoff T-20/T-10/T-5:
   `python -m services.flattener --mode live` (use a separate MT5 login session).
4. Paper gates before any live order (SPEC §15): ≥ 6 weeks, ≥ 40 trades,
   signal-match ≥ 90% on replay, median entry slippage ≤ 0.5 pip EU, zero
   category-A incidents.

### Ops drills (run each at least once during paper)

- [ ] Kill switch fired from dashboard → flat, halted, re-arm only after file delete
- [ ] Mid-position process restart → reconcile reports adopt-or-flatten correctly
- [ ] Broker disconnect ≥ 5 min → entries paused (OPS_UNHEALTHY), alert sent
- [ ] Forced-flatten failure path → critical alert escalation observed
- [ ] DST-mismatch week behavior → windows verified, self-check clean

## 6. Live arming / disarming

Going live is a deliberate three-factor act (all checked per order, not per boot):

1. `LIVE_TRADING=1` in the process environment;
2. file `ARM_LIVE` exists in the working directory;
3. loaded config checksum == DB active `config_version` row.

Disarm: delete `ARM_LIVE` (fastest), or unset the env var, or activate a
different config. The default state everywhere is paper.

Kill switch: `POST /kill` (or dashboard button) flattens, writes `data/KILL`,
halts entries. **Re-arm requires manually deleting `data/KILL`** and then
`POST /resume`. An API call alone can never un-kill the system.

## 7. Incidents

Category A (any one → kill switch + post-mortem before re-arm; two in a month →
back to paper): duplicate order, missed flatten, unreconciled position,
trade on wrong symbol, order outside risk limits.

Post-mortem template: timeline (UTC), audit rows, decisions involved, root
cause, corrective change (config or code, with new checksum/version), sign-off.

## 8. Recurring ops

- Daily 20:00 London: read `GET /reports/daily` artifact; archive it.
- Weekly: re-read the firm's ToS / announcements (SPEC §19 row 23); any change →
  halt, update `firm_profile.yaml`, re-run startup validation, new config version.
- Weekly: refresh the manual calendar CSV for the week ahead if the FF feed is
  unavailable; verify blackout windows visible on the dashboard.
- Monthly: SQLite backup of `data/*.sqlite`; logs retained ≥ 1 year, DB forever.

## 9. Known v1 limitations (also in DECISIONS.md)

- Backtests ran with the news-blackout filter against an EMPTY historical
  calendar (no free archive); paper/live use the live FF feed + CSV fallback.
- Bank-holiday filter has no historical source; thin ranges are partially
  caught by the range filters.
- Dukascopy wicks ≠ your broker's wicks: before live, re-run signal detection
  on ≥ 3 months of the broker's own M1 (SPEC §13.1) and investigate mismatches.
