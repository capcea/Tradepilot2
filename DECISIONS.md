# DECISIONS.md

Running log of decisions made where SPEC.md is silent, plus per-milestone summaries.
Convention: newest milestone at the bottom; each decision states the choice and why
the conservative option was the one taken.

---

## M0 — config + timebase (2026-06-10)

### Dependencies (SPEC: "ask before adding any dependency not listed")

- **PyYAML** (added): SPEC mandates YAML config files (`configs/*.yaml`, §8, Appendix A)
  but the stack list omits a YAML parser. Treated as implied by the spec. Flagged for
  review at the M0 gate.
- **tzdata** (added): Windows has no system IANA timezone database; `zoneinfo`
  (used for Europe/London / America/New_York per §3) requires the `tzdata` wheel on
  Windows. Without it the system cannot compute sessions at all. Flagged for review.
- **HTTP client (NOT added — decision needed before M1)**: the M1 Dukascopy downloader
  and ForexFactory calendar fetch need an HTTP client; none is in the listed stack.
  Awaiting approval (proposal: `httpx`, single modern client usable sync and async).
- **git**: not installed on this machine; repo laid out git-ready, `git init` deferred
  until git is available. Config governance does not depend on git (checksums + DB
  `config_version` rows per §8c).

### Design decisions

1. **Timebase lives in `core/timebase.py`.** The repo layout in the build brief does not
   name a timebase module; session math is needed by the pure core (range lock, entry
   window, forced exit), and timezone conversion via `zoneinfo` is deterministic
   computation, not I/O. Purity holds: the module takes dates in, returns UTC instants.
2. **Config schemas are pure (`core/config_schema.py`); file I/O is in
   `services/config_loader.py`.** Keeps the no-I/O rule for `core/` intact while letting
   the core own all validation logic.
3. **Decimal policy:** every money/price/pip field is `Decimal`. YAML floats are
   converted via `Decimal(str(x))` at the model boundary so no binary-float artifact
   enters money math. Booleans are explicitly rejected as numerics (YAML `true` is an
   `int` subclass in Python).
4. **`daily_loss.basis` accepts only `equity_incl_floating`.** §8 shows only that basis;
   §7 mandates modeling the harsher variant when in doubt. A basis the risk engine does
   not model is refused at config load rather than silently approximated. Extending the
   enum requires a deliberate risk-engine change (M3).
5. **`trailing_dd.mode` keeps all three spec values** (`intraday_equity | eod_balance |
   static`) since §8 enumerates them explicitly; §7's "model the harsher variant" is a
   runtime concern for M3, not a schema restriction.
6. **DST-transition Sundays intentionally fail `self_check`** (Asian-window duration
   mismatch) → NO-TRADE day per §6.9 ("recomputed; if the computed window fails
   self-checks, the day is NO-TRADE rather than guessed"). Both transition Sundays are
   pinned in tests. FX is closed most of Sunday anyway; failing closed costs nothing.
7. **Session anchors not present in Appendix A** (Friday 16:00 London cutoff, NY
   16:45–17:15 rollover window, 17:00 NY daily reset) are defaults in
   `SessionTimesConfig` rather than new strategy.yaml keys, keeping the shipped
   strategy.yaml byte-compatible with Appendix A. `StrategyConfig.session_times()` is
   the single glue point.
8. **Schema-level risk clamps:** `per_trade_usd` ∈ [150, 250] (§7 bounds),
   `cooldown_usd ≤ per_trade_usd` (no martingale by construction),
   `day_hard_stop < day_soft_stop < 0`, `week_stop < day_hard_stop`,
   `max_entries_day ≤ 3`, `max_concurrent == 1` (EU/GU one risk unit, §2.1). Configs
   violating these never construct, independent of the runtime guards M3 will add.
9. **Startup cross-validator** (`validate_startup`) raises with machine-readable
   violation codes when internal buffers are not *strictly* inside firm limits:
   daily hard stop vs firm daily loss, floor buffer and week stop vs trailing DD,
   consistency day-cap vs `consistency_pct × profit_target`, strategy news blackout
   narrower than the firm's, and (when instruments are supplied) any traded pair
   lacking an instrument spec.
10. **Config checksums** are sha256 over newline-normalized (`\r\n → \n`) UTF-8 text so
    the same logical config hashes identically across OSes; this checksum is what the
    `config_version` row and the live-arming gate will pin (§8c, live-gating rule).
11. **Instrument sanity is FX-specific on purpose:** `point == 10^-digits` and
    `pip == 10 × point`. Metals/indices (e.g. XAUUSD) deliberately fail validation in
    v1 — per spec, XAU/USD is excluded and adding it must be an explicit model change.

### SUMMARY M0

Scope delivered: repo skeleton per spec layout; `pyproject.toml` with the full declared
stack (MetaTrader5 marked Windows-only); shipped configs byte-faithful to SPEC Appendix A
/ §8; pure UTC/session timebase with DST-aware windows, US/UK-mismatch detector, and
§6.9 self-checks; pydantic-v2 config schemas (strategy, firm profile, instruments) with
Decimal money math; config loader with governance checksums; startup validator.

Tests: 128 passed (pytest + hypothesis). Coverage includes pinned DST dates for winter,
summer, and both 2024/2025/2026 mismatch windows; range lock at 06:55 London verified
for every calendar day 2024–2025 and property-tested 2018–2030; transition-Sunday
self-check failures; firm-profile refusals (EA policy false, incomplete profile, bad
reset clause); buffer cross-validation; instrument consistency; checksum determinism.

Exit gate "all timezone tests green": MET (verified by running `python -m pytest` in
this repo; see test run output).

---

## M1 — data layer + calendar (2026-06-10)

### Dependencies

- **httpx** added (downloader + calendar fetch). The M0 summary proposed it explicitly;
  the user's instruction to proceed with the whole roadmap is taken as approval.

### Design decisions

1. **Bulk history comes from Dukascopy daily M1-candle files (BID+ASK), aggregated to
   M5 in `services/bar_builder.py`; hourly tick archives remain implemented and are the
   validation path.** Rationale: ticks for 2018–present × 2 pairs ≈ 100k+ HTTP requests
   / several GB, infeasible here; the M1-candle path is ~1/1000th the requests on the
   same feed. Empirical cross-check (`services/feed_validation.py`, run live against
   Dukascopy for EURUSD 2024-03-15): tick-built vs candle-built M5 agreed on 252/252
   common bars with max diff 0 points, confirming both bi5 parsers (tick records are
   ms/ASK/BID/vols; candle records are secs/Open/Close/Low/High/vol).
2. **`M5Bar` carries bid AND ask OHLC**; SSR predicates (M2) evaluate on the bid side
   (the MT5 chart feed) while ask exists for spread observability and costing (§13.1).
   For M1-built bars, `spread_median` is the median of per-minute open spreads —
   documented proxy for the tick-grade median.
3. **Candle persistence**: spec §11 `candle` table has one OHLC per row, so an M5 bar
   stores as two rows: `tf='M5'` (bid, carries `spread_pts`) and `tf='M5A'` (ask).
   `get_candles` reconstructs full bars and only returns timestamps present on both
   sides.
4. **Decimal vs REAL columns**: §11 declares REAL. Values are written as floats and read
   back via `Decimal(str(x))`, which round-trips exactly at these magnitudes (≤ 15
   significant digits). Money is never aggregated in SQL — only in Python with Decimal.
5. **Zero-volume flat padding candles** (e.g. the 21:00–24:00 UTC block after Friday
   close, observed live) are dropped at DB build time; the pure builder keeps them so
   the validator can see the raw feed.
6. **404 responses are cached as empty files** so weekend/holiday probes cost one
   request ever and re-runs are offline-reproducible.
7. **Audit append-only is enforced in the database** (BEFORE UPDATE/DELETE triggers
   that RAISE(ABORT)), not just by code discipline.
8. **Idempotency at the DB layer**: `order_intent.idempotency_key` UNIQUE; the store
   raises a typed `IdempotencyViolation` — double-send is impossible regardless of
   caller bugs (§10.5, tested).
9. **Calendar history limitation (flagged)**: ForexFactory's free feed serves the
   current week only; there is no clean free archive of historical high-impact events.
   The adapter supports a manual CSV fallback for any window. How the M4 backtest
   handles the missing-history case will be decided and documented at M4 — it will NOT
   silently pretend blackouts were applied.

### SUMMARY M1

Delivered: five port contracts (`ports/`), core event/value types (`core/events.py`),
Dukascopy downloader with cached retrying fetch + pure bi5 parsers + bulk DB builder
CLI, M5 bar builder (ticks and M1 paths) with gap detection, ForexFactory/CSV calendar
adapter, SQLite store implementing the full §11 schema with append-only audit and
config-activation governance. Live feed validation passed (exact agreement). Full
2018–present download for EURUSD+GBPUSD launched (resumable; completion logged at M4).
Suite: 166 tests passing.

---

## M2 — pure SSR core + filter pipeline (2026-06-10)

### Design decisions

1. **`stop_buffer_pips` added to per-pair config** (2 EURUSD / 3 GBPUSD). §3.1 specifies
   it but Appendix A omits it; an explicit config key beats a hardcoded constant. The
   shipped strategy.yaml now deviates from Appendix A by exactly this one key per pair.
2. **All OHLC predicates evaluate on the BID side** (the MT5 chart feed). Ask exists on
   every bar for spread observability and entry costing.
3. **Sweep invalidation reading of §4.2**: the two stated conditions ("closes below
   range_low − sweep_max_pen" / "penetration exceeds sweep_max_pen") collapse to the
   wick test, since close ≥ low. Implemented: direction INVALID iff penetration beyond
   the extreme exceeds sweep_max_pen. A bar may *close* outside the range (within
   max_pen) and the attempt stays alive pending reclaim within `reclaim_bars`.
4. **Reclaim semantics**: the FIRST bar closing back inside the range by ≥ 1 pip is the
   only reclaim candidate. If its top/bottom-40% quality fails, the direction attempt is
   consumed (RECLAIM_QUALITY) — "a pair whose setup was vetoed does not retry the same
   direction that day" (§3.2). The same bar that first penetrates may itself reclaim.
   `sweep_extreme` is the extreme over the penetration sequence including the reclaim bar.
5. **Sweep detection window**: bars with ts_open ≥ entry_start and ts_close ≤ entry_end.
   Sweeps in the 06:55–07:05 gap or after 10:30 are ignored by design.
6. **Decision-time spread** = reclaim bar's `spread_median`; the at-send re-check (§3.1
   "checked at decision and at send") happens in the order manager (M5).
7. **Degenerate target structure** (tp2 ≤ tp1, possible when the reclaim closes deep
   into the range) → skip with `TARGET_STRUCTURE_INVALID` rather than trade a malformed
   bracket. Spec is silent; conservative option taken.
8. **Same-bar SL-before-TP is core management law** (`manage_on_bar`), not just a
   backtest fill assumption; breakeven SL set by TP1 binds from the NEXT bar (intrabar
   ordering unknowable on closed bars). Forced exit outranks the time stop on the same
   bar. TP1 quantity = tp1_close fraction of ORIGINAL size; execution layer rounds to
   lot step.
9. **NoTradeDay precedence**: DST/window violations are announced before anything else
   (first bar of the day); ADR20 unavailability and missing Asian bars (> 2) surface at
   range lock as DATA_INCOMPLETE.
10. **Engine emits value objects** (RangeLocked / NoTradeDay / AttemptEnded /
    SetupDetected); decision-row persistence is the shells' job, guaranteeing identical
    core behavior in backtest and live.

### SUMMARY M2

Delivered: `core/reasons.py` (single ReasonCode enum), `core/state_machine.py` (§3.2
with any→HALTED), `core/filters.py` (complete §6 list, every failure reported, plus
in-house ADR/ATR/percentile/Monday-gap helpers), `core/strategy_ssr.py` (SSREngine
detection + pure position management). Synthetic fixtures: clean long, clean short,
deep-sweep skip, no-reclaim expiry, reclaim-quality fail, stop-out-of-bounds, DST
anomaly, narrow/wide range, missing data, plus spread-gate and blackout filter cases.
Exit gate "100% of fixture cases pass": MET — suite 238 tests passing.

---

## M3 — risk + compliance + sizing (2026-06-10)

### Design decisions

1. **All three engines are pure**: frozen dataclass state + transition functions, so
   hypothesis can drive thousands of adversarial paths. Shells own persistence/clocks.
2. **Daily soft/hard stops evaluate realized + floating** (§7 "realized + floating");
   the hard stop **latches** for the day once triggered — recovery of floating P&L does
   not un-halt. Consistency cap also counts floating (conservative: blocks earlier).
3. **Scratch trades (P&L exactly 0) do not reset the consecutive-loss streak**
   (conservative reading; spec silent).
4. **Cooldown semantics** (§7 "auto-reduced to $125 the day after a 2-loss day,
   restored after a green day"): cooldown turns on at day-roll after a day with ≥ 2
   losing trades; stays on through flat/red days; turns off only after a strictly
   positive day. Risk can therefore never exceed `per_trade_usd` (property-tested).
5. **Weekly stop resets on ISO-week boundary** (Monday anchor); checked against weekly
   realized + current floating.
6. **Trailing floor model**: floor is monotone non-decreasing in every mode
   (property-tested); intraday_equity ratchets on every equity observation — the
   harshest variant per §7. The floor is NOT capped at the initial balance (some firms
   stop trailing at breakeven; modeling the un-capped variant is harsher, so kept).
7. **Margin/leverage**: leverage is not in the firm profile (§8 yaml) — it comes from
   the broker account at runtime, so `margin_ok` takes it as an input. 60% free-margin
   floor per §7.
8. **REQUIRED property verified**: random equity paths (trade outcomes in
   [-$350, +$500], i.e. max risk + worst-case slippage) with the $800 floor-buffer
   entry guard active never breach the modeled trailing floor (300 hypothesis examples
   each run); consistency guard halts exactly at threshold.

### SUMMARY M3

Delivered: `core/sizing.py` (floor-to-step sizing per §4.5, pip value, margin check),
`core/risk.py` (daily/weekly/consecutive/consistency/max-entries internal rules with
cooldown), `core/compliance.py` (trailing floor in three modes, firm daily-loss
tracking, floor-buffer entry gate). Exit gate "trailing-floor and consistency guards
proven on adversarial paths": MET — suite 274 tests passing.

---

## M4 — backtester + Monte Carlo + campaign (2026-06-10)

### Design decisions

1. **Fill model (§13.3)**: entries buy the ask (bid close + observed per-bar spread)
   + 0.3p slippage; ALL exits pay 0.4p slippage including limit-style TPs
   (conservative); SLs gapped over fill from the bar open; $7/lot round-turn
   commission charged on closed quantity. Same-bar SL-before-TP is core law
   (`manage_on_bar`) and re-verified through the fill layer.
2. **Compliance floor re-anchors daily in the backtest.** The campaign measures the
   strategy's trade distribution under the day-level rules; the multi-day trailing
   floor RACE is exactly what the §13.6 Monte Carlo simulates from that distribution.
   A cumulative floor would have conflated one specific 8-year account path with the
   distribution (and silently truncated the sample after the first bad stretch).
3. **ADR percentile requires ≥ 60 days of ADR history** before the regime filter is
   trusted; before that the day is DATA_INCOMPLETE (a percentile over a handful of
   points is noise).
4. **Monte Carlo**: 5-trade block bootstrap of trade-level R; day structure resampled
   from the campaign's empirical trades-per-day distribution (including zero-trade
   days); daily soft stop and consecutive-loss halt applied inside simulated days;
   bust = equity touching the trailing floor; 120-day attempt horizon; deterministic
   seed.
5. **No parameter search was performed.** Parameters are the spec's Appendix-A
   defaults; exactly 2 variants ever ran (baseline + slippage stress), recorded here
   per §13.7. There is therefore no walk-forward overfitting concern to correct for —
   and no tuning was attempted after seeing results (§14 "no silent re-tuning").
6. **Known inactive filters in the campaign** (stated in the report itself): news
   blackout ran against an empty historical calendar (no free archive exists; live
   uses the FF feed + CSV fallback) and the bank-holiday filter had no source. Both
   would most plausibly REMOVE a few bad trades; neither plausibly flips the sign of
   a -0.13R expectancy.

### Campaign results (real runs on real downloaded data; reports/ for full detail)

Data: Dukascopy bid+ask M1→M5, 2018-01-01..2026-06-05, ~630k bars/pair/side,
validated against tick-built bars (exact agreement on the cross-check day).

| Run | Trades | Win rate | Avg win | Avg loss | Expectancy | PF | Net | MC pass prob |
|---|---|---|---|---|---|---|---|---|
| Baseline (0.3/0.4p slip) | 669 | 49.9% | +0.757R | -1.025R | **-0.132R** | **0.741** | -$15,360 | **~0.000** |
| Stress (1.0/1.5p slip) | 669 | 48.0% | +0.628R | -1.131R | -0.287R | 0.512 | -$33,285 | ~0.000 |

Sample-size gate (§13.8): 669 trades ≥ 300 ✓, 8.4 calendar years ≥ 3 ✓, spans the
2019 low-vol and 2020 crisis-vol regimes ✓ — the conclusion is admissible.
Only 2021 was net positive (PF 1.25); every other year had PF < 1.

### GO/KILL DECISION: **KILL** (do not trade SSR v1 as parameterized)

The spec's pre-committed falsification trigger (§2.3) is met: out-of-sample profit
factor 0.74 < 1.05 after realistic costs over ≥ 300 trades across ≥ 3 regimes.
The win rate landed inside the hypothesized 48–52% band, but the exit structure
delivered an average win of 0.76R — far below the hypothesized 1.0–1.3R — because
TP1-at-1R halves the position early while breakeven stops and time stops truncate
the remainder; at these numbers the breakeven win rate is ≈ 57%, not the spec's
45–48%. Per §14 and Appendix B, the correct outputs are: do not go live; any revision
(e.g. exit-structure hypothesis) requires a NEW spec version and a full re-run —
not parameter tuning on this result. This outcome cost a few weeks of build and zero
drawdown, which is the best bad outcome available (Appendix B).

### SUMMARY M4

Delivered: `backtest/costs.py`, `backtest/fills.py`, `backtest/engine.py` (same pure
core via backtest adapters, no look-ahead), `backtest/montecarlo.py`,
`backtest/reports.py`, campaign CLI; full 2018–2026 campaign + slippage stress run
executed on real downloaded data; reports generated (`reports/campaign_2018_2026.md`
/ `.html`, `reports/campaign_stress_slippage.md` / `.html`). Exit gate "full campaign
run; go/kill decision on strategy": MET — decision is KILL.

---

## M5 — execution layer + paper mode (2026-06-10)

### Design decisions

1. **Rate limiter binds ENTRIES only.** §10.5's "max 1 entry/5 min, max 6 order
   ops/day" is implemented so that closes and SL-tightening are counted but never
   blocked — a limiter able to prevent flattening would be a hazard, not a safeguard.
   The runaway-loop property is tested: 20 attempted entries land ≤ 6 broker sends.
2. **Idempotency is a DB-layer property** (UNIQUE key inserted BEFORE any send), so
   double-send is impossible regardless of caller bugs; tested with duplicate submits.
3. **Partial fills resize management, not intent**: the managed position is built from
   the ACTUAL filled lots (TP1 quantities and remainder derive from it); the intent row
   keeps the requested size, the fill row the actual.
4. **Reconcile policy**: positions matched by ticket to today's intents are kept;
   anything else is flattened + alerted by default; adopting (same magic) is an
   explicit opt-in flag — matching the spec's "default: flatten + alert".
5. **Bracket split between broker and client** (live/paper): SL and TP2 ride ON the
   broker natively from entry (§4.7 "never managed only in client memory"); TP1
   partial + breakeven move, time stop, forced exit run client-side on the quote poll
   (`core.manage_on_quote`, pure). MT5 cannot express a partial-close TP natively.
6. **Paper fill assumptions**: entries at current quote, bracket exits AT the level —
   deliberately simple; the §15 slippage audit exists precisely to measure the gap to
   reality.
7. **MT5 adapter**: every pure part (request building incl. filling-mode selection,
   retcode→retryable mapping, symbol validation) is unit-tested against a fake API;
   the thin I/O wrappers require a terminal and are exercised during paper bring-up
   (RUNBOOK §5). The live gate is checked per-order BEFORE touching the API.
8. **Watchdog**: a failed NTP query does NOT set the skew flag (unknown ≠ wrong);
   it alerts instead. Stale tick (>10 s) or stale bar (>7 min) both gate entries.
9. **Flattener** verifies flat by re-querying the broker (never trusts its own sends),
   retries up to 3 rounds, then escalates with a critical alert.

### SUMMARY M5

Delivered: `services/order_manager.py` (+RateLimiter), `services/live_gate.py`
(3-factor arming), `services/flattener.py` (independent entrypoint),
`services/watchdog.py` (incl. stdlib SNTP client), `services/alerts.py`
(Telegram/log fan-out), `adapters/paper/` (quote-driven bracket simulation),
`adapters/mt5/` (import-guarded), `services/runner.py` (PaperSession: same core,
ports + order manager), `core.manage_on_quote`. REQUIRED tests all present and green:
idempotency double-send, partial-fill resize, restart reconcile adopt-or-flatten,
rate limiter, flattener fake-broker + escalation, live-gate combinations, end-to-end
paper day (entry → TP1 partial+BE → TP2 bracket close → realized). Real-terminal ops
drills are an operator activity listed in RUNBOOK §5.

---

## M6 — API + dashboard + runbook (2026-06-10)

### Design decisions

1. **Kill re-arm is file-deletion, not an API call**: `/kill` flattens, writes
   `data/KILL`, pauses; `/resume` returns 409 while the file exists. A compromised or
   buggy client cannot un-kill the system.
2. **Config governance endpoint** (`POST /config`): validates schema + startup
   invariants, computes the checksum, inserts an INACTIVE `config_version` row, then
   activates — and mid-evaluation accepts only changes where every risk field is equal
   or strictly safer and non-risk sections are untouched (`risk_reduction_only`,
   pure + tested).
3. **`GET /orders` returns `[]` in v1**: the system trades market-in/bracket-out and
   never rests orders; an honest empty list beats inventing data.
4. **Decimal-over-JSON**: all money crosses the API as strings (exact), never floats.
5. **Dashboard** is a single inline HTML page (§17's four zones, confirm dialogs on
   flat/kill) calling the token-guarded JSON API; token kept in localStorage;
   localhost-bound, SSH tunnel for remote.

### SUMMARY M6

Delivered: `api/auth.py` (constant-time bearer token), `api/app.py` (all §12
endpoints + dashboard + audit rows on every mutation), store day-queries, RUNBOOK.md
(full ops procedures: arming, drills, incidents, recurring tasks), Makefile targets.
Suite: 378 tests passing.
