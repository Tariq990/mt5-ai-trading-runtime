# GPTTRADDER

Zero-touch, demo-only trading automation scaffold for BTCUSD and XAUUSD.

> **Safety:** This repository is intentionally **DEMO ONLY**. The runtime and broker adapter include guards intended to prevent use with a real-money account.

## Architecture

- **ChatGPT** — sole trading decision engine.
- **DeepSeek** — orchestration/transport helper only; it must never create or alter a trade decision.
- **Deterministic Python code** — market/account collection, risk limits, validation, idempotency, execution, retries, and logging.
- **Broker** — execution source of truth.

The target loop is:

```text
BTCUSD + XAUUSD broker data
        ↓
Collector + Safety Gate
        ↓
Market Packet
        ↓
Existing Playwright / ChatGPT bridge
        ↓
Structured Decision JSON
        ↓
Validator + Risk Engine
        ↓
DEMO execution
        ↓
Verify + Log
```

## Current MVP

- BTCUSD + XAUUSD support.
- Timeframes from 1 minute through 1 day.
- Five-minute polling runtime.
- Event-trigger hooks for future expansion.
- Structured market packets and decision contracts.
- 3% daily-loss hard stop.
- 10% trailing drawdown hard stop from peak equity.
- Duplicate-decision protection.
- SQLite audit log.
- Simulated broker for safe local testing.
- MT5 demo adapter scaffold with explicit demo-account guard.
- HTTP decision bridge contract for an existing Playwright transport.
- Mock decision provider for local end-to-end testing.
- Tests for safety, idempotency, and orchestration.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env
pytest
python -m gpttradder.cli cycle
```

Run the local API:

```bash
uvicorn gpttradder.api:app --reload
```

## Modes

### Simulated broker

Default development mode. It produces deterministic demo quotes and accepts only simulated orders.

### MT5 Demo

Set the broker to `mt5_demo` only after MetaTrader 5 is installed, logged in to a **demo** account, and the symbol mapping has been verified.

The adapter rejects an account unless its environment explicitly identifies it as demo. Do not remove this guard.

## Environment

Copy `.env.example` to `.env` and configure only local non-secret settings there. Never commit:

- broker passwords
- API keys
- session cookies
- Playwright storage state
- account tokens

## Development priorities

1. Keep the full cycle reliable before adding more market data.
2. Never let DeepSeek infer or repair a trade decision.
3. Every decision must have a unique ID and expiration.
4. Refresh bid/ask immediately before market execution.
5. Broker-side SL/TP must remain active if automation fails.
6. A transport failure must never become a fallback trade.

## Documentation

- `docs/ARCHITECTURE.md`
- `docs/BRIDGE_PROTOCOL.md`
- `prompts/chatgpt_decision_prompt.md`
- `prompts/deepseek_orchestrator_prompt.md`

## Status

MVP scaffold. Keep it on DEMO until the transport, broker adapter, and execution verification have been tested extensively.
