# GPTTRADDER Operations

## Recommended Windows process tree

```text
Windows Task Scheduler (ONLOGON)
  └─ gpttradder watchdog
       ├─ node bridge/server.mjs
       │    └─ existing chatgpt-zcode-browser-mcp
       │         └─ Playwright / authenticated ChatGPT profile
       └─ gpttradder run
            ├─ 5-minute scheduler
            ├─ event monitor
            ├─ runtime heartbeat
            ├─ daily report loop
            └─ local dashboard :8765
```

## Watchdog states

SQLite state keys expose:

- `watchdog_status`
- `bridge_status`
- `heartbeat:watchdog`
- `heartbeat:runtime`

A process being alive is not considered sufficient health. The watchdog also checks the HTTP bridge and the runtime heartbeat timestamp.

## Telegram failure behavior

Telegram is never in the broker safety path. A send failure is logged and does not prevent SL/TP/close handling. Runtime startup does fail preflight if Telegram is explicitly enabled but its credentials/authentication are invalid, preventing a false assumption that alerts are working.

## Dashboard security

`dashboard_host` accepts only `127.0.0.1`, `localhost` or `::1`. Public/LAN binding is rejected by configuration validation.

## Daily reports

The report summarizes durable cycle/decision/execution state and current account snapshots. A report date is unique in SQLite. Successful Telegram delivery is not duplicated; failed delivery can be retried.

## Recovery principles

- ChatGPT transport failure: no fallback trading decision.
- Duplicate decision: reject.
- Execution already claimed: reject.
- Runtime heartbeat stale: watchdog restarts runtime.
- Bridge process alive but HTTP health never becomes good: watchdog recycles bridge.
- Real MT5 account detected: hard failure.
- Risk entry lock reached: no new LONG/SHORT; protective management remains available.
