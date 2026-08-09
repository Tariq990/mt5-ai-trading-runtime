# Architecture

```text
BTCUSD + XAUUSD MT5 DEMO
        |
        +--> quotes / candles / DOM when available
        +--> positions / pending / recent deals
        +--> account equity
        |
        v
Collector + durable risk state
        |
        v
MarketPacket + prior decision history
        |
        v
5m scheduler <---- Event Monitor (price/spread/breakout)
        |
        v
Cross-process cycle lock
        |
        v
Local HTTP Decision Adapter
        |
        v
Existing chatgpt-zcode-browser-mcp (stdio MCP)
        |
        v
ChatGPT (sole trading decision owner)
        |
        v
Strict Decision JSON
        |
        v
Decision validation
- cycle ID / expiry
- account entry lock
- trade geometry
- fresh execution quote
- monetary SL risk
- daily / trailing headroom
        |
        v
Atomic execution claim in SQLite
        |
        v
MT5 DEMO order_check -> order_send
        |
        v
Execution verification + audit log
```

## Ownership boundaries

- **ChatGPT:** market judgment, direction, timing, size, risk %, SL/TP, order type, and position-management decisions.
- **DeepSeek:** optional orchestration only when a deterministic implementation is impractical; no trading values.
- **Python/Node code:** transport, collection, schema validation, risk enforcement, dedupe, retries, scheduling, event detection, broker writes, logging.
- **MT5 broker:** execution source of truth for quotes, fills, positions and orders.

## At-most-once execution

There are two independent idempotency layers:

1. Browser MCP uses a stable `client_message_id` and returns its stored result on retries.
2. GPTTRADDER inserts an execution claim keyed by `decision_id` before touching MT5.

A process crash after the local claim fails closed: the same decision is not automatically resent to the broker.

## Risk persistence

SQLite stores:

- current risk day;
- daily start equity;
- highest equity ever observed by the runtime.

A process restart therefore does not reset the 3% daily or 10% trailing drawdown controls. The event monitor samples equity between 5-minute scans to reduce missed peak-equity moves.

## Runtime modes

- Scheduled full scan every five minutes.
- Event monitor between scans.
- Manual local API event triggers remain available for integrations.

## External readiness gates

`gpttradder verify-bridge` verifies the real browser transport while forcing simulated execution.

`gpttradder preflight` verifies the selected broker account, exact symbols, market reads, inventory reads, persistent risk state and bridge health before continuous Demo runtime is started.

## Operations layer (0.3.0)

```text
Windows ONLOGON task
        |
        v
Watchdog ---------------------> Telegram operational alerts
 |   |
 |   +--> bridge HTTP health / restart
 |   +--> runtime heartbeat / restart
 |
 +--> Node bridge --> existing Playwright MCP --> ChatGPT
 +--> Trading runtime
       +--> dashboard (loopback only)
       +--> daily report scheduler
       +--> Telegram execution alerts
       +--> SQLite heartbeats/report state
```
