# Architecture

```text
BTCUSD + XAUUSD DEMO Broker
          |
          v
  Broker Adapter / Collector
          |
          v
  MarketPacket + Account State
          |
          v
  Hard Safety Gate
  - demo only
  - 3% daily loss
  - 10% trailing DD
          |
          v
  Existing Playwright Bridge
          |
          v
       ChatGPT
  sole decision owner
          |
          v
    Decision JSON
          |
          v
  Deterministic Validator
  - cycle match
  - freshness
  - expiry
  - price range
  - idempotency
          |
          v
  DEMO Broker Execution
          |
          v
 SQLite audit log
```

## Ownership boundaries

- ChatGPT: trading judgment.
- DeepSeek: optional non-trading orchestration only.
- Python code: collection, risk, validation, retries, state, dedupe, broker writes, logging.
- Broker: source of truth for executable prices and fills.

## Runtime modes

- Scheduled scan every 5 minutes.
- Event scans through the local API for breakouts, spread spikes, execution events, or other configured triggers.

## Deliberate MVP omissions

- No real-money account support.
- No automatic self-modifying strategy.
- No broker-independent retry of rejected trades.
- No advanced DOM/OI/funding collector until the end-to-end loop is stable.
