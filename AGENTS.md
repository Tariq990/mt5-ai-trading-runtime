# AGENTS.md

Guidance for AI agents working on GPTTRADDER. Read this before touching the codebase.

## Project purpose

GPTTRADDER is a DEMO-only trading runtime that lets ChatGPT act as the sole trading decision engine over a live MetaTrader 5 demo account (or a simulated fallback broker), with deterministic Python code enforcing every safety invariant.

## Architectural ownership

| Layer | Owner | Responsibilities |
|---|---|---|
| ChatGPT (via browser MCP + HTTP bridge) | Trading decisions ONLY | LONG/SHORT/WAIT/MANAGE/CLOSE/CANCEL JSON, per `decision/base.py` schema |
| DeepSeek / local automation | Orchestration only | Collects market state, transports packets, executes decisions — NEVER invents or alters trading values |
| Code (Python) | Deterministic infrastructure | Safety gates, risk math, persistence, dashboard, watchdog |
| Broker (MT5 demo / SimulatedBroker) | Execution truth | Quotes, contract metadata, fills. External web data is context only, never trusted over broker data |

The ChatGPT decision prompt lives (hash-synced) in:
- `src/gpttradder/assets/prompts/chatgpt_decision_prompt.md`
- `prompts/chatgpt_decision_prompt.md`

## Demo-only rule

REAL MONEY TRADING IS NOT AUTHORIZED. `Settings.allow_real_trading` is hard-forced to `False` by a pydantic validator that raises on any attempt to set it. `MT5DemoBroker.assert_demo()` refuses non-demo accounts with `REAL ACCOUNT BLOCKED`. Never remove or weaken these guards.

## Supported instruments

Canonical symbols (application namespace; packet keys and `Decision.symbol` are canonical):

- `BTC`
- `ETH`
- `XAU` (Gold)
- `EURUSD`
- `GBPUSD`

Broker symbols differ per broker (e.g. `BTCUSDm`, `XAUUSD.a`). `src/gpttradder/symbols.py` maps canonical -> broker at runtime via `resolve_canonical_symbols()`:

1. explicit `GPTTRADDER_SYMBOL_MAP` override (JSON, optional);
2. exact name match;
3. prefix/suffix match (e.g. `XAU` -> `XAUUSD.a`).

Ambiguous matches fail closed (`AMBIGUOUS`) and require an explicit `symbol_map` entry. Missing symbols are `NOT_FOUND`/`UNRESOLVED` and are excluded from packets.

## Repository map

- `src/gpttradder/symbols.py` — canonical symbol list, resolution logic, exposure groups
- `src/gpttradder/models.py` — pydantic schemas: MarketPacket, Decision, Quote, Position, PendingOrder, AccountState, SymbolContractSpec, SymbolStatus (decomposed: broker_trade_mode / quote_fresh / market_session_open / executable_now), ExposureContext
- `src/gpttradder/config.py` — `Settings` (env-prefixed `GPTTRADDER_`), includes the chatgpt<decision timeout hierarchy validator
- `src/gpttradder/timeutil.py` — broker-clock -> UTC normalization (`normalize_broker_epoch`), fail-closed quote freshness (future-stamped quotes are never fresh), weekend market-session calendar per exposure group
- `src/gpttradder/collector.py` — pulls quotes/candles/account/positions/orders/deals into one `MarketPacket`; per-symbol `symbol_status`; canonical translation; aggregate exposure
- `src/gpttradder/risk.py` — `normalize_volume`, `exposure_notional`, `compute_exposure`, `assess_trade_risk` (broker-native monetary risk + margin + normalization drift)
- `src/gpttradder/safety.py` — every hard gate (see Safety invariants); `NORMALIZATION_DRIFT_MAX_PCT = 25.0`
- `src/gpttradder/broker/` — `base.py` (abstract Broker), `simulated.py` (test fallback), `mt5_demo.py` (real MT5, demo-locked)
- `src/gpttradder/orchestrator.py` — cycle flow: collect -> hash -> bextract -> safety -> risk breakdown -> execute -> persist; cycle lock + duplicate prevention
- `src/gpttradder/db.py` — SQLite state (cycles, decisions, executions, risk state, dashboard snapshots)
- `src/gpttradder/decision/` — `bridge.py`/`http_bridge.py`/`mock.py` decision providers
- `src/gpttradder/webapp.py` + `dashboard.py` — loopback-only dashboard + API
- `src/gpttradder/notifications.py` — Telegram (optional)
- `src/gpttradder/reports.py` — durable daily report
- `src/gpttradder/watchdog.py` — zero-touch Windows startup/restart
- `src/gpttradder/verification.py` — `verify_mt5_demo_write` (pending order place + cancel on demo)
- `src/gpttradder/preflight.py` — startup environment/broker/bridge checks (`broker_clock`, `symbols_resolved`, `market:c`, `contract:c` per canonical symbol)
- `src/gpttradder/cli.py` — `gpttradder` console commands
- `src/gpttradder/decision/http_bridge.py` — freezes the per-cycle payload ONCE (byte-identical retries, one SHA-256, one `client_message_id`), persists every send attempt via `on_send`
- `tests/` — pytest suite (see Testing requirements)
- `REPOSITORY.md` — full specification and phase history

