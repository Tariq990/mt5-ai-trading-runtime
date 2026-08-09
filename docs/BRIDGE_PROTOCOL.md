# Existing Playwright Bridge Protocol

GPTTRADDER intentionally does not store browser cookies, ChatGPT storage state, passwords, or session data.

The user's existing bridge should expose a local endpoint:

`POST http://127.0.0.1:8787/decision`

Request body:

```json
{
  "task": "TRADE_DECISION_REQUEST",
  "constraints": {
    "decision_owner": "ChatGPT",
    "deepseek_role": "orchestration_only",
    "demo_only": true,
    "return": "Decision JSON only"
  },
  "market_packet": {}
}
```

Response body must be a bare JSON object conforming to `gpttradder.models.Decision`.

The bridge owns browser/session details. GPTTRADDER owns validation and execution.

## Required bridge behavior

1. Reuse the configured authenticated browser session.
2. Send the packet to the designated conversation.
3. Wait until the response is complete.
4. Extract only the structured Decision JSON.
5. Never ask DeepSeek to choose or alter trading values.
6. Return a non-2xx response if the browser session is unhealthy, the response is incomplete, or JSON cannot be validated.
7. Never return the previous response as if it were new unless the Decision explicitly allows reuse.

## Secrets

Do not commit any of these:
- cookies
- storage state
- session tokens
- broker credentials
- API keys

Keep them outside the repository and inject them at runtime.
