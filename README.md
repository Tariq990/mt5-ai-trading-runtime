# MT5 AI Trading Runtime

**GPTTRADDER** is a demo-only multi-asset trading runtime that connects a MetaTrader 5 demo account to an external ChatGPT decision channel while keeping execution, risk, persistence, retries, and safety controls deterministic in Python.

The project is designed around a strict boundary: the model may propose a trading decision, but code remains authoritative for whether that decision is valid and executable.

> **Safety boundary:** real-money trading is hard-blocked. This repository is an engineering project for demo/simulated execution only.

## What this project demonstrates

- Python application architecture beyond a single trading script.
- FastAPI/HTTP control surfaces and a local operational dashboard.
- Broker abstraction with both a simulated broker and MetaTrader 5 demo adapter.
- Pydantic models/configuration with fail-closed validation.
- SQLite-backed state, idempotency, audit trails, and restart-safe risk state.
- Deterministic risk gates around an external AI decision engine.
- Event-driven monitoring plus scheduled decision cycles.
- Retry/reconciliation logic that distinguishes pre-dispatch failures from uncertain post-dispatch states.
- Watchdog, health checks, reporting, optional Telegram alerts, and Windows autostart support.
- Regression tests for broker behavior, risk logic, clock normalization, idempotency, bridge transport, and operational failure modes.

## Stack

| Layer | Technology |
|---|---|
| Runtime | Python 3.11+ |
| API / dashboard | FastAPI, Uvicorn |
| Models / configuration | Pydantic, pydantic-settings |
| Persistence | SQLite |
| Broker integration | MetaTrader5 Python API + simulated broker |
| AI transport | HTTP bridge + Node.js browser/MCP adapter |
| Testing | pytest, pytest-asyncio, Node test runner |
| Operations | watchdog, Task Scheduler, rotating logs, optional Telegram |

## Architecture

```text
MetaTrader 5 demo / SimulatedBroker
        |
        v
Symbol resolution + broker clock normalization
        |
        v
MarketCollector
  quotes / candles / account / positions / orders / deals
        |
        v
MarketPacket + contract metadata + exposure context
        |
        v
External ChatGPT decision bridge
        |
        v
Typed Decision
        |
        v
SafetyEngine + broker-native risk validation
        |
        +---- reject / request a fresh decision
        |
        v
Demo broker execution
        |
        v
SQLite audit/state
  + dashboard
  + reports
  + notifications
  + watchdog
```

### Responsibility boundaries

| Component | Responsibility |
|---|---|
| ChatGPT decision channel | `LONG` / `SHORT` / `WAIT` / management decisions and their explicit trade parameters |
| Python runtime | data collection, validation, risk limits, schema enforcement, idempotency, execution, persistence, retries, observability |
| MT5 demo / simulated broker | execution truth, quotes, contract metadata, fills |
| Optional orchestration | transport and workflow coordination only; it must not invent or alter trading values |

The runtime rejects a model decision when required broker facts or safety conditions are missing. It does not silently “repair” economic intent.

## Supported market set

The current runtime uses canonical application symbols and resolves them to broker-specific names at runtime:

- `BTC`
- `ETH`
- `XAU`
- `EURUSD`
- `GBPUSD`

A broker may expose names such as `BTCUSDm` or `XAUUSD.a`. The symbol-resolution layer handles exact and suffix/prefix candidates and fails closed on ambiguity unless an explicit override is configured.

## Engineering highlights

### 1. Demo-only execution is enforced in code

Real-account execution is not a README convention. Configuration validation rejects attempts to enable real trading, and the MT5 adapter verifies that the connected account is a demo account before broker writes.

### 2. Risk controls are deterministic

New risk is blocked by code-level limits including:

- 3% daily-loss entry lock;
- 10% trailing drawdown lock from persisted peak equity;
- aggregate open-risk + proposed-risk checks;
- contract metadata requirements;
- fresh quote and open-session requirements;
- volume-grid normalization and normalization-drift rejection;
- symbol trade-mode and account-mode checks;
- fresh-price revalidation immediately before execution.

Protective management actions remain available when new entries are locked.

### 3. Broker time is normalized explicitly

MT5 broker timestamps may represent server-local time rather than UTC. The runtime measures the broker offset from fresh ticks, normalizes timestamps, re-measures across cycles for DST changes, and rejects contradictory/manual offsets rather than treating future-stamped quotes as fresh.

### 4. External AI dispatch is idempotent

A decision payload is frozen once per cycle with one SHA-256 fingerprint and one stable client message ID. Retries reuse the byte-identical payload.

The bridge distinguishes states such as:

- `FAILED_BEFORE_DISPATCH`
- `DISPATCHED_UNCONFIRMED`
- `DISPATCHED_CONFIRMED`
- `RESPONSE_RECEIVED`
- `NOT_FOUND`

This prevents a transport retry from casually becoming a duplicate AI turn or duplicate broker action.

### 5. Persistent operational state

SQLite stores cycle/decision/execution state, risk state, bridge-send audit data, dashboard snapshots, notifications, and review events. Critical claims are persisted before side effects where idempotency requires it.

## Repository map

```text
src/gpttradder/
  broker/             broker interface, simulated broker, MT5 demo adapter
  decision/           decision providers and HTTP bridge client
  assets/             bundled prompts / bridge assets
  collector.py        market/account packet construction
  safety.py           hard execution gates
  risk.py             monetary risk, margin and exposure calculations
  symbols.py          canonical -> broker symbol resolution
  timeutil.py         broker clock normalization and market-session logic
  orchestrator.py     end-to-end decision cycle
  db.py               SQLite persistence and claims
  event_monitor.py    event-triggered reevaluation
  webapp.py            FastAPI operational surface
  dashboard.py        local dashboard model/rendering
  reports.py          durable daily reporting
  notifications.py    optional Telegram integration
  watchdog.py         runtime/bridge supervision
  cli.py              command-line entry points

bridge/                Node/browser decision bridge and tests
tests/                 Python regression suite
prompts/               human-readable decision prompt source
docs/                  supporting documentation
REPOSITORY.md           detailed engineering history and design record
```

## Quick start: simulated mode

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env

gpttradder smoke --mock-decision
```

The simulated path is the easiest way to inspect the system without MetaTrader credentials or broker writes.

## MT5 demo setup

MT5 integration requires Windows, the MetaTrader 5 terminal, and a **demo account**:

```powershell
pip install -e ".[dev,mt5]"
```

Example configuration:

```env
GPTTRADDER_ENV=demo
GPTTRADDER_BROKER=mt5
GPTTRADDER_SYMBOLS=["BTC","ETH","XAU","EURUSD","GBPUSD"]
GPTTRADDER_SYMBOL_MAP={}
```

For the external ChatGPT transport, configure the browser/MCP bridge path and conversation URL locally. Credentials, browser state, broker credentials, and tokens must not be committed.

## Verification

Core local checks:

```powershell
python -m pytest -q
python -m compileall -q src tests
npm --prefix bridge run check
```

Operational readiness commands:

```powershell
gpttradder preflight
gpttradder verify-bridge
gpttradder verify-mt5-write
```

`verify-mt5-write` is demo-only: it verifies the broker write/cancel path using a controlled pending-order flow.

The repository's regression suite covers deterministic code paths. Live readiness still depends on external components that unit tests cannot prove: an authenticated browser/MCP session, an MT5 terminal connected to a demo account, and the broker's current symbols/session state.

## Runtime and operations

Run one cycle:

```powershell
gpttradder once --mock-decision
```

Run the supervised runtime:

```powershell
gpttradder watchdog
```

The watchdog can supervise the Python runtime and decision bridge with health checks, heartbeats, and bounded exponential restart backoff.

The local dashboard is intentionally loopback-only:

```text
http://127.0.0.1:8765/dashboard
```

Useful endpoints include:

- `GET /health`
- `GET /dashboard`
- `GET /api/dashboard`
- `GET /api/report/today`
- `POST /cycle`
- `POST /event/{reason}`

## Optional notifications and reports

Telegram alerts are optional and disabled until credentials are supplied locally:

```env
GPTTRADDER_TELEGRAM_ENABLED=true
GPTTRADDER_TELEGRAM_BOT_TOKEN=YOUR_TOKEN
GPTTRADDER_TELEGRAM_CHAT_ID=YOUR_CHAT_ID
```

Daily reports are persisted so a successful delivery is not sent twice after restart.

## Limitations

- Demo/simulated trading only; real-money execution is intentionally blocked.
- The decision channel depends on an external authenticated browser/MCP environment when not using a mock decision provider.
- MT5 symbols, contract metadata, sessions, and server clock behavior vary by broker.
- Passing tests validates implementation behavior, not profitability or trading quality.

## Security / repository hygiene

Do not commit:

- `.env` files with real values;
- broker credentials;
- Telegram/API tokens;
- browser cookies or Playwright state;
- local SQLite runtime databases;
- logs or generated runtime artifacts.

See `.env.example`, `AGENTS.md`, and `REPOSITORY.md` for the detailed operational contract and engineering history.

## License

This repository is **source-available for personal, non-commercial use only** under the [Personal Non-Commercial Software License 1.0](LICENSE).

You may inspect, clone, run, and privately modify the project for your own personal non-commercial use. Commercial use, client work, paid services, resale, SaaS/hosting, redistribution, sublicensing, or inclusion in a commercial product requires prior written permission from the copyright holder.

This is not an OSI-approved open-source license.
