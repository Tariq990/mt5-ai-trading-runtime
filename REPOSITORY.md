You are the lead engineer and release owner for GPTTRADDER.

Work autonomously on the CURRENT LOCAL WORKING PROJECT that is already operational on Windows.

Do NOT rebuild the project from an old GitHub version.

The local working copy is newer than GitHub and contains important fixes and verified operational state.

Your job is to upgrade the current working system, test everything, create durable agent documentation, and publish the exact latest tested version to GitHub.

# REPOSITORY

GitHub:

https://github.com/Tariq990/GPTTRADDER

Target repository:

Tariq990/GPTTRADDER

The currently working local project is GPTTRADDER 0.3.0 or newer.

The local working copy is the source of truth until you successfully publish the new release.

Do NOT overwrite it with the older GitHub `main`.

Before changing anything:

1. Identify the exact local project directory.
2. Run:
   - `git status`
   - `git branch --show-current`
   - `git log --oneline -10`
3. Inspect all existing source files, `.env.example`, tests and runtime configuration.
4. Preserve all existing working fixes.
5. Never overwrite the local project with an older remote checkout.
6. Never commit `.env`, credentials, cookies, Playwright state, broker credentials, Telegram secrets, or other local secrets.

# CURRENT VERIFIED STATE

The following has already been achieved and MUST NOT regress:

- 63 tests passing (baseline v0.3.0).
- 100 tests passing after the v0.4.0 upgrade (full suite: `python -m pytest -q`).
- 121 Python tests + 15 node bridge tests passing after the v0.4.0 hardening pass (clock normalization + executable status).
- 142 Python tests + 18 node bridge tests passing after the Fix 4 (v0.4.0) release-blocking pass — auto-measured broker clock, deterministic bridge messages, live MT5 revalidation all green.
- 154 Python tests + 18 node bridge tests passing after the Fix 5 (review channel) pass; the review channel was verified live end-to-end against a real ChatGPT conversation.
- `python -m compileall -q src` passes.
- End-to-end smoke cycle passes with the simulated broker (`once --mock` -> SKIPPED/WAIT).
- Preflight passes with the simulated broker: all five canonical symbols resolved
  (`symbol:BTC: broker_symbol=BTCUSD` ...), `contract:<symbol>` metadata checks green,
  `market:<symbol>` quote checks green.
- MT5 Demo preflight passes.
- MT5 Demo account tested successfully.
- Demo account ID observed during testing: `52996387`.
- Real Demo MT5 write/cancel verification succeeded.
- A pending order was created and cancelled safely.
- Dashboard works on port 8765.
- Watchdog monitors services and restarts failed runtime components.
- Daily reports work.
- Windows autostart / Task Scheduler works.
- `GPTTRADDER-Watchdog` task exists or has been tested.
- `GPTTRADDER-MT5-Terminal` task exists or has been tested.
- Real ChatGPT/Playwright bridge was tested.
- The review channel was tested live: separate ChatGPT conversation bound to
  `gpttradder-review`, TEST event delivered, ChatGPT reply persisted to
  `review_events` (SENT), duplicate suppression verified, wrong session key
  rejected, trading session untouched.
- Three consecutive real ChatGPT decision cycles completed cleanly.
- ChatGPT returned WAIT decisions normally.
- No freezing remained after timeout correction.
- No unintended broker execution occurred.

Two runtime defects were already found and fixed:

## Fix 1 — ChatGPT timeout hierarchy

The bridge timeout had been longer than the outer GPTTRADDER decision timeout.

That caused cycles to hang.

The working correction is approximately:

`GPTTRADDER_CHATGPT_TIMEOUT_SECONDS=80`

while the outer decision timeout is greater than that.

Preserve the invariant:

INTERNAL CHATGPT/BRIDGE TIMEOUT
<
OUTER GPTTRADDER DECISION TIMEOUT

Never allow the inner operation timeout to exceed the outer caller timeout.

Add an automated validation/test for this configuration invariant if one does not already exist.

## Fix 2 — Windows PowerShell Unicode output

CLI Unicode warning/output problems on Windows PowerShell were fixed.

Preserve this fix and test CLI output on Windows-safe encoding assumptions where practical.

## Fix 3 — Broker server clock normalization + weekend/stale symbol status

Observed on the live MT5 demo: BTC/ETH quotes arrived ~3h ahead of UTC (the
broker server clock runs EEST), so they looked perpetually fresh; meanwhile the
weekend-re-emitted XAU/EURUSD/GBPUSD quotes carried Friday timestamps but still
reported TRADABLE. Both symptoms had one root cause family: broker timestamps
were interpreted as UTC, and symbol availability had no session/clock model.

Corrections (all fail closed):

- `timeutil.py` — `normalize_broker_epoch` shifts every broker timestamp (ticks,
  candles, positions, orders, deals) from the configurable server offset
  (`GPTTRADDER_BROKER_SERVER_UTC_OFFSET_HOURS`, default 2.0 = EET) to UTC.
  `quote_is_fresh` treats quotes stamped in the future as NEVER fresh.
- `models.py` — `SymbolStatus` decomposed into `broker_trade_mode`,
  `quote_fresh` (with `quote_age_seconds`), `market_session_open` and the
  aggregated `executable_now`; new status `TIME_INVALID` for future-stamped
  quotes (excluded from packets, like NO_QUOTE).
- `collector.py` — weekend/calendar plus freshness verdicts per symbol
  (crypto 24/7; FX/metal/unclassified closed Friday 21:00 UTC -> Sunday
  21:00 UTC, configurable). Valid-but-stale symbols stay visible with
  `executable_now: false`, never executable.
- `safety.py` — new `SYMBOL_NOT_EXECUTABLE` gate: LONG/SHORT entries require
  `executable_now`; protective management/closes stay available; freshness
  checks also reject future-stamped quotes (`STALE_SYMBOL_QUOTE`,
  `STALE_PACKET`).
- `event_monitor.py` — skips future-skewed quotes and closed sessions.
- `preflight.py` — reports quote age and session state per symbol and fails a
  market check on future-stamped quotes (offset misconfiguration).
- `mt5_demo.py` — `history_deals_get` windows are converted back to server time.
- Prompt (both hash-synced copies) documents the decomposed flags.

Regression coverage: `tests/test_time_status.py` (14 tests) — unit tests for
the clock/calendar helpers plus collector/safety/MT5-adapter integration.

## Fix 4 — Broker clock auto-measurement + deterministic ChatGPT bridge messages

Two release-blocking issues found during final verification against the live
MT5 demo:

1. **Stale hardcoded clock assumption.** Fix 3 kept `GPTTRADDER_BROKER_SERVER_UTC_OFFSET_HOURS`
   with a 2.0h default. Live measurement (Raw Trading demo account 52996387,
   build 6104) proved the server runs **UTC+3 (EEST)** — EURUSD tick `time_msc`
   was +2.995h vs UTC. Any hardcoded value (winter EET) silently shifts fresh
   quotes 1h into the future.

2. **Nondeterministic ChatGPT message between retries.** `digest.mjs` computed
   `quote_age_s` from `Date.now()` at render time. Every retry rebuilt a
   DIFFERENT message for the same `client_message_id`, which the browser MCP's
   dedup correctly rejected with `client_message_id ... is already associated
   with different message text` → infinite 502 storms.

Corrections (all fail closed):

- `timeutil.py` — `measure_broker_utc_offset_hours()` probes fresh ticks
  (`tick_msc` encodes the server wall clock), keeps only near-fresh samples,
  quantizes to the 15-minute grid, rejects implausible results; `broker_utc_offset_plausible`
  bounds the band (-14..+14h).
- `mt5_demo.py` — offset is AUTO-DETECTED at `connect()` and re-measured every
  `resolve_symbols()` (DST-safe: EET↔EEST shifts are picked up automatically).
  An explicit `GPTTRADDER_BROKER_SERVER_UTC_OFFSET_HOURS` override
  (`server_utc_offset_hours`) is validated against the live measurement:
  |override − measured| ≥ 0.5h → RuntimeError (fail closed).
  Unmeasurable (full weekend closure) + no override → fail closed at connect.
  `preflight.py` reports a `broker_clock` check with the attribution
  ("auto-measured 3h", "consistent with measured 3h", ...).
- `config.py` — `broker_server_utc_offset_hours` default is now `None` (auto);
  an explicit value is an override, not the default.
- `digest.mjs` — message is fully deterministic: quote ages are computed against
  the FIXED packet reference instant (`packet_created_at`), never `Date.now()`.
  Repeated renders are byte-identical.
- `server.mjs` — renders the message once per cycle, hashes it (SHA-256), and
  rejects a same-cycle re-render whose fingerprint differs (fail loudly BEFORE
  dispatch); forwards `message_sha256` with the send.
- `http_bridge.py` — the complete POST payload is FROZEN once per cycle
  (canonical sort + compact JSON), one SHA-256 fingerprint, one
  `client_message_id` (`gpttradder:<cycle_id>`); every retry reuses the
  byte-identical body. Re-freezing the same cycle with different content is
  impossible (RuntimeError). Deterministic repeated errors still fail fast.
- `db.py` — new `bridge_sends` table persists every attempt's
  `cycle_id / client_message_id / message_sha256 / attempt` (audit trail;
  factory wires it via `on_send`).
- `json.mjs` — markdown-escape unescaping extended to `[ ] { } | + ~ ` `
  (ChatGPT web renders arrays/objects with `\[` `\]`; the live reply used
  `acceptable\_price\_range":\[65170,65190\]`).
- `models.py` — lenient-but-safe ChatGPT shorthand parsing: scalar
  `take_profit` coerces to one full-size target; `{min,max}` coercion for
  `acceptable_price_range`; ambiguous multi-scalar TP fails closed
  ("close_percent total cannot exceed 100").
- Prompt (both hash-synced copies) now documents the exact
  `take_profit` JSON target form (`[{"price":..., "close_percent":...}]`) and
  that bare numbers are schema-invalid.

Live revalidation (Raw Trading MT5 Demo, account 52996387, 2026-08-10):

- `broker_clock: 3h (auto-measured 3h)` — measured EEST offset confirmed.
- Preflight: ALL PASS — all five canonical symbols resolve (BTCUSD, ETHUSD,
  XAUUSD, EURUSD, GBPUSD); contracts FULL; BTC quote_age 0.4s, EURUSD 13.3s,
  GBPUSD 7.2s (normalized to UTC, fresh).
- `verify-mt5-write`: pending order placed (ticket 1849030789) and cancelled —
  PASS.
- Real ChatGPT cycle: LONG BTC → STOP @65170 size 0.87, SL 65055,
  TP 65245@30% / 65350@70% → live pending order placed on the demo (ticket
  1849033187, retcode 10009 PLACED), then removed for a clean handover.
- bridge_sends audit rows persisted (1 send, frozen sha stable).

Regression coverage: `tests/test_time_status.py` (8 new: +2/+3 auto-detect,
DST transition, contradicting override fails closed, unmeasurable fails closed,
stale-quote discard) + `tests/test_bridge_idempotency.py` (6 new: byte-identical
retries A, persisted attempts A2, same-cycle divergence B, new-cycle C, cached
reuse D, transport-retry single execution E) + `tests/test_models.py` (5 new:
TP coercion/rejection, price-range coercion) + node `digest.test.mjs` (2 new:
determinism, fixed reference) + `json.test.mjs` (1 new: escaped arrays).

## Fix 5 — Isolated ChatGPT review channel (v0.5.0)

A post-trade/system REVIEW channel that uses a SEPARATE ChatGPT conversation
(`gpttradder-review` session key), strictly advisory: it analyses closed
trades, rejected entries, system errors and periodic/daily summaries, and its
replies are stored for the audit record. It never executes trades, never
modifies parameters, and its failure NEVER blocks or stalls the trading cycle.

**Session separation (hard invariant):**
- `bridge/server.mjs` — `POST /review` accepts ONLY the `gpttradder-review`
  session key; any other key (including the trading `gpttradder` key) is
  rejected with 502 BEFORE dispatch. The trading session is never used for
  review messages and is never re-bound by the review channel
  (`ensureReviewSession` binds only `gpttradder-review`; startup is
  best-effort so a missing review conversation cannot break trading).
- Review messages are frozen per event: one canonical serialization + SHA-256,
  mirroring the Fix 4 decision-bridge idempotency contract.
- `watchdog.py` passes the review env vars (`GPTTRADDER_CHATGPT_REVIEW_*`) to
  the auto-started bridge; the trading env vars are untouched.

**Non-blocking by contract:**
- `orchestrator.py` fires every review event as a BACKGROUND task
  (`ReviewService.fire`); `send_*`/`scan_*`/`process_pending` swallow all
  failures internally. A dead review bridge or a 502 can never stall or fail a
  trading cycle. Verified live: a review send against a stale bridge recorded
  `FAILED` in the DB while the cycle continued normally.

**Event types:**
- `TRADE_CLOSED` — durable position-snapshot diff (`review:positions_snapshot`
  in DB state, survives restarts); enriched with the broker statement deal
  (ticket/price/profit/comment) when available; R-multiple computed.
- `TRADE_REJECTED` — every LONG/SHORT that fails the safety gate or broker
  execution, with quote, contract metadata, symbol status and the monetary
  risk breakdown attached.
- `SYSTEM_ERROR` — collection failures, decision-provider failures,
  cycle-blocked (STALE_PACKET/TIME_INVALID), broker exceptions,
  idempotency-protection triggers; noisy repeats are aggregated per window
  (first occurrence + every Nth), never more than one send per window bucket.
- `PERIODIC_REVIEW` — configurable cadence (default 6h) with cycle/decision/
  execution/symbol counts, equity/drawdown, open positions, top rejections.
- `DAILY_REVIEW` — once per local risk-timezone day after
  `review_daily_hour:minute`, with the day summary plus the last closed trades
  and rejections; deep-review advisory prompt.
- `TEST` — `gpttradder review-test` CLI command (end-to-end channel check;
  no broker connection, no trading side effects).

**Idempotency:** `db.claim_review_event` (INSERT with unique `event_id`,
`gpttradder-review:` prefix enforced) is the durable at-most-once boundary —
the same event id can never be delivered twice, even across process restarts
(verified live and by restart tests).

**Persistence:** `review_events` table (event_id, event_type, status
CLAIMED/SENT/FAILED, payload_json, response_json, attempts, timestamps);
`get_review_payloads` feeds the daily review; dashboard/API-friendly
`get_review_events`.

**Config (`.env.example`):** `GPTTRADDER_REVIEW_ENABLED`,
`GPTTRADDER_CHATGPT_REVIEW_SESSION_KEY` (default `gpttradder-review`),
`GPTTRADDER_CHATGPT_REVIEW_CONVERSATION_URL` (must differ from the trading
conversation), `GPTTRADDER_CHATGPT_REVIEW_TIMEOUT_SECONDS`,
`GPTTRADDER_REVIEW_BRIDGE_URL` (default `http://127.0.0.1:8787/review`),
`GPTTRADDER_REVIEW_RETRY_DELAYS`, `GPTTRADDER_REVIEW_PERIODIC_HOURS`,
`GPTTRADDER_REVIEW_DAILY_HOUR/MINUTE`, `GPTTRADDER_REVIEW_ERROR_AGGREGATION_WINDOW_SECONDS`,
`GPTTRADDER_REVIEW_ERROR_AGGREGATE_EVERY`. The `GPTTRADDER_CHATGPT_REVIEW_*`
env names are declared as pydantic validation aliases on the `review_*`
Settings fields (`populate_by_name=True` keeps Python-side construction intact).

**Live verification (2026-08-10, real ChatGPT web):**
- Created a NEW ChatGPT conversation for the review channel (conversation URL
  kept out of the repo; see `.env`) via the
  browser MCP `chatgpt_new_session` + first-message capture; the trading
  conversation was never touched and its binding was verified unchanged via `/health`.
- `gpttradder review-test` → HTTP 200, `send_confirmed: true`, ChatGPT replied
  in the review conversation: "OK — I expect trade-review events such as
  signals, entries/exits, risk checks, strategy decisions, execution outcomes,
  anomalies, and runtime/contract tests."
- Response persisted: `review_events` row `status=SENT` with the reply in
  `response_json`; a pre-fix send attempt against a stale bridge persisted as
  `FAILED` (audit trail, no crash).
- Same event id re-dispatched → suppressed (duplicate), exactly one SENT row.
- `POST /review` with the trading session key → 502
  "review messages must use session_key 'gpttradder-review'".

**Regression coverage:** `tests/test_review.py` (11 new: disabled-channel
short-circuit, frozen message + once-only delivery, error aggregation first/Nth
and fresh-window, closed-trade scanner + durable snapshot, periodic bucket
once-per-window, daily once-per-local-day, bridge failure recorded without
raising, process_pending never raises, claim survives restart, no DB pollution
when disabled) + `tests/test_orchestrator.py` (1 new: rejected entry fires a
TRADE_REJECTED review event with context).

## Fix 6 — Explicit bridge dispatch states + reconciliation (v0.5.1)

**Observed failure (2026-08-10, live):** `DECISION_PROVIDER_FAILED` twice
within ~8 minutes, each with `HTTP 502 {"error":"ChatGPT bridge did not
confirm message dispatch"}`, and at least one cycle completing with 0
decisions.

**Root cause (proven from the MCP durable store):** all three affected cycles
have MCP turn status `failed_before_send` / `error_code=unexpected_error` —
a Playwright `locator.fill` actionability timeout on the `#prompt-textarea`
composer (`context.setDefaultTimeout(15_000)`). The durable turn rows carry
NO dispatch evidence (`send_dispatch_intent_at`/`send_dispatched_at` are
NULL), so the message was **never dispatched** in every observed failure. The
failure mode is therefore FAILED_BEFORE_DISPATCH (safe to retry), NOT
lost-acknowledgement. The bridge however reported every non-confirmation as
the same generic error, and the runtime's "identical body twice → fail fast"
heuristic abandoned the cycle after 2 attempts despite the failure being
pre-dispatch and retryable (evidence: cycle `9bea802c` succeeded on its
second attempt minutes later).

**Fail-closed verified for both failed cycles:** status `DECISION_FAILED`,
zero execution rows, zero decisions stored, no orphan pending orders, no
reused decision response — the at-most-once and execution boundaries held.

**Fix — explicit dispatch states (canonical vocabulary):**
`NOT_DISPATCHED`-style outcomes are now classified by the bridge into
`FAILED_BEFORE_DISPATCH` (durable proof nothing reached the browser; safe to
re-send the same frozen payload), `DISPATCHED_UNCONFIRMED` (durable dispatch
evidence exists; NEVER send again — bounded reconciliation only),
`DISPATCHED_CONFIRMED` (backend confirmed; reply pending),
`RESPONSE_RECEIVED` (reply proven) and `NOT_FOUND` (cannot prove either way;
fail closed). `bridge/dispatch-state.mjs` is the pure, unit-tested classifier;
`bridge/server.mjs` returns it in every non-200 body together with
`retryable`, `status`, `error_code`, `cycle_id`, `client_message_id`,
`message_sha256`, `found_in_conversation` and `response_found`.

**Reconciliation behavior:** the runtime never re-dispatches an unconfirmed
turn (the MCP itself refuses: same `client_message_id` returns the stored
turn). It re-POSTs the byte-identical frozen payload on
`GPTTRADDER_DECISION_RECONCILE_INTERVAL_SECONDS` (default 20s) — a cached
read that returns the reply the moment the turn completes — within a bounded
budget (`decision_timeout_seconds` + one interval). If the reply cannot be
proven, the cycle fails closed with a full audit. `FAILED_BEFORE_DISPATCH`
failures (the observed mode) retry through the full retry-delay list instead
of failing fast, which fixes the actual outage without ever risking a
duplicate ChatGPT turn (frozen payload + MCP write-once evidence both
guarantee at-most-once dispatch).

**Audit:** `bridge_sends` gains `dispatch_state`, `error_code`, `outcome`,
`response_found` (idempotent ALTER migration for existing DBs); every failed
decision-provider review event now reports `cycle_id`, `client_message_id`,
`message_sha256`, `dispatch_state`, `send_attempt`,
`found_in_conversation`, `response_found` and `reconcile_outcome` — no
secrets, no browser state. Error codes are state-aware
(`BRIDGE_FAILED_BEFORE_DISPATCH`, `BRIDGE_DISPATCH_UNCONFIRMED`,
`BRIDGE_RESPONSE_INVALID`, `BRIDGE_STATE_UNKNOWN`).

**Regression coverage:** `bridge/dispatch-state.test.mjs` (9) +
`tests/test_http_bridge.py` (11, incl. unconfirmed-then-response,
bridge-restart-during-reconciliation, byte-identical frozen payloads across
attempts, bounded fail-closed, legacy-body handling) + updated
`tests/test_bridge_idempotency.py` + `tests/test_orchestrator.py` (1:
provider failure is fail-closed with full audit in the SYSTEM_ERROR review
event) + `tests/test_review.py` (2: audit carried in message + payload, and
backward compatibility). Full suite: 163 Python + 27 Node tests, compileall,
`node --check` on both `server.mjs` copies.

# CORE ARCHITECTURAL CONTRACT

GPTTRADDER is DEMO ONLY.

ChatGPT is the sole trading decision engine.

DeepSeek, local automation, the broker adapter, and all other agents are NOT allowed to make trading decisions.

Code may:

- collect data;
- compute deterministic metrics;
- enforce safety;
- validate schema;
- validate broker constraints;
- reject invalid decisions;
- calculate monetary risk;
- execute exact ChatGPT-approved actions;
- manage transport;
- monitor services;
- log events;
- prevent duplicate execution.

Code must NEVER:

- invent LONG/SHORT;
- alter direction;
- invent an SL;
- invent a TP;
- silently change trade economic intent;
- choose a different trade because ChatGPT failed;
- let DeepSeek repair or infer a trade decision.

If a ChatGPT decision cannot be safely executed:

REJECT IT
→ return exact reason and fresh state
→ request a new ChatGPT decision.

Never silently mutate it.

# MAIN UPGRADE GOAL

Upgrade GPTTRADDER from monitoring:

- BTC
- XAU / Gold

to monitoring and trading these FIVE instrument classes:

1. BTC
2. ETH
3. XAU / Gold
4. EURUSD
5. GBPUSD

All five should have equal monitoring priority.

ChatGPT dynamically chooses the best opportunities across them.

The objective remains roughly:

3–4 logical trades PER DAY TOTAL across the entire portfolio when market conditions reasonably permit.

NOT 3–4 trades per instrument.

Do not force trades merely to meet a quota.

# PHASE 1 — BROKER SYMBOL AUTO-DISCOVERY

Do NOT blindly assume the broker uses these literal names:

BTCUSD
ETHUSD
XAUUSD
EURUSD
GBPUSD

Brokers may use:

BTCUSDm
ETHUSDm
XAUUSDm

or suffixes such as:

.a
.pro
.raw
-
_

etc.

Use the MetaTrader5 Python API to inspect:

`mt5.symbols_get()`

Build a robust symbol discovery/resolution layer.

Canonical application symbols should be:

BTC
ETH
XAU
EURUSD
GBPUSD

Map each canonical symbol to the actual broker symbol.

Example:

```json
{
  "BTC": "BTCUSDm",
  "ETH": "ETHUSDm",
  "XAU": "XAUUSDm",
  "EURUSD": "EURUSD",
  "GBPUSD": "GBPUSD"
}
```

Do not resolve ambiguous symbols silently.

If multiple plausible broker symbols exist:

- rank exact/common candidates;
- inspect trade mode and symbol properties;
- report ambiguity;
- fail closed if necessary.

Persist/configure the resolved mapping cleanly.

Keep compatibility with explicit configuration overrides.

Update `.env.example` accordingly.

List fields in `.env` must use Pydantic-compatible JSON syntax.

For example:

```env
GPTTRADDER_SYMBOLS=["BTCUSD","ETHUSD","XAUUSD","EURUSD","GBPUSD"]
GPTTRADDER_DECISION_RETRY_DELAYS=[0,15,30,60]
```

But broker symbol auto-discovery should make manual suffix editing unnecessary when possible.

Add tests for:

- exact symbol names;
- suffix names;
- ambiguous candidates;
- missing symbol;
- disabled/non-tradable symbol.

# PHASE 2 — COMPLETE MT5 CONTRACT METADATA

ChatGPT currently refuses LONG/SHORT because the MarketPacket lacks enough broker contract metadata to verify sizing/risk safely.

THIS MUST BE FIXED.

For every monitored broker symbol collect relevant values from:

`mt5.symbol_info(symbol)`

At minimum include:

- broker_symbol
- canonical_symbol
- description
- path

PRICE / PRECISION:

- bid
- ask
- spread
- point
- digits
- trade_tick_size

TICK VALUE:

- trade_tick_value
- trade_tick_value_profit
- trade_tick_value_loss

CONTRACT:

- trade_contract_size

VOLUME:

- volume_min
- volume_max
- volume_step
- volume_limit

BROKER CONSTRAINTS:

- trade_stops_level
- trade_freeze_level
- trade_mode
- trade_calc_mode
- filling_mode
- order_mode
- expiration_mode

CURRENCY:

- currency_base
- currency_profit
- currency_margin

SWAP / CARRY:

- swap_mode
- swap_long
- swap_short
- swap_rollover3days

SESSION / TRADING AVAILABILITY where reasonably available.

Include any additional MT5 symbol metadata that materially improves correct sizing or execution.

Do not dump useless undocumented fields blindly.

Define a typed Pydantic model, for example:

`SymbolContractSpec`

or an equivalently clear name.

MarketPacket should expose contract information for every monitored symbol.

Example conceptual shape:

```json
{
  "symbols": {
    "BTC": {
      "broker_symbol": "BTCUSDm",
      "quote": {},
      "contract": {},
      "candles": {}
    }
  }
}
```

Use a clean schema consistent with the current architecture rather than forcing this exact shape if the existing models suggest a better design.

# PHASE 3 — CORRECT MONETARY RISK CALCULATION

Use broker-native calculations wherever available.

Prefer MetaTrader5 functions such as:

- `order_calc_profit`
- `order_calc_margin`

rather than manually assuming pip/tick math across all asset classes.

Verify monetary stop-loss risk for:

- BTC
- ETH
- XAU
- EURUSD
- GBPUSD

The same formula cannot be blindly assumed for crypto CFDs, metals and FX.

For a proposed ChatGPT trade calculate and expose:

- estimated SL loss in account currency;
- risk % of current equity;
- estimated required margin;
- normalized broker volume;
- actual volume after permissible technical normalization.

Technical normalization may handle only broker mechanics such as:

- volume step;
- allowed precision;
- tick size.

If normalization materially changes ChatGPT's economic risk intent:

REJECT
→ tell ChatGPT
→ request a new decision.

Do not silently increase risk to satisfy minimum lot.

Add tests covering all five instrument types.

# PHASE 4 — MARKET PACKET EXPANSION

Every full decision cycle should send all five instruments:

BTC
ETH
XAU
EURUSD
GBPUSD

Include for each:

- Bid
- Ask
- Spread
- Quote timestamp
- OHLCV
- 1m
- 5m
- 15m
- 30m
- 1h
- 4h
- 1d
- current trade availability
- SymbolContractSpec
- relevant DOM/order book when broker exposes it
- deterministic volatility metrics already supported
- broker state necessary for correct execution

Also include portfolio/account state:

- balance
- equity
- free margin
- peak equity
- daily baseline
- daily PnL
- trailing drawdown
- account currency
- account mode
- hedging_allowed
- open positions
- pending orders
- recent deals
- open risk
- remaining daily-loss headroom
- remaining trailing-drawdown headroom

Include recent GPTTRADDER decision history.

The packet must make it possible for ChatGPT to reason safely without inventing missing broker facts.

# PHASE 5 — MULTI-ASSET EXPOSURE CONTEXT

With five assets, simple isolated per-trade risk is not enough.

Add deterministic exposure/context calculations.

At minimum distinguish:

CRYPTO GROUP:
- BTC
- ETH

USD FX GROUP:
- EURUSD
- GBPUSD

METAL:
- XAU

Also identify directional USD exposure where possible.

Examples:

EURUSD LONG and GBPUSD LONG both generally create overlapping USD-short exposure.

BTC LONG + ETH LONG can produce strongly correlated crypto exposure.

Do NOT let deterministic code decide whether a correlated setup is good or bad.

Instead:

- calculate existing exposure;
- calculate proposed added exposure;
- expose the data to ChatGPT;
- enforce only existing hard safety/risk limits;
- reject aggregate risk that exceeds those hard constraints.

Do not invent arbitrary correlation coefficients as trading truth.

If rolling correlation metrics are added, clearly label them as computed statistical context, not deterministic risk facts.

# PHASE 6 — CHATGPT DECISION PROMPT UPDATE

Update the ChatGPT decision contract.

It must explicitly state:

ChatGPT is monitoring:

- BTC
- ETH
- XAU / Gold
- EURUSD
- GBPUSD

with equal monitoring priority.

ChatGPT should dynamically select the strongest opportunities.

Target:

approximately 3–4 logical trades total per day across the five instruments when conditions reasonably permit.

Do not mechanically force trades.

ChatGPT must consider:

- all timeframes;
- technical structure;
- broker contract metadata;
- spreads;
- execution constraints;
- portfolio exposure;
- current open risk;
- news/macro;
- current market conditions.

ChatGPT independently performs current web research when market context requires it.

Relevant context may include:

BTC/ETH:
- funding
- open interest
- liquidations
- crypto market news
- risk sentiment

XAU:
- USD
- yields
- central banks
- geopolitical context
- macro releases
- COT/positioning when useful

EURUSD / GBPUSD:
- Fed
- ECB
- Bank of England
- inflation
- labor data
- GDP
- PMIs
- rate expectations
- major economic calendar events
- USD index / yields where relevant

Broker data remains execution truth.

External web data is context only.

# PHASE 7 — EVENT MONITOR UPDATE

Extend event monitoring to all five instruments.

Keep normal:

5-minute full decision scans.

Between scans monitor meaningful events such as:

- abnormal move;
- breakout;
- volatility expansion;
- spread expansion;
- position proximity to SL/TP;
- order fill/rejection;
- meaningful state change.

Event monitor NEVER decides a trade.

It only triggers a fresh ChatGPT evaluation.

Avoid event storms.

Maintain:

- debounce;
- cycle lock;
- symbol-level safety;
- global duplicate protection.

Add tests for simultaneous events across multiple symbols.

# PHASE 8 — DASHBOARD

Extend the dashboard to clearly show all five assets.

Display at least:

- canonical symbol;
- broker symbol;
- bid/ask;
- spread;
- data freshness;
- trade availability;
- positions;
- pending orders;
- recent ChatGPT decision;
- contract metadata summary where useful.

Portfolio area should show:

- balance
- equity
- daily PnL
- daily drawdown
- trailing DD
- peak equity
- free margin
- aggregate open risk

Performance breakdown:

- BTC
- ETH
- XAU
- EURUSD
- GBPUSD

The dashboard must remain local and must not become dependent on unnecessary third-party services.

# PHASE 9 — DAILY REPORT

Update daily reports for five instruments.

Report:

- total decisions
- WAIT decisions
- trade decisions
- executions
- broker rejections
- wins
- losses
- realized PnL
- unrealized PnL where appropriate
- expectancy where sample size allows
- profit factor where meaningful
- max observed DD
- slippage
- performance by symbol
- performance by setup/timeframe if logged
- event-driven vs scheduled decisions
- news-mode vs normal mode if available
- operational failures/restarts

Do not output mathematically misleading metrics when sample size/data is insufficient.

# PHASE 10 — TELEGRAM

Update Telegram notifications to include the canonical symbol and broker symbol where useful.

Important events:

- entry submitted
- fill
- pending placed
- cancellation
- partial close
- full close
- SL/TP modification
- broker rejection
- hard daily lock
- hard trailing-DD lock
- bridge failure/recovery
- watchdog restart
- critical operational fault
- daily report

Telegram failure must never crash trading runtime.

Never print or commit the bot token.

# PHASE 11 — SAFETY REGRESSION

Reverify all hard safety invariants.

DEMO ONLY.

Hard daily loss:

3%.

Hard trailing drawdown:

10% from highest observed equity.

These cannot be overridden by ChatGPT.

When the new-entry risk lock activates:

NEW LONG/SHORT must be blocked.

But protective actions must remain available:

- HOLD where meaningful
- MOVE_SL
- MOVE_TP
- BREAK_EVEN
- PARTIAL_CLOSE
- FULL_CLOSE
- CLOSE_POSITION
- CANCEL_ORDER

Never prevent risk-reducing management merely because the daily limit was hit.

Verify broker-side SL remains the last-line protection.

# PHASE 12 — AGENTS.md

Create or comprehensively update:

`AGENTS.md`

at the repository root.

This file is mandatory.

It must enable a future coding agent to understand and safely work on GPTTRADDER without reading this conversation.

Include:

## Project purpose

Explain what GPTTRADDER does.

## Architectural ownership

Explicitly:

ChatGPT = sole trading decision owner.

DeepSeek = orchestration only.

Code = deterministic infrastructure/safety.

Broker = execution truth.

## Demo-only rule

Make it extremely clear that real-money support is not currently authorized.

## Supported instruments

Canonical:

- BTC
- ETH
- XAU
- EURUSD
- GBPUSD

Explain broker-symbol mapping.

## Repository map

Describe important directories/files.

## Runtime flow

Describe:

MT5
→ Collector
→ MarketPacket
→ ChatGPT bridge
→ Decision
→ Validator
→ Risk
→ Demo execution
→ Verification
→ Logging

## Commands

Document all useful commands:

- installation
- tests
- smoke
- bridge verification
- preflight
- verify MT5 write
- once
- run
- watchdog
- dashboard
- reports
- autostart installation/removal
- any new discovery/debug commands

## Safety invariants

Document every hard rule.

## Environment variables

Document required and optional variables without secrets.

## Testing requirements

Any agent changing risk/execution/schema must add regression tests.

## Git rules

Never commit:

- `.env`
- credentials
- cookies
- storage state
- broker account secrets
- Telegram secrets
- generated DB/logs

## Change protocol

Before modifying broker execution/risk logic:

1. inspect existing tests;
2. preserve safety;
3. add regression;
4. run full suite;
5. run smoke/preflight where applicable.

## Known operational architecture

Document Windows Task Scheduler, watchdog, bridge and MT5 terminal lifecycle.

## Current release status

Include the actual final test counts from THIS upgrade.

Do not put fake results.

# PHASE 13 — SKILL.md

Create a dedicated reusable skill file.

Preferred location:

`skills/gpttradder/SKILL.md`

If the repository already uses a different agent-skill convention, follow it, but also ensure a clearly discoverable `SKILL.md` exists.

The skill should be called conceptually:

GPTTRADDER Operations & Engineering

Its purpose is to teach future agents how to:

- diagnose runtime failures;
- inspect MT5 safely;
- discover broker symbols;
- validate MarketPackets;
- verify ChatGPT bridge;
- debug WAIT behavior;
- inspect Decision JSON;
- test execution without real-money risk;
- run preflight;
- verify demo write/cancel;
- inspect watchdog;
- inspect dashboard;
- generate daily reports;
- test changes;
- prepare a release.

Include trigger/use cases such as:

Use this skill whenever a task involves:

- GPTTRADDER;
- trading runtime;
- MT5 adapter;
- symbol mapping;
- Decision schema;
- risk engine;
- Playwright/ChatGPT bridge;
- dashboard;
- watchdog;
- reporting;
- release verification.

The SKILL must reinforce:

NEVER ENABLE REAL TRADING.

ChatGPT remains sole trading decision owner.

Do not make SKILL.md generic boilerplate.

Make it specific enough that another competent agent can operate this project correctly without conversation history.

# PHASE 14 — TESTS

The previous verified system had 63 passing tests.

After this upgrade the final count must be >=63.

Add meaningful tests for new behavior.

At minimum test:

1. BTC symbol resolution.
2. ETH symbol resolution.
3. XAU symbol resolution.
4. EURUSD symbol resolution.
5. GBPUSD symbol resolution.
6. broker suffix discovery.
7. ambiguous symbol behavior.
8. missing symbol fail-closed.
9. SymbolContractSpec parsing.
10. contract size.
11. tick size.
12. tick value.
13. volume min/max/step.
14. stops/freeze levels.
15. currency metadata.
16. MT5 monetary SL-risk calculation.
17. normalized volume.
18. invalid minimum-volume case.
19. MarketPacket containing five instruments.
20. quote freshness per symbol.
21. closed/stale XAU not invalidating healthy BTC/FX packet.
22. stale ETH preventing only unsafe ETH decision.
23. aggregate exposure context.
24. duplicate decision prevention.
25. overlapping event-trigger protection.
26. 3% daily loss persistence.
27. 10% peak-equity trailing DD persistence.
28. restart persistence.
29. netting behavior.
30. hedging behavior.
31. protective management after risk lock.
32. timeout hierarchy.
33. dashboard multi-symbol response.
34. daily-report per-symbol aggregation.
35. Telegram message generation.
36. watchdog health behavior.
37. CLI Windows-safe output.
38. Demo-account guard.
39. Real-account rejection.
40. broker write/cancel verification path with Fake MT5.

Run:

`pytest -q`

Run Node bridge tests.

Run Python compile check.

Run lint if configured.

Run smoke.

Do not merely say tests pass — capture the exact final test counts.

# PHASE 15 — REAL DEMO PREFLIGHT

After all unit/integration tests pass:

Use the currently installed MT5 terminal.

Confirm account is still Demo.

Never rely solely on account number.

Check MT5 trade_mode.

Run broker symbol discovery.

Resolve all five desired instruments.

If one instrument is unavailable at this broker:

do not fake it.

Clearly report which one is unavailable.

Run:

`gpttradder preflight`

or the updated equivalent.

Every required check must pass before write tests.

# PHASE 16 — CHATGPT REAL BRIDGE TEST

Use the existing working Playwright/MCP bridge.

Preserve:

`GPTTRADDER_CHATGPT_TIMEOUT_SECONDS=80`

unless empirical testing demonstrates a safer value.

Keep outer decision timeout higher.

Perform at least THREE consecutive real ChatGPT cycles.

Inspect the packets.

Confirm the packet now contains the contract metadata ChatGPT previously said was missing.

The previous failure mode was:

ChatGPT refused LONG/SHORT because contract-size / tick-value / volume-step and related broker details were absent.

This upgrade should eliminate that INFORMATION deficiency.

IMPORTANT:

ChatGPT may still legitimately choose WAIT.

Do NOT force ChatGPT to output LONG/SHORT merely to satisfy a test.

Instead verify:

- it receives sufficient contract data;
- there is no longer a WAIT caused specifically by missing contract metadata;
- no malformed response;
- no freeze;
- cycle IDs are correct;
- retries do not duplicate messages.

# PHASE 17 — SAFE DEMO WRITE VALIDATION

Do NOT force a random live market trade.

Use the existing safe:

`verify-mt5-write`

workflow.

Run it against the Demo account.

Verify:

- broker accepts test pending order;
- volume normalization is valid;
- order appears;
- cancellation succeeds;
- no residue remains.

If it supports symbol selection, test representative asset classes:

- one FX instrument;
- one crypto or XAU instrument.

Still Demo only.

# PHASE 18 — CONTROLLED END-TO-END RUNTIME

Run the real Demo system under watchdog.

Verify:

MT5 Demo
→ five-symbol collector
→ MarketPacket
→ ChatGPT
→ Decision
→ validation
→ logging
→ dashboard

Observe at least several cycles/events.

Do not wait indefinitely for a trade.

A valid WAIT is acceptable.

What matters is that WAIT is not caused by missing broker contract metadata.

Inspect:

- runtime heartbeat
- bridge heartbeat
- dashboard
- DB
- decision log
- error log
- Task Scheduler state

Confirm no service flapping/restart loop.

# PHASE 19 — VERSION

This is a meaningful feature release.

Bump version from 0.3.0 to:

`0.4.0`

unless there is already a newer local version.

Update all authoritative version locations consistently.

Do not leave mismatched package/API/dashboard version strings.

# PHASE 20 — GIT CLEANUP

Before publishing:

Run:

`git status`

Inspect all changes.

Ensure no secrets are staged.

Explicitly check for:

- `.env`
- cookies
- browser storage
- passwords
- broker credentials
- Telegram tokens
- account secrets
- SQLite runtime DB
- logs

Run a secret scan if available.

Remove generated build/runtime junk.

Do not remove legitimate source files.

# PHASE 21 — GITHUB PUBLICATION

The user explicitly authorizes publishing the latest tested version to:

`Tariq990/GPTTRADDER`

GitHub currently may contain an older version than the local working system.

The LOCAL TESTED WORKING COPY is authoritative.

Do NOT merge old GitHub code over newer local fixes.

Before push:

1. Fetch remote refs.
2. Inspect divergence.
3. Protect the current remote main by creating a backup tag or backup branch if useful, for example:

`backup/pre-0.4.0`

4. Ensure the new commit contains the complete tested local system.

Commit message should be clear, for example:

`release GPTTRADDER 0.4.0 multi-asset contract-aware runtime`

Push the tested release to GitHub.

Preferred end state:

`main` = latest fully tested 0.4.0

Do not leave the actual newest code only on an agent branch unless branch protection makes direct update impossible.

If direct main push is prevented:

- push a complete release branch;
- open a PR;
- clearly report that main still needs merge.

Do not claim main is updated unless verified from remote after push.

After push:

FETCH THE REMOTE AGAIN.

Verify from GitHub itself:

- main HEAD SHA
- `pyproject.toml` version
- `AGENTS.md`
- `skills/gpttradder/SKILL.md`
- updated README
- new tests
- five-symbol implementation
- contract metadata code
- CI workflow

Run/observe GitHub CI.

If CI fails:

- inspect logs;
- fix;
- retest locally;
- push correction;
- repeat until green.

# PHASE 22 — FINAL GITHUB VERIFICATION

The final task is NOT complete merely because `git push` returned success.

Verify the actual remote repository.

Report:

REMOTE REPOSITORY:
Tariq990/GPTTRADDER

MAIN HEAD:
<actual SHA>

RELEASE VERSION:
<actual version>

CI:
PASS / FAIL

AGENTS.md:
PRESENT / MISSING

SKILL.md:
PRESENT / MISSING

SUPPORTED INSTRUMENTS:
BTC
ETH
XAU
EURUSD
GBPUSD

LOCAL TEST COUNT:
<exact>

REMOTE CI TEST COUNT:
<exact if visible>

MT5 DEMO PREFLIGHT:
PASS / FAIL

MT5 DEMO WRITE/CANCEL:
PASS / FAIL

REAL CHATGPT CYCLES:
<number tested>

CONTRACT METADATA PRESENT:
PASS / FAIL

WATCHDOG:
PASS / FAIL

DASHBOARD:
PASS / FAIL

DAILY REPORT:
PASS / FAIL

WINDOWS AUTOSTART:
PASS / FAIL

# CRITICAL SAFETY RULES

These rules override everything else.

NEVER:

- enable real trading;
- test on a real-money account;
- remove Demo guard;
- expose credentials;
- commit `.env`;
- commit browser authentication;
- let DeepSeek make trading decisions;
- let local code invent trades;
- silently alter ChatGPT's risk intent;
- blindly retry broker rejection;
- execute a duplicate decision_id;
- use stale quotes for execution;
- force a LONG/SHORT just to prove execution;
- overwrite the newer local project with stale GitHub code.

If the account is not Demo:

STOP broker writes.

If ChatGPT is unavailable:

NO NEW TRADE.

If MarketPacket is stale:

NO NEW TRADE.

If required contract metadata is missing:

NO NEW TRADE.

If decision schema is invalid:

NO NEW TRADE.

If broker constraints require a material change:

REJECT AND REQUERY CHATGPT.

# WORK STYLE

Act autonomously.

Do not stop after each command.

Do not send me a tutorial instead of doing the work.

Use terminal/files/Git/GitHub yourself.

When you find a bug:

1. reproduce it;
2. identify root cause;
3. fix it;
4. add regression test;
5. rerun targeted tests;
6. rerun full suite when appropriate;
7. continue.

Only ask me something if it truly cannot be discovered or safely resolved automatically.

Do not ask me to manually provide:

- broker symbol names, if MT5 can discover them;
- local repo paths, if filesystem search can find them;
- Git branch info, if Git can provide it;
- current account mode, if MT5 can provide it.

# FINAL DELIVERABLE

Do not finish with vague language.

Finish with this exact structured readiness report:

GPTTRADDER RELEASE:
<version>

LOCAL COMMIT:
<SHA>

GITHUB MAIN:
<SHA>

GITHUB URL:
https://github.com/Tariq990/GPTTRADDER

CI:
PASS / FAIL

TESTS:
<X passed, Y failed>

SUPPORTED MARKET MAPPING:
BTC -> <broker symbol>
ETH -> <broker symbol>
XAU -> <broker symbol>
EURUSD -> <broker symbol>
GBPUSD -> <broker symbol>

CONTRACT METADATA:
PASS / FAIL

CHATGPT BRIDGE:
PASS / FAIL

REAL CHATGPT CYCLES:
<number>

MT5 DEMO ACCOUNT:
<account ID>

MT5 DEMO MODE VERIFIED:
YES / NO

MT5 PREFLIGHT:
PASS / FAIL

MT5 SAFE WRITE/CANCEL:
PASS / FAIL

DASHBOARD:
PASS / FAIL

WATCHDOG:
PASS / FAIL

DAILY REPORT:
PASS / FAIL

TELEGRAM:
PASS / NOT CONFIGURED / FAIL

WINDOWS AUTOSTART:
PASS / FAIL

AGENTS.md:
CREATED / UPDATED / MISSING

SKILL.md:
CREATED / UPDATED / MISSING

SECRETS COMMITTED:
NO / YES

CONTINUOUS DEMO RUNTIME:
READY / NOT READY

FILES CHANGED:
<list>

BUGS FOUND AND FIXED:
<list>

REMAINING BLOCKERS:
<none or exact blockers>

FINAL START COMMAND:
<exact command>

Do not state READY unless the evidence above supports it.