# GPTTRADDER

Zero-touch **DEMO-only** BTC/XAU trading automation with ChatGPT as the sole trading decision engine.

## Responsibility boundaries

- **ChatGPT:** trading judgment — LONG/SHORT/WAIT, entry type, size, risk, SL/TP and position management.
- **DeepSeek:** optional orchestration only; it is not allowed to invent or modify trading values.
- **Deterministic code:** broker data, validation, hard risk limits, idempotency, execution, retries, event detection, logging, watchdog, reports and dashboard.
- **MT5:** execution source of truth. Real-account trading is hard-blocked.

## Included in 0.4.0

### Trading/runtime
- BTC + XAU monitored together.
- 1m, 5m, 15m, 30m, 1h, 4h and 1d data.
- Full 5-minute cycles plus price/spread/breakout event interrupts.
- Market, Limit and Stop entries.
- HOLD, MOVE_SL, MOVE_TP, BREAK_EVEN, PARTIAL_CLOSE and FULL_CLOSE.
- Pending-order cancellation.
- MT5 hedging/netting detection.
- Fresh quote revalidation before new entries.
- Stale-market-data rejection per symbol.

### Hard safety
- Demo account required before broker writes.
- 3% daily loss entry lock persisted across restarts.
- 10% trailing drawdown entry lock from persisted peak equity.
- Open stop-risk + new stop-risk bounded by remaining risk headroom.
- `decision_id` claimed in SQLite **before** broker write.
- Cross-process cycle lock.
- Stable ChatGPT bridge message IDs for retry deduplication.
- Protective close/tighten/cancel actions remain available when new-risk entry is locked.

### Operations
- Local dashboard at `http://127.0.0.1:8765/dashboard`.
- Telegram execution/error alerts with persistent anti-spam dedupe.
- Daily report at a configurable local time (default 23:55 Asia/Amman).
- Runtime + bridge watchdog with health checks, heartbeat detection and exponential restart backoff.
- Windows Task Scheduler auto-start at user logon.
- Rotating local logs.
- Read-only preflight, safe browser bridge verification, and Demo-only MT5 write/cancel verification.

## Install

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,mt5]"
Copy-Item .env.example .env
```

The GPTTRADDER bridge reuses `@modelcontextprotocol/sdk` from your existing `chatgpt-zcode-browser-mcp` installation, so there is no second Node dependency install step.

Edit `.env` and set at minimum:

```env
GPTTRADDER_BROKER=mt5
GPTTRADDER_SYMBOLS=BTCUSD,XAUUSD
GPTTRADDER_BROWSER_MCP_DIR=C:\path\to\chatgpt-zcode-browser-mcp
GPTTRADDER_CHATGPT_CONVERSATION_URL=https://chatgpt.com/c/YOUR-CONVERSATION-ID
```

Your broker may use names such as `BTCUSDm` or `XAUUSD.a`; use exactly what MT5 shows.

## Optional Telegram

Add your own credentials locally — never commit them:

```env
GPTTRADDER_TELEGRAM_ENABLED=true
GPTTRADDER_TELEGRAM_BOT_TOKEN=YOUR_TOKEN
GPTTRADDER_TELEGRAM_CHAT_ID=YOUR_CHAT_ID
```

The preflight checks Telegram authentication when enabled.

## Readiness gates

Run these before continuous Demo operation:

```powershell
pytest -q
npm --prefix bridge run check
gpttradder verify-bridge
gpttradder preflight
gpttradder verify-mt5-write
```

`verify-bridge` uses the real Playwright → ChatGPT path but **forces simulated broker execution**.

`verify-mt5-write` is Demo-only. It places a minimum-volume pending order far from market and immediately cancels it to verify `order_check → order_send → cancel` on the actual broker.

## Zero-touch mode

The watchdog is the recommended entrypoint:

```powershell
gpttradder watchdog
```

When configured, it:

1. checks/starts the Node ChatGPT bridge;
2. waits for bridge health;
3. starts GPTTRADDER runtime;
4. monitors runtime heartbeat and bridge HTTP health;
5. restarts failed/wedged components with bounded exponential backoff.

Install it at Windows logon:

```powershell
gpttradder autostart-install
```

Remove the scheduled task:

```powershell
gpttradder autostart-remove
```

The task name is `GPTTRADDER-Watchdog`.

## Dashboard

With runtime running:

```text
http://127.0.0.1:8765/dashboard
```

The dashboard is deliberately restricted to loopback interfaces and uses no CDN/external frontend assets. It displays account/risk state, market quotes, open positions, pending orders, watchdog/bridge state and recent decisions.

Useful API endpoints:

- `GET /health`
- `GET /dashboard`
- `GET /api/dashboard`
- `GET /api/report/today`
- `POST /cycle`
- `POST /event/{reason}`

## Reports

Print today's report:

```powershell
gpttradder report
```

Force-send it to Telegram (when enabled):

```powershell
gpttradder report-send
```

Daily reports are persisted in SQLite and are not sent twice after a successful delivery. Failed Telegram delivery remains retryable.

## Logs

- `logs/gpttradder.log`
- `logs/watchdog.log`
- `logs/runtime-supervised.log`
- `logs/bridge-supervised.log`
- `logs/watchdog-autostart.log`

## Commands

```text
gpttradder smoke --mock-decision
gpttradder verify-bridge
gpttradder preflight
gpttradder verify-mt5-write
gpttradder once
gpttradder run
gpttradder watchdog
gpttradder report
gpttradder report-send
gpttradder autostart-install
gpttradder autostart-remove
```

## What local CI cannot prove

Tests can fully validate the code, simulator, fake-MT5 request shapes, dashboard, notification logic, watchdog policy and packaging. The actual Windows machine still owns three external dependencies: its authenticated Playwright profile, its MT5 terminal/broker symbols, and its Demo broker write path. The three readiness commands above are the gates for those dependencies.

Experimental Demo trading only. Technical correctness does not guarantee profitable trading decisions.