## Runtime flow

```
MT5 demo (or SimulatedBroker)
  -> resolve_symbols(canonical)            # broker symbol discovery (symbols.py)
  -> MarketCollector.collect()             # quotes, candles, account, positions, orders, deals
  -> MarketPacket (canonical keys + symbol_status + exposure + contract metadata)
  -> packet hash (hashing.py)              # duplicate/cycle protection
  -> ChatGPT bridge (decision/http_bridge.py) -> Decision JSON
  -> Decision parse + validation (models.py)
  -> SafetyEngine.decision_gate            # freshness, demo, limits, geometry, contract metadata,
                                           # volume normalization, spread, hedging/netting
  -> broker-native risk breakdown          # monetary SL risk + margin + normalization drift
  -> Demo execution (broker adapter)       # mandatory fresh quote check before execute
  -> ExecutionResult -> SQLite + dashboard + Telegram
  -> Daily report (durable)
```

## Commands

```powershell
# Install (Windows + MT5 requires the mt5 extra)
pip install -e .[mt5]

# Tests
python -m pytest -q

# Compile check
python -m compileall -q src

# One decision cycle (requires bridge or --mock)
gpttradder once --mock
gpttradder once

# Preflight (broker/DB/bridge/Telegram checks)
gpttradder preflight

# Verify demo write path (places + cancels a pending order; MT5 demo only)
gpttradder verify-mt5-write

# Run the full runtime (poll loop + event monitor + dashboard)
gpttradder run

# Watchdog (auto-restart; Task Scheduler managed)
gpttradder watchdog

# Dashboard/report
gpttradder dashboard        # or open http://127.0.0.1:8765
gpttradder report           # force-generate daily report

# Windows autostart (Task Scheduler)
gpttradder install-autostart
gpttradder remove-autostart
```

## Safety invariants

Every one of these is enforced by code (mostly `safety.py`), never by ChatGPT:

- DEMO ONLY (`REAL_ACCOUNT_BLOCKED`; `allow_real_trading` rejected by config validator)
- 3% daily loss lock for NEW risk (`DAILY_LOSS_LOCK`)
- 10% trailing drawdown from peak equity for NEW risk (`TRAILING_DD_LOCK`)
- Packet + per-symbol quote freshness (`STALE_PACKET`, `STALE_SYMBOL_QUOTE`)
- Broker timestamps are normalized from server clock to UTC (`normalize_broker_epoch`); quotes still stamped in the FUTURE are never fresh (`TIME_INVALID`, excluded from packets) — endless "freshness" from a 3h-skewed server clock is impossible
- Decisions must reference the current cycle (`CYCLE_MISMATCH`), not be expired (`DECISION_EXPIRED`)
- New entries require `executable_now` = fresh quote + open market session + entry-capable trade mode (`SYMBOL_NOT_EXECUTABLE`); the per-symbol `symbol_status` decomposes broker_trade_mode / quote_fresh / market_session_open / executable_now, fail-closed
- Market session calendar (crypto/ETH/BTC 24/7; usd_fx + metal + unclassified symbols closed Friday 21:00 UTC -> Sunday 21:00 UTC, configurable) — weekend re-emitted quotes are NOT_TRADABLE, never executable
- No new trade without complete contract metadata (`CONTRACT_METADATA_MISSING` requires volume_min, volume_step, trade_contract_size, trade_tick_value)
- Trade mode gates: CLOSEONLY/DISABLED (`SYMBOL_NOT_OPEN_FOR_ENTRY`), LONGONLY/SHORTONLY direction gates
- Position sizing: risk budget enforcement, volume grid normalization; request below broker minimum is rejected (`VOLUME_BELOW_MINIMUM`); normalization drift >25% is rejected (`NORMALIZATION_DRIFT`)
- Price sanity: zero/inverted quotes excluded at collection; entry SL/TP geometry valid; fresh execution quote within `acceptable_price_range`
- Netting accounts: no independent second same-symbol entry (`NETTING_POSITION_CONFLICT`); hedging accounts may
- Daily loss/DD locks block NEW risk only — protective management (HOLD/MOVE_SL/MOVE_TP/BREAK_EVEN/PARTIAL_CLOSE/FULL_CLOSE/CLOSE_POSITION/CANCEL_ORDER) stays available
- Broker-side SL remains the last line of protection
- Decision duplication + overlapping event triggers prevented (DB claims, cycle lock, debounce)
- The ChatGPT message is FROZEN once per cycle: byte-identical retries, one SHA-256, one `client_message_id`; quote ages come from the fixed packet reference instant, never `Date.now()`; every send attempt is persisted in `bridge_sends`
- Dashboard binds loopback only

## Environment variables

All prefixed `GPTTRADDER_` (see `.env.example`). Required for real use: `GPTTRADDER_ENV=demo`, `GPTTRADDER_BROKER=mt5` (default `simulated`), `GPTTRADDER_DB_PATH`. Optional: `GPTTRADDER_SYMBOLS` (JSON array of canonical symbols), `GPTTRADDER_SYMBOL_MAP` (JSON override), `GPTTRADDER_BROKER_SERVER_UTC_OFFSET_HOURS` (explicit MT5 server clock offset override; unset = AUTO-detected at connect from fresh tick probes, re-measured every cycle for DST; any override contradicting the measured clock by >= 0.5h fails closed), `GPTTRADDER_QUOTE_FUTURE_SKEW_TOLERANCE_SECONDS`, `GPTTRADDER_MARKET_SESSION_WEEKEND_START_HOUR_UTC` / `_END_HOUR_UTC`, Telegram (`GPTTRADDER_TELEGRAM_ENABLED/BOT_TOKEN/CHAT_ID`), timing/limits. Secret values (token, chat id) must never be committed or logged.

Timeout invariant (pydantic validator in `config.py`): `GPTTRADDER_CHATGPT_TIMEOUT_SECONDS` must be strictly below `GPTTRADDER_DECISION_TIMEOUT_SECONDS` (defaults 80 < 90).

## Testing requirements

Any change to risk, execution, broker adapters, models, or DB schema must add regression tests. Run `python -m pytest -q` and keep the whole suite green before claiming completion. The suite covers the broker write/cancel path with a `FakeMT5` (no real terminal needed).

## Git rules

Never commit: `.env`, credentials, cookies/session files, `state/` DBs, `logs/`, generated artifacts (see `.gitignore`). No `git push` of secrets.

## Change protocol

Before modifying broker execution/risk logic:

1. inspect existing tests (`tests/`);
2. preserve the safety invariants above;
3. add a regression test proving the new behavior;
4. run the full suite (`python -m pytest -q`) and a compile check;
5. where applicable run `gpttradder preflight` and the demo write verification on an MT5 demo instance.

## Known operational architecture

Windows deployment runs the Python runtime as a service-like process managed by `watchdog.py`: it checks the runtime heartbeat every `watchdog_poll_seconds`, restarts a stale/broken runtime with exponential backoff (`watchdog_restart_backoff_max_seconds`), and keeps `bridge_status` in the DB. Startup is optional via Task Scheduler (`gpttradder install-autostart`). The ChatGPT bridge (`server.mjs`, Playwright/browser MCP) lives in the separate `GPTTRADDER_BROWSER_MCP_DIR`; the runtime auto-starts it when `GPTTRADDER_BRIDGE_AUTO_START=true`. MT5 terminal must be running and logged into the demo account for the mt5 broker adapter to work; all write verification is done against demo only.

## Current release status

v0.4.0 hardening complete: broker server-clock -> UTC normalization (`timeutil.py`), auto-measured broker offset (Raw Trading demo measured UTC+3 EEST; `broker_clock: 3h (auto-measured 3h)` in preflight; DST-safe per-cycle re-measure, fail-closed override validation), decomposed fail-closed `SymbolStatus`, new `SYMBOL_NOT_EXECUTABLE` entry gate, weekend session calendar, deterministic frozen ChatGPT bridge messages (one SHA-256, one `client_message_id`, `bridge_sends` audit trail; root cause of the 502 dedup storms was `Date.now()` in the digest — fixed), lenient ChatGPT shorthand parsing (scalar TP, {min,max} price range), extended markdown-unescape — final test count: 142 Python + 18 node tests; live MT5 preflight all PASS, demo write/cancel PASS, real ChatGPT cycle placed a live demo order (see REPOSITORY.md Fix 4).

Next release in flight: Git init + publish to GitHub `Tariq990/GPTTRADDER`, final verification and readiness report.