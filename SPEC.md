# Autonomous FX Prop-Trading System
## Quant Research Memo & Engineering Specification — v1.0

Scope: forex/CFD prop-firm evaluation and funded accounts (MT5 first). Reference account: $50,000 / +$3,000 target / $2,500 trailing drawdown / $1,250 daily loss limit.

---

## 0. Honesty Preamble — read this before anything else

This document is the output of the requested seven research passes. Before the memo, five things that an honest desk would put on page one:

1. **No rulebook on paper has a known positive expectancy.** The rules in Section 4 are a *falsifiable hypothesis* with a plausible behavioral rationale. The platform's primary job is to validate or kill that hypothesis cheaply and safely — on your broker's feed, with your firm's rules — before real money is at stake. Anyone handing you "exact rules" and claiming they are profitable is selling something.

2. **Prop-firm economics are stacked against you by design.** A $3,000 target against a $2,500 trailing drawdown is a stochastic race: even a genuinely positive-expectancy system fails individual evaluations with material probability. Budget for multiple attempts, treat evaluation fees as a cost of doing business, and model pass probability explicitly (Section 14.6). Most CFD prop "funded" accounts are simulated environments with profit-share payouts — there is counterparty and rule-change risk that no code can remove.

3. **Check the firm's automation policy before writing a line of code.** Many forex prop firms restrict or ban EAs, third-party algorithms, copy trading, tick scalping, HFT, and "strategies identical across multiple accounts." Some flag accounts whose trades match other clients' (a real risk if a published bot is used by many people). This is the single most common non-market way automated traders lose accounts. Get the policy in writing.

4. **Parts of the uploaded research report were deliberately dropped.** Order-flow/footprint confirmation (volume delta, CVD, depth) is not implementable on MT5 CFD feeds — there is no centralized tape; you only get broker tick volume. LLM sentiment in the live execution path adds latency, nondeterminism, and audit problems for no demonstrated edge. The report's headline statistics (e.g., "89% of volume is AI", "14.2% AI fund returns", "3–5% NLP alpha") should be treated as unverified marketing-grade figures, not design inputs.

5. **"Foolproof" here means engineered against failure modes** — survival, compliance, auditability — not guaranteed profit. Section 20 is therefore as important as Section 4.

Nothing in this memo is financial advice. It is an engineering and research specification.

---

## 1. Executive Summary

**Selected strategy:** *Session Sweep-and-Reclaim (SSR)* — fade failed breakouts of the Asian-session range at the London open (New York open added in Phase 2) on **EUR/USD (primary)** and **GBP/USD (secondary)**. Long when price sweeps liquidity below the range low and closes back inside; short on the mirror. Stop beyond the sweep extreme; scaled exits at +1R and ~+2R; flat well before rollover and session end; hard news blackouts; 0–3 trades/day.

**Why this strategy:** it was not chosen because it "wins more" — that is unknowable pre-test — but because it best fits the *constraint geometry* of forex prop accounts: a higher-win-rate / capped-loss profile suits trailing drawdowns, consecutive-loss stops, and consistency rules far better than lumpy breakout/trend systems; it is fully objective (definable on M5 OHLC alone, no order-flow data needed); it is low-frequency, latency-insensitive, and session-anchored so news filtering integrates cleanly.

**Platform:** a single, config-driven Python system with a *pure strategy core* shared verbatim between backtester, paper, and live (hexagonal/ports-and-adapters), MT5 as the first execution adapter (cTrader/DXtrade adapters specified for later), a rules-as-code prop-compliance engine with conservative internal buffers ($600 internal daily stop vs. $1,250 firm limit; $800 buffer above the trailing floor), an independent end-of-day flattener process, idempotent order handling with restart reconciliation, structured audit logging where *every non-trade has a reason code*, and a kill switch reachable by file, API, and dashboard.

**AI layer:** v1 ships with deterministic filters only. Phase 2 adds a gradient-boosted *veto-only* classifier trained on ≥400 logged setups, with a veto-rate circuit breaker and drift monitoring. No LLM ever sits in the execution path; LLMs are used offline for research summaries, post-trade audit narratives, and code review.

**Rollout:** backtest (tick-grade data, conservative fills) → parameter-plateau and walk-forward validation → ≥6 weeks paper with signal-match and slippage audits → smallest evaluation at reduced risk. Kill criteria are pre-committed at every gate.

**Realistic expectation:** transaction costs consume roughly 5–8% of risk (R) per trade on EUR/USD with this design; the hypothesis needs roughly a 48–52% win rate at the specified exit structure to clear costs. Whether it does is an empirical question this platform is built to answer. Expected build effort: 6–10 weeks part-time with AI-assisted coding.

---

## 2. Market Selection and Best-Strategy Selection

### 2.1 Pair evaluation (indicative figures — re-measure on your broker's feed)

Figures are typical for raw-spread prop/CFD feeds in recent years; spreads exclude ~$6–7/lot round-turn commission. ADR = 14–20 day average daily range.

| Pair | Typical raw spread | ADR (pips) | London-open behavior | News risk | Slippage risk | Automation fit | Verdict |
|---|---|---|---|---|---|---|---|
| EUR/USD | 0.1–0.3 | 60–85 | Clean range→expansion; deepest book | High but well-scheduled (EUR+USD) | Lowest of all | Excellent (universal on MT5) | **Primary** |
| GBP/USD | 0.3–0.8 | 80–110 | Strong sweep amplitude; whippier | High (GBP data + USD) | Low–moderate | Excellent | **Secondary** |
| USD/JPY | 0.2–0.5 | 70–110 | Asia-centric; London less structured | BoJ/MoF intervention tail risk (2022–24 precedent) | Moderate, spikes on intervention | Good | Defer |
| XAU/USD | 10–35¢ (spikes ≫) | $25–50 | Largest sweeps, best amplitude | Extreme; spreads can 5–10× on red news | High | Good but unforgiving | Phase 3 only, with halved risk, volatility-scaled stops, stricter blackouts |
| GBP/JPY | 0.8–2.0 | 120–180 | Violent | High, two volatile legs | High | OK | Reject |
| EUR/JPY | 0.6–1.5 | 90–140 | Violent | High | Moderate–high | OK | Reject |
| AUD/USD | 0.2–0.6 | 45–65 | Muted at London (Asia-centric pair) | Moderate (AUD overnight) | Low | Good | Optional later |
| USD/CAD | 0.3–0.9 | 60–90 | OK; oil-driven noise | CAD+USD releases collide at 8:30 ET | Moderate | Good | Reject for v1 |

**Decision:** EUR/USD primary, GBP/USD secondary, treated as **one correlated risk unit** (typical correlation 0.6–0.85): never hold positions in both simultaneously in v1. XAU/USD is explicitly excluded from v1; it is the pair most likely to blow a trailing drawdown through spread/slippage shock, and several firms apply special restrictions to it.

### 2.2 Strategy universe scored (Passes 1–3)

Scoring 1–5 (5 = best) against prop constraints. "Data feasibility" = can it be built from what an MT5 CFD feed actually provides (OHLC + bid/ask + tick volume; **no** real volume, depth, or tape).

| Strategy | Automatable | Drawdown smoothness | Consistency-rule fit | News/ToS risk | Data feasibility | Latency sens. | Verdict |
|---|---|---|---|---|---|---|---|
| Liquidity sweep / stop-run reversal (objective form) | 5 | 4 | 4 | 4 | 5 | 5 | **Selected (as SSR)** |
| Opening range breakout (continuation) | 5 | 2 (low WR, lumpy wins) | 2 (big-day concentration) | 4 | 5 | 4 | Runner-up; Phase 3 module |
| VWAP mean reversion | 3 (tick-volume VWAP is unstable in FX) | 2 (trend-day tail) | 4 until the tail hits | 4 | 2 | 4 | Reject for v1 |
| Trend continuation (HTF bias + pullback) | 4 | 3 | 3 | 4 | 5 | 5 | Viable Phase 3 complement |
| Breakout continuation | 5 | 2 | 2 | 4 | 5 | 4 | Folded into ORB runner-up |
| Order-flow imbalance | 2 | – | – | – | 1 (no real order flow on CFDs) | 2 | **Killed** |
| Microstructure (footprint/CVD/depth) | 1 | – | – | – | 1 | 1 | **Killed** |
| Volatility expansion/contraction | 5 (as a filter) | n/a | n/a | 5 | 5 | 5 | **Adopted as filter, not signal** |
| News-reaction trading | 4 | 1 | 1 | 1 (explicit ToS violations at many firms; profits voided) | 4 | 2 | **Killed** |
| Statistical arbitrage / pairs | 3 | 2 (hidden tail) | 3 | 2 (hedging-rule conflicts) | 3 | 2 | **Killed** (costs, latency, rule conflicts) |
| AI/NLP sentiment as primary signal | 2 | unknown | unknown | 2 (opacity, audit) | 2 (paid data, latency) | 3 | **Killed as signal**; veto-only ML in Phase 2 |

### 2.3 Why SSR beats the alternatives (Pass 4)

The decisive argument is the account's constraint geometry, not claimed alpha:

- A $2,500 **trailing** drawdown punishes equity-curve lumpiness more than it punishes mediocre expectancy. Sweep-and-reclaim is structurally a higher-win-rate, capped-loss style (win ≈ 1.0–1.3R average, loss = 1R + slippage), which produces smoother curves than breakout systems whose P&L concentrates in a few large trend days.
- **Consistency rules** (no single day > 30–50% of profits) actively penalize exactly the days breakout systems live on. SSR's per-day P&L is naturally capped by the 3-trade limit and the +$700 day guard.
- **Consecutive-loss stops** (halt after 2–3 losses) interact terribly with low-win-rate systems, which routinely string 4–6 losses; at ~50% WR the 2-loss halt triggers far less often and costs less expectancy.
- It is the most **objectively definable** member of the "smart-money" family in the uploaded report: range, sweep, and reclaim are pure OHLC predicates — no discretionary "order blocks," no order-flow data the platform cannot get.
- It is **slow**: decisions on closed M5 bars, 0–3 trades/day, no latency edge required — realistic on a retail VPS against an MT5 poll-based API.

**Known weaknesses (stated up front):** SSR loses on genuine trend days when the "sweep" was a real breakout (mitigated by the deep-sweep skip rule and optional regime filter, Section 7); sweep detection depends on *your broker's wicks* — backtests must use data of the same grade as the live feed; the Asian-range anchor shifts with DST and must be timezone-engineered, not hardcoded; and in strong one-way macro regimes (e.g., 2022-style USD trends) signal quality degrades — the weekly loss stop and regime filter exist for this.

**What would falsify the hypothesis:** out-of-sample profit factor < 1.05 after realistic costs over ≥300 trades spanning ≥3 distinct volatility regimes; or live/paper win rate persistently > 5 percentage points below backtest on matched signals. Pre-commit to killing or revising the strategy on those triggers — do not tune parameters until the light turns green.

---

## 3. Final Strategy Rulebook — Session Sweep-and-Reclaim (SSR) v1

All logic runs on **closed M5 bars** of the broker feed. All timestamps stored UTC; session anchors defined in `Europe/London` local time and converted daily (DST-aware). Pip = 0.0001 for EUR/USD and GBP/USD.

### 3.1 Parameters (defaults; every one lives in versioned config)

| Param | Default EUR/USD | Default GBP/USD | Notes |
|---|---|---|---|
| `asian_range_window` | 00:00–06:55 London | same | Defines range high/low |
| `entry_window` | 07:05–10:30 London | same | New entries only inside this |
| `range_min_pips` | 10 | 13 | Below → no-trade day |
| `range_max` | 0.60 × ADR20 | 0.60 × ADR20 | Above → event regime, no-trade |
| `sweep_min_pen` | max(2 pips, 0.10 × range) | max(3, 0.10 × range) | Minimum penetration beyond extreme |
| `sweep_max_pen` | 0.60 × range | 0.60 × range | Deeper = treat as real breakout → skip |
| `reclaim_bars` | 6 | 6 | M5 bars allowed between first penetration and reclaim close |
| `reclaim_quality` | close in top/bottom 40% of bar | same | Long: close in top 40% of reclaim bar; short mirror |
| `stop_buffer` | 2 pips + current spread | 3 pips + spread | Beyond sweep extreme |
| `stop_min / stop_max` | 7 / 22 pips | 9 / 28 pips | Outside bounds → skip setup |
| `risk_per_trade` | $175 (0.35%) | $175 | Range allowed: $150–$250 |
| `tp1` | +1.0R, close 50%, SL→entry | same | |
| `tp2` | min(+2.2R, opposite range extreme − 1 pip) | same | |
| `time_stop` | exit if < +0.5R after 90 min | same | |
| `forced_exit` | 19:30 London | same | All positions flat |
| `max_entries_day` | 3 (system-wide) | — | Hard, enforced in execution layer too |
| `consec_loss_halt` | 2 | — | Day halted |
| `max_concurrent` | 1 position total | — | EU+GU are one risk unit |
| `spread_gate` | spread ≤ 1.8 × rolling 60-min median AND ≤ 1.2 pips abs | ≤ 1.8× AND ≤ 1.8 pips | Checked at decision **and** at send |
| `max_deviation` | 1.5 pips | 2.0 pips | MT5 order deviation cap |
| `news_blackout` | −10 min / +20 min around high-impact events for the pair's two currencies | same | Configurable to firm's stricter rule |

### 3.2 State machine

```
IDLE → RANGE_LOCKED (06:55) → [SWEPT_LOW | SWEPT_HIGH] → RECLAIMED
     → FILTERS_PASSED → ORDER_SENT → MANAGING (TP1/TP2/time/forced exits)
     → FLAT → (next setup or day end)
Any state → HALTED on risk/compliance/ops trigger.
```

One long attempt and one short attempt are permitted per pair per day; a pair whose setup was vetoed by filters does not retry the same direction that day.

---

## 4. Exact Long Setup

1. At 06:55 London, lock `range_high`/`range_low` from the Asian window. Verify `range_min_pips ≤ width ≤ 0.60 × ADR20`; else mark the pair NO-TRADE for the day.
2. Inside the entry window, detect **sweep**: an M5 bar's low penetrates `range_low` by ≥ `sweep_min_pen`. If any bar *closes* below `range_low − sweep_max_pen` reached, or penetration exceeds `sweep_max_pen`, mark direction INVALID (treat as real breakout) — no long today.
3. Detect **reclaim**: within `reclaim_bars` of the first penetration bar, an M5 bar closes back **inside** the range by ≥ 1 pip, with close in the top 40% of that bar's range. Record `sweep_low` = lowest low of the penetration sequence.
4. Run filters (Section 6 no-trade conditions + Section 7 risk gates + spread gate). Every failure writes a `decision` row with a reason code.
5. **Entry:** market order at the reclaim bar's close (next tick), `max_deviation` enforced. Lots = `floor_to_lot_step( risk_usd / (stop_pips × pip_value_per_lot) )`; pip value $10/standard lot for USD-quoted pairs; skip if computed size < broker min lot.
6. **Stop:** `sweep_low − stop_buffer`. If stop distance ∉ [stop_min, stop_max] → skip (writes reason code `STOP_OOB`).
7. **Targets/management:** TP1 +1.0R close 50% and move SL to entry; TP2 = min(+2.2R, `range_high` − 1 pip) on remainder; time stop at 90 min if open P&L < +0.5R; unconditional flat at 19:30 London. SL/TP attached natively in the MT5 order (bracket), never managed only in client memory.

## 5. Exact Short Setup

Mirror of Section 4: sweep above `range_high` by ≥ `sweep_min_pen` (and ≤ `sweep_max_pen`, no close above), reclaim close back inside the range in the **bottom 40%** of the reclaim bar within `reclaim_bars`; entry at reclaim close; stop at `sweep_high + stop_buffer`; TP1 +1R (50%), TP2 = max(+2.2R cap, `range_low` + 1 pip equivalent); same time and forced exits.

---

## 6. No-Trade Conditions (complete list)

A setup is skipped — with a logged reason code — when **any** of the following holds:

1. News blackout active: high-impact event for either of the pair's currencies within −10/+20 min (firm's window if stricter). Sources: economic-calendar feed with manual CSV fallback; events cached daily at 06:00 London and re-checked hourly.
2. A high-impact event is scheduled within the next 30 minutes (don't open into news).
3. Spread gate fails (decision time or send time).
4. Asian range invalid (too narrow/too wide) or session data incomplete (missing bars > 2 in the window).
5. ADR20 regime filter: ADR20 below its 25th percentile (dead market) or above its 90th percentile (crisis regime) of the trailing year.
6. Daily/weekly/consecutive-loss/consistency halts active (Section 7).
7. Bank holiday in UK or US; the daily-rollover window (16:45–17:15 New York) — no orders, and flat before it; Friday after 16:00 London — no new entries.
8. Monday gap filter: pair opened > 0.5 × ADR20 away from Friday's close → skip the pair for the day.
9. DST-transition anomaly flag: during the 2–3 weeks each March/October–November when US and UK clocks are out of sync, sessions are recomputed; if the computed window fails self-checks, the day is NO-TRADE rather than guessed.
10. Ops health: stale tick (> 10 s), clock skew (> 2 s vs NTP), reconnect in progress, or kill/pause flag set.
11. Symbol validation failed at boot (digits/contract size mismatch vs. instrument spec).
12. Position already open anywhere in the EU/GU risk unit.

---

## 7. Risk Model

Account: $50,000. 1R = `risk_per_trade` = $175 default (0.35%), bounds $150–$250.

| Layer | Firm rule | Internal rule (enforced) | Rationale |
|---|---|---|---|
| Per-trade risk | n/a | $175; auto-reduced to $125 the day after a 2-loss day, restored after a green day | Deterministic anti-tilt; never increases after losses (no martingale by construction) |
| Daily loss | $1,250 hard | No new entries at day P&L ≤ −$350 (−2R); flatten + halt at −$600 (realized + floating) | ≥ $650 buffer to firm limit covers slippage on the worst exit |
| Trailing drawdown | $2,500 below HWM | Trading halts when `equity − firm_floor < $800`; floor modeled **intraday-trailing on equity** unless the firm confirms EOD-balance trailing in writing | Always model the harsher variant |
| Weekly loss | n/a | −$1,200 → halt until Monday review | Regime-failure circuit breaker |
| Consecutive losses | n/a | 2 → day halted (configurable 3) | Matches account spec |
| Trades/day | n/a | 3 entries hard cap, duplicated in the execution adapter | Defense in depth |
| Consistency | e.g., no day > 30–50% of profits | No new entries once day P&L ≥ +$700 | Safe under a 30% rule on the $3,000 target with margin |
| Position count | varies | 1 concurrent position total | EU/GU correlation |
| Overnight/weekend | banned | Independent flattener at 19:30 London + hard checks at T−20/T−10/T−5 before firm cutoff | Section 19/20 |

**Cost reality:** with a 14-pip average stop on EUR/USD and ~0.4–0.6 pips round-trip cost (spread + commission + modeled slippage), costs ≈ 0.03–0.05R per trade at entry plus exit ≈ **5–8% of R per trade**. With the Section 3 exit structure (average win ≈ 1.1–1.3R), breakeven win rate is ≈ 45–48%; the hypothesis therefore needs ≈ 48–52% to be worth running. The backtest must confirm or kill this — do not assume it.

**Sizing formula:** `lots = floor_step( risk_usd / (stop_pips × pip_value_lot) , lot_step)`; pip value converted via current quotes for non-USD-quote pairs (kept generic even though v1 pairs are USD-quoted). Margin check: required margin at account leverage must leave ≥ 60% free margin or the trade is skipped.

---

## 8. Prop-Firm Compliance Engine (rules as code)

The firm's rules are data, not code paths scattered around the app. A versioned **firm profile** drives validators and runtime guards:

```yaml
firm_profile:
  name: "ExampleFunding 50k"        # verify every field against current ToS
  account_size: 50000
  profit_target: 3000
  trailing_dd: {amount: 2500, mode: intraday_equity}   # intraday_equity | eod_balance | static
  daily_loss: {amount: 1250, basis: equity_incl_floating, reset: "17:00 America/New_York"}
  consistency_pct: 40                # null if none
  min_trading_days: 5
  news_rule: {blackout_pre_min: 5, blackout_post_min: 5, profits_voided: true}
  overnight_allowed: false
  weekend_allowed: false
  max_lots: 10
  ea_policy: {allowed: true, requires_disclosure: true, copy_trading_banned: true}
  payout: {first_after_days: 14, split_pct: 80}
```

Engine behavior: (a) **startup validator** refuses to run if the profile is incomplete, internal buffers are not strictly inside firm limits, or `ea_policy.allowed` is false; (b) **runtime guards** recompute daily-loss distance, trailing-floor distance, trade counts, and consistency headroom on every tick batch and before every order; (c) **config governance** — any change writes a new `config_version` row with checksum and author, and live parameter changes are blocked mid-evaluation except risk *reductions*; (d) firm presets are unit-tested against synthetic equity paths (e.g., "HWM rises intraday then small loss must not breach modeled floor").

---

## 9. AI Validation Layer

**v1: deterministic only.** Every "AI-like" decision in v1 is a transparent rule (spread gate, regime filter, news filter). This is intentional: you cannot train a useful trade-quality model before you have logged real setups, and an untrained model in the loop is pure risk.

**Phase 2: veto-only gradient-boosted classifier (LightGBM/XGBoost).**

- Training data: logged setups (taken *and* skipped-but-simulated) with outcome label `R ≥ +0.5` vs not. Minimum 400 samples before first training; class-imbalance handled by weighting, not synthetic data.
- Features (market-only; **no account-state features**, to avoid leakage and tilt-coupling): range width as % of ADR20, sweep depth %, bars-to-reclaim, reclaim bar displacement vs ATR(14,M5), time-of-day bucket, day-of-week, spread state vs median, ADR20 percentile, distance of entry from session VWAP-proxy, prior-day direction.
- Validation: purged walk-forward CV (no shuffling time series), report precision/recall at candidate thresholds.
- Deployment rules: the model can only **veto** (never add trades, never size up). Veto-rate circuit breaker: if it vetoes > 40% of setups over a rolling 30, it is auto-disabled and flagged — a model that blocks nearly everything is broken or drifted. Monthly PSI drift check on each feature; PSI > 0.25 on any key feature → auto-disable. Every inference logs the full feature vector, score, threshold, and model version.
- Retraining pipeline: monthly cron extracts features → trains → validates against frozen test window → writes to `model_registry` → **manual approval** required to activate.

**LLMs:** offline only. Approved uses: nightly plain-English audit summary of all decisions and incidents (generated *from* the logs, never feeding back into trading), research literature summaries, code review, and post-mortem drafting. Banned uses: generating/validating live orders, parsing news into live signals, modifying config. Rationale: nondeterminism, latency, prompt-injection surface via news text, and un-auditability.

---

## 10. Platform Architecture

### 10.1 Principles

1. **One pure strategy core, three shells.** Strategy + risk logic is pure (no I/O, no clocks, no network); backtester, paper, and live differ only in adapters. This kills the classic "backtest code ≠ live code" failure.
2. **Boring beats distributed.** v1 is a single asyncio Python process plus an independent flattener process and a watchdog — not microservices. Fewer moving parts is a safety feature at this scale.
3. **Broker is the source of truth** for positions/orders; local DB is the source of truth for intents and audit. Reconciliation runs at startup and every loop.

### 10.2 Component map

```
+------------------------------- VPS (Windows, for MT5) -------------------------------+
|  MT5 Terminal <-> MT5Adapter(poll) --ticks/bars--> DataService --M5 closed--> Core    |
|                                                                                       |
|  EconCalendarService (fetch+cache+manual CSV fallback) ------------------------+      |
|                                                                                v      |
|  Core: StrategyEngine(SSR, pure) -> FilterPipeline -> RiskEngine -> ComplianceEngine  |
|        -> OrderManager(idempotent intents) -> ExecutionPort -> MT5Adapter             |
|                                                                                       |
|  StateStore (SQLite/Postgres)   AuditLog(append-only)   AlertService(Telegram/email)  |
|  FastAPI (dashboard + admin + kill)      Watchdog(heartbeats, NTP, stale-data)        |
+---------------------------------------------------------------------------------------+
|  Independent FlattenerProcess: separate PID, own MT5 login session, cron 19:10/19:20/ |
|  19:30 London + firm-cutoff T-20/T-10/T-5; verifies flat via broker query; can kill.  |
+---------------------------------------------------------------------------------------+
```

### 10.3 Ports (interfaces) and adapters

`MarketDataPort`, `ExecutionPort`, `ClockPort`, `CalendarPort`, `StorePort`. Implementations: `MT5Adapter` (v1), `BacktestAdapter` (v1), `CTraderAdapter` (Phase 3: cTrader Open API — OAuth2 + Protobuf/FIX, true event callbacks, Linux-friendly), `DXtradeAdapter` (Phase 3: REST + websocket; session-token auth; vendor-specific symbol metadata). Because all strategy/risk code talks to ports, adding a platform is adapter work only.

### 10.4 MT5 realities the design absorbs

The `MetaTrader5` Python package is Windows-only (VPS or Wine), **poll-based** (no order/tick callbacks — loop at 250–500 ms), and account-mode sensitive: netting vs hedging changes position semantics; filling modes (FOK/IOC/RETURN) and `trade_stops_level`/`freeze_level` must be read from `symbol_info` and respected when placing SL/TP. Orders carry a strategy **magic number** and an intent ID in the comment. Symbol mapping is config: `{canonical: EURUSD, broker: [EURUSD, EURUSD.pro, EURUSDm]}` with boot-time validation of digits/point/contract size against the instrument spec — a wrong-symbol trade is refused, not "fixed."

### 10.5 Order safety

Every order starts as an `order_intent` row with a UNIQUE idempotency key (`date|pair|direction|setup_id`) **before** anything is sent. Send path: pre-send checks (position count, spread, blackout, risk headroom, margin) → send with deviation cap → poll result → record fill or rejection. Max 3 retries with backoff on transient rejects, then abandon + alert. A rate limiter in the adapter itself (max 1 entry/5 min, max 6 order ops/day) is independent of strategy logic — a runaway loop hits the limiter, not the broker. On restart: fetch broker positions/orders, match by magic+comment to intents; unmatched broker positions trigger the **adopt-or-flatten policy** (default: flatten + alert).

---

## 11. Database Schema (SQLite v1 → Postgres later)

```sql
CREATE TABLE instrument(symbol TEXT PRIMARY KEY, broker_symbol TEXT, digits INT, point REAL,
  pip REAL, contract_size REAL, quote_ccy TEXT, min_lot REAL, lot_step REAL, max_lot REAL);
CREATE TABLE candle(symbol TEXT, tf TEXT, ts_utc TEXT, o REAL, h REAL, l REAL, c REAL,
  tick_vol INT, spread_pts INT, PRIMARY KEY(symbol, tf, ts_utc));
CREATE TABLE econ_event(id TEXT PRIMARY KEY, ts_utc TEXT, currency TEXT, impact TEXT,
  title TEXT, source TEXT, fetched_at TEXT);
CREATE TABLE setup(id TEXT PRIMARY KEY, ts_utc TEXT, symbol TEXT, direction TEXT,
  range_high REAL, range_low REAL, sweep_extreme REAL, reclaim_close REAL,
  features_json TEXT, status TEXT);            -- detected|vetoed|ordered|expired
CREATE TABLE decision(id TEXT PRIMARY KEY, setup_id TEXT, ts_utc TEXT, stage TEXT,
  passed INT, reason_code TEXT, details_json TEXT);
CREATE TABLE order_intent(id TEXT PRIMARY KEY, setup_id TEXT, ts_utc TEXT, symbol TEXT,
  side TEXT, lots REAL, entry REAL, sl REAL, tp REAL, status TEXT,
  broker_ticket TEXT, idempotency_key TEXT UNIQUE);
CREATE TABLE fill(id TEXT PRIMARY KEY, intent_id TEXT, ts_utc TEXT, price REAL, lots REAL,
  slippage_pips REAL, kind TEXT);               -- entry|tp1|tp2|sl|time|forced
CREATE TABLE position_snapshot(ts_utc TEXT, symbol TEXT, lots REAL, avg_price REAL,
  upl REAL, sl REAL, tp REAL);
CREATE TABLE equity_snapshot(ts_utc TEXT PRIMARY KEY, balance REAL, equity REAL,
  hwm REAL, firm_floor REAL, dist_floor REAL);
CREATE TABLE risk_day(d DATE PRIMARY KEY, realized REAL, fees REAL, trades INT,
  consec_losses INT, halted INT, halt_reason TEXT, consistency_headroom REAL);
CREATE TABLE config_version(id TEXT PRIMARY KEY, ts_utc TEXT, author TEXT,
  yaml TEXT, checksum TEXT, active INT);
CREATE TABLE model_registry(id TEXT PRIMARY KEY, trained_at TEXT, metrics_json TEXT,
  features_json TEXT, approved_by TEXT, active INT);
CREATE TABLE audit(seq INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT, actor TEXT,
  event TEXT, payload_json TEXT);               -- append-only; no UPDATE/DELETE grants
```

## 12. API Endpoints (FastAPI, localhost-bound, token auth)

| Method/Path | Purpose |
|---|---|
| GET /health | Heartbeats: tick age, bar age, NTP skew, broker connect, flattener alive |
| GET /state | Engine state machine, active config checksum, model version |
| GET /positions, /orders | Live broker-reconciled view |
| GET /pnl/today, /pnl/range | P&L, R-multiples, fees |
| GET /risk | Distances to internal/firm limits, halts active, consistency headroom |
| GET /setups?date, /decisions?setup_id | Full decision trail incl. veto reason codes |
| GET /calendar/next | Upcoming events + active blackouts |
| GET /logs?level&since | Structured log tail |
| POST /pause, /resume | Entry-gate control (positions keep managing) |
| POST /flat-all | Close everything now (also callable by flattener) |
| POST /kill | Kill switch: flatten, cancel, halt, require manual re-arm |
| POST /config (admin) | Propose config; activates only after validator pass; risk-reductions only while in evaluation |
| GET /reports/daily | Daily audit artifact (JSON + human summary) |

---

## 13. Backtesting Methodology

1. **Data.** Tick-grade bid/ask history for EUR/USD and GBP/USD (e.g., Dukascopy tick archives via a downloader module), 2018–present, plus the broker's own M1/M5 for cross-checking. Build M5 bars from **bid and ask separately** so spread is observable per bar. Caveat logged in the report: Dukascopy wicks ≠ your broker's wicks; before live, re-run signal detection on ≥3 months of the broker's own M1 to measure setup-match rate.
2. **Clock discipline.** Everything UTC; sessions derived via `Europe/London`. Unit tests pin known DST dates (including the US/UK mismatch weeks) and assert window boundaries.
3. **Fill model (conservative by default).** Decisions only on closed bars. Entry at next tick ≥ signal close + `slippage_in` (default 0.3 pip; stress 1.0). If SL and TP fall inside the same M5 bar and tick data is unavailable, **assume SL hit first**. Exits pay `slippage_out` (default 0.4 pip; 1.5 around news-adjacent minutes). Costs: variable spread from data + $7/lot round-turn commission. No swap (flat EOD).
4. **No-look-ahead enforcement.** The backtest adapter feeds the same `on_bar_closed` events the live adapter does; the strategy core cannot see the forming bar, the day's future events, or its own future fills.
5. **Metrics.** Expectancy in R, profit factor, win rate, max intraday-equity drawdown, time under water, daily P&L concentration (largest day / total — the consistency-rule check), trades/day distribution, and per-regime breakdowns (yearly + volatility-tercile).
6. **Evaluation-pass Monte Carlo.** Resample the backtest's trade-level R distribution (block bootstrap, 5-trade blocks) into 10,000 simulated evaluations of the actual race: +$3,000 target vs. $2,500 *trailing* floor vs. all internal halts. Output: pass probability per attempt, expected attempts, expected fee spend, distribution of days-to-pass. This number — not the equity curve — is the honest summary statistic for a prop context. Do not proceed live if pass probability per attempt is below a pre-committed threshold (suggested: 35–40%).
7. **Multiple-testing honesty.** Every parameter variant ever run is recorded in the DB. Report the count; expect the best variant's edge to shrink out of sample. Parameter changes are made on **plateaus** (±30% perturbation of each parameter must not flip the sign of expectancy), never on peaks.
8. **Sample-size gate.** No conclusion before ≥300 trades spanning ≥3 calendar years and at least one high-vol and one low-vol regime.

## 14. Walk-Forward Validation Plan

- Core SSR rules are **frozen** after the initial design; walk-forward re-fits only the regime filter thresholds and (Phase 2) the ML veto.
- Scheme: rolling 18-month train / 6-month test across 2018–present; additionally one fully untouched holdout (most recent 9 months) opened exactly once, at the end.
- Pass criteria (pre-committed): every test fold PF ≥ 1.05 after costs; pooled out-of-sample PF ≥ 1.15; no fold with max DD > $1,800 in account terms at $175 risk; daily-concentration metric compliant with a 30% consistency rule.
- Fail behavior: kill or formally revise the hypothesis (new spec version, full re-run). **No silent re-tuning.**

## 15. Paper-Trading Plan

- ≥ 6 weeks and ≥ 40 trades on the prop platform's demo (same feed family as the evaluation), full system end-to-end including flattener and alerts.
- Acceptance gates: (a) signal-match — replaying the same dates offline must reproduce ≥ 90% of live-detected setups (feed differences explain the rest; investigate every mismatch); (b) slippage audit — median entry slippage ≤ 0.5 pip EU; (c) spread-gate hit rate and blackout behavior verified against the calendar; (d) zero category-A ops incidents (duplicate order, missed flatten, unreconciled position).
- Ops drills run at least once each: kill-switch fire, mid-position process restart with reconcile, broker disconnect ≥ 5 min, forced-flatten failure escalation, DST week behavior.

## 16. Live Rollout Plan

1. Written confirmation of the firm's EA/automation policy on file. Smallest available evaluation first.
2. Weeks 1–2: EUR/USD only, risk $125/trade, max 2 entries/day. Escalate to spec defaults only after 10 trades with slippage and behavior within paper bounds.
3. Add GBP/USD in week 3+ if EU live matches paper. XAU/USD not before Phase 3 and a separate validation cycle.
4. Incident policy: any category-A incident → kill switch, post-mortem before re-arm. Two category-A incidents in a month → back to paper.
5. Funded phase: same system, risk unchanged (do **not** increase risk because it's "the firm's money" — trailing floors don't care); withdraw at the first payout window; treat each payout as the real P&L metric. Budget evaluation fees per quarter in advance and stop when the budget is spent — that is the bankroll rule that prevents the meta-level martingale of endlessly re-buying evals.

## 17. Dashboard Design

Single page, four zones: (1) **Status strip** — engine state, kill/pause/flat buttons (confirm dialogs), heartbeat lights (tick age, broker, NTP, flattener), active config checksum; (2) **Risk zone** — equity vs. HWM vs. firm floor vs. internal floor as one chart, distance-to-limit gauges, today's counters (trades, consec losses, consistency headroom); (3) **Trade zone** — open position with live R, today's setups/decisions table with reason codes, blackout timeline for the next 12 h; (4) **History zone** — R-multiple histogram, daily P&L bars, slippage tracker, model-veto stats (Phase 2). Plotly/Dash or plain FastAPI + HTMX; localhost + SSH tunnel; no public exposure.

## 18. Logging & Audit Design

- Structured JSON logs (structlog) with run ID, config checksum, and intent IDs on every line; INFO for lifecycle, WARN for gate failures, ERROR for ops incidents.
- **Every non-trade has a reason code** in `decision` — the absence of a trade is evidence, not silence.
- Append-only `audit` table (no UPDATE/DELETE) capturing config changes, manual actions, kill events, model activations.
- Daily artifact at 20:00 London: JSON + human-readable summary (optionally LLM-phrased *from* the logs) covering setups, decisions, fills, slippage, risk distances, incidents. Retention: logs 1 year, DB indefinitely. This is the file you show a firm if a trade is ever disputed.

---

## 19. Safety / Failure-Mode Table (Pass 6)

| # | Failure mode | Detection | Mitigation / automated action |
|---|---|---|---|
| 1 | Stale market data | Tick age > 10 s in session | Pause entries; > 60 s with open position → alert; > 5 min → flatten (configurable) |
| 2 | Broker/terminal disconnect | API errors, heartbeat | Reconnect with backoff; reconcile on resume; if open position + blackout approaching → flatten |
| 3 | Wrong symbol variant | Boot validation digits/point/contract size vs spec | Refuse to trade symbol; alert |
| 4 | Duplicate order | UNIQUE idempotency key; pre-send open-position check | Second send impossible at DB layer; adapter rate-limiter as backstop |
| 5 | Partial fill | Fill qty < intent qty | Recompute SL/TP for actual qty; orphan-volume reconciler closes remainder mismatches |
| 6 | Slippage spike | Fill vs intended > max_deviation | Order rejected by deviation cap; if filled worse anyway, log incident, halt entries for 60 min |
| 7 | Spread widening | Gate at decision **and** send | Skip with reason code; never chase |
| 8 | News shock with open position | Calendar + blackout engine | Entries blocked around events; configurable pre-news flatten if open profit < +0.5R; stops never widened |
| 9 | AI hallucination | n/a in v1 (no LLM in loop) | ML veto-only + schema-validated numeric output + veto-rate breaker; LLM offline only |
| 10 | Bad timezone / DST | NTP check; pinned-date unit tests; window self-checks | Skew > 2 s → halt entries; failed window self-check → NO-TRADE day |
| 11 | Daily rollover spreads | Clock-based window | No orders 16:45–17:15 NY; flat well before |
| 12 | Contract rollover | n/a for spot FX CFDs | Swap irrelevant (flat EOD); XAU futures-style symbols excluded in v1 |
| 13 | Prop rule misconfiguration | Profile validator; buffer assertions; preset unit tests | Engine refuses to start on invalid/missing fields; manual checklist at onboarding |
| 14 | Runaway trading loop | Adapter rate limiter; DB trade counter independent of strategy | Hard stop at 6 order ops/day; kill switch on breach |
| 15 | Position not flat by session end | Independent flattener process + T−20/T−10/T−5 checks | Retries, then market-close at any spread, then kill + phone alert |
| 16 | Data feed delay (bars late) | Bar-age watchdog vs expected close time | Pause entries; resume after 3 healthy bars |
| 17 | Model drift | Monthly PSI per feature; live-vs-train score dist | PSI > 0.25 → veto model auto-disabled, strategy continues deterministic |
| 18 | Overfitting | Variant registry; plateau rule; frozen holdout | Governance: changes require full re-validation; no live tuning mid-eval |
| 19 | VPS reboot / crash | Service auto-start; watchdog PID file | Restart → reconcile → adopt-or-flatten policy; alert |
| 20 | MT5 terminal auto-update breaks API | Boot canary (login, symbol query, test ping) | Pin terminal version where possible; fail closed |
| 21 | Margin/stop-out risk | Pre-send free-margin check | Skip trade if free margin < 60% post-trade |
| 22 | Weekend gap | Calendar rule | Flat by Friday cutoff; Monday gap filter |
| 23 | Firm changes ToS | Weekly manual review task in runbook | Profile re-versioned; system halted until updated |
| 24 | Reject/requote storm | Retry counter | 3 retries → abandon intent, alert, pause 30 min |
| 25 | Copy-trade similarity flag (shared bot) | n/a (policy risk) | Own parameterization, own schedule jitter (±randomized entry-window minutes within spec), written EA disclosure to firm |

---

## 20. Development Roadmap

| Milestone | Scope | Exit gate |
|---|---|---|
| M0 (wk 1) | Repo, config schema + validator, UTC/session library with DST tests, instrument spec | All timezone tests green |
| M1 (wk 2–3) | Data layer: Dukascopy downloader, broker M1 fetch, M5 builder (bid/ask), calendar service + CSV fallback | 2018–now data validated; calendar diff ≤ known gaps |
| M2 (wk 3–4) | Pure SSR core + filter pipeline + unit tests on synthetic fixtures (clean long, deep-sweep skip, no-reclaim, spread skip, DST week) | 100% of fixture cases pass |
| M3 (wk 4–5) | Risk + compliance engines + property tests on synthetic equity paths | Trailing-floor and consistency guards proven on adversarial paths |
| M4 (wk 5–6) | Backtester + cost model + reports + Monte Carlo pass-probability | Full campaign run; **go/kill decision on strategy** |
| M5 (wk 6–8) | MT5 adapter, order manager, flattener, watchdog, alerts; paper mode | Ops drills passed; paper gates (Sec. 15) running |
| M6 (wk 8–9) | Dashboard, daily reports, runbook; hardening | 2 clean paper weeks, zero cat-A incidents |
| Gate | Review everything against Sections 13–16 | Live only if all gates green |


---

## Appendix A — Strategy config (v1 defaults)

```yaml
strategy: ssr_v1
pairs:
  EURUSD: {range_min_pips: 10, sweep_min_pips: 2, stop_min: 7, stop_max: 22,
           spread_abs_cap: 1.2, max_deviation: 1.5}
  GBPUSD: {range_min_pips: 13, sweep_min_pips: 3, stop_min: 9, stop_max: 28,
           spread_abs_cap: 1.8, max_deviation: 2.0}
shared: {range_max_adr_mult: 0.60, sweep_max_range_mult: 0.60, reclaim_bars: 6,
         reclaim_quality_pct: 0.40, tp1_r: 1.0, tp1_close: 0.5, tp2_r_cap: 2.2,
         time_stop_min: 90, forced_exit_london: "19:30",
         entry_window_london: ["07:05","10:30"], asian_window_london: ["00:00","06:55"],
         spread_median_mult: 1.8, adr_pctile_skip: [25, 90]}
risk: {per_trade_usd: 175, cooldown_usd: 125, day_soft_stop: -350, day_hard_stop: -600,
       week_stop: -1200, consec_loss_halt: 2, max_entries_day: 3, max_concurrent: 1,
       consistency_day_cap: 700, floor_buffer: 800}
news: {pre_min: 10, post_min: 20, lookahead_block_min: 30,
       currencies: {EURUSD: [EUR, USD], GBPUSD: [GBP, USD]}}
```

## Appendix B — Final notes

This system optimizes for **survival and cheap falsification**. If M4's campaign and Monte Carlo say the edge isn't there after costs, the correct output of this entire project is *not trading* — that result will have cost a few weeks and zero drawdown, which is the best bad outcome available in this industry. Trading CFDs involves substantial risk of loss; nothing here is financial or legal advice, and the firm's current written rules supersede every default in this document.
