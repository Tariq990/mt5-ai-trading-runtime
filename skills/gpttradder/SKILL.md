# GPTTRADDER Operations & Engineering

Skill for diagnosing, operating, testing, and releasing the GPTTRADDER demo-only trading runtime (ChatGPT decides, Python enforces safety, MT5 demo executes).

## When to use

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

## Ground rules (never break)

- NEVER ENABLE REAL TRADING. `Settings.allow_real_trading` is validator-blocked; `MT5DemoBroker.assert_demo()` rejects real accounts with `REAL ACCOUNT BLOCKED`. Real-money support is NOT authorized, period.
- ChatGPT remains the sole trading decision owner. Automation (and you) only transports packets/executions. Never invent, modify, or "improve" trading values in code paths — the pipeline is written to reject silent edits (see NORMALIZATION_DRIFT).
- Broker data is execution truth. External web data is context only.
- Repo root: `C:\Users\tarik\Desktop\GPTTRADDER-0.3.0`. Read `AGENTS.md` and `REPOSITORY.md` before changing anything.

## Diagnose runtime failures

1. `gpttradder preflight` — runs broker/DB/bridge/Telegram checks up front.
2. Dashboard: `gpttradder dashboard` (or http://127.0.0.1:8765) — account, per-symbol `symbol_status` (TRADABLE / NO_QUOTE / UNRESOLVED / AMBIGUOUS / NOT_TRADABLE), exposure, positions, recent decisions.
3. DB state: `state/gpttradder.sqlite3` (cycles, decisions, executions, risk state). `runtime_heartbeat_age` and `bridge_status` tell you if the runtime/bridge is alive.
4. Logs: `logs/`.
5. Check `symbol_status` reasons — they explain why a symbol is excluded from packets.

## Inspect MT5 safely

- MT5 terminal must be running and logged into the DEMO account.
- Adapter: `src/gpttradder/broker/mt5_demo.py`. All writes go through `assert_demo()`.
- Any symbol manipulation must use `resolve_canonical_symbols()` — do not hard-code broker symbol names; they vary per broker (`BTCUSDm`, `XAUUSD.a`).

## Discover broker symbols

- `resolve_canonical_symbols(available_names, canonical, symbol_map)` in `src/gpttradder/symbols.py`.
- Resolution order: explicit `GPTTRADDER_SYMBOL_MAP` override -> exact name -> suffix match.
- `AMBIGUOUS` fails closed: add an explicit `symbol_map` entry in `.env` (never commit `.env`).
- Missing = `NOT_FOUND` and the symbol is carried in `symbol_status` but excluded from packets.

## Validate MarketPackets

- `src/gpttradder/models.py` — packet is canonical-keyed; each symbol carries quote, candles, `contract`, `symbol_status`, plus portfolio `exposure`.
- Sanity: zero/inverted quotes are dropped at collection (symbol becomes `NO_QUOTE`); all-closed raises "No configured symbol has a live quote".

## Verify the ChatGPT bridge

- Bridge URL default: `http://127.0.0.1:8787/decision` (bridge assets in `GPTTRADDER_BROKER_MCP_DIR`).
- `gpttradder once --mock` runs a cycle with a WAIT provider (no browser).
- `gpttradder once` runs a real cycle through the bridge; check `bridge_status` in the DB and the dashboard.
- Prompt (hash-synced, edit BOTH if changed): `src/gpttradder/assets/prompts/chatgpt_decision_prompt.md` + `prompts/chatgpt_decision_prompt.md`.

## Debug WAIT behavior

- Confirm the packet arrived (dashboard `latest_cycle`); confirm the decision JSON contains `cycle_id` matching the packet, a timezone-aware `valid_until`, and `confidence`.
- Check `decision_history`/`recent_decisions` in the DB and rejection reasons in `executions`.
- WAIT with a reason is normal — ChatGPT may legitimately decline.

## Inspect Decision JSON

- Schema: `Decision` in `src/gpttradder/models.py`. Entry requires `symbol` (canonical!), `order.{type,entry,size}`, `stop_loss`, `risk_percent`, `valid_until`.
- Decisions are stored per cycle in `decisions` table as JSON (`payload_json`).

## Test execution without real-money risk

- `SimulatedBroker` (`src/gpttradder/broker/simulated.py`) is the default broker — full collect/decide/execute path in-process.
- `tests/test_mt5_adapter.py` covers real adapter mechanics with a `FakeMT5` (no terminal needed) including the demo write/cancel path.
- Never point tests at a real account; the demo guard will refuse anyway.

## Run preflight

`gpttradder preflight` — checks: env/demo lock, broker connection, per-symbol `symbols_resolved`, `market:c`, `contract:c` (contract metadata completeness), DB, bridge, Telegram creds when enabled.

## Verify demo write/cancel

`gpttradder verify-mt5-write` — places a pending order and cancels it on the MT5 demo account (requires `GPTTRADDER_BROKER=mt5` and a running demo login). Use after any broker adapter change.

## Inspect watchdog

- `gpttradder watchdog` — restart loop; `watchdog_status`/`watchdog_heartbeat_age` in DB/dashboard.
- Logs and Task Scheduler (`install-autostart` / `remove-autostart`) on Windows.

## Inspect dashboard / reports

- `gpttradder dashboard` — loopback-only; market table shows canonical -> broker symbol, bid/ask, spread, freshness, status, contract summary; exposure card shows per-group gross/net.
- `gpttradder report` — forces today's durable daily report; per-symbol counts, equity change, market state, exposure.

## Test changes

- `python -m pytest -q` — full suite must stay green; add regression tests for risk/execution/models/DB changes.
- `python -m compileall -q src` — compile check.
- Suite covers: safety gates (incl. contract metadata, volume normalization, hedging/netting), collector canonical translation, symbol resolution, exposure math, MT5 adapter + verifier, persistence, watchdog, dashboard, reports.

## Prepare a release

1. Version bump `pyproject.toml` + `src/gpttradder/__init__.py` + `webapp.py` FastAPI version.
2. Full suite green + compile check; record exact counts in `REPOSITORY.md`.
3. Verify no secrets staged (`.gitignore` covers `.env`, `state/`, `logs/`).
4. Commit only intended files; never push secrets.
5. Live checks: `preflight`, `verify-mt5-write`, one controlled `once` cycle on the demo account.