# ChatGPT Browser Bridge Protocol

GPTTRADDER ships a ready adapter at `bridge/server.mjs` for the existing private `chatgpt-zcode-browser-mcp` project.

The Python process talks only to:

`POST http://127.0.0.1:8787/decision`

The Node adapter talks to the existing MCP over stdio and calls:

- `chatgpt_list_sessions`
- `chatgpt_bind_session` when necessary
- `chatgpt_status`
- `chatgpt_send`

For every market cycle it uses:

```text
client_message_id = gpttradder:<cycle_id>
```

This makes HTTP retry behavior converge on the browser MCP's durable at-most-once send semantics.

## Python request

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

## Adapter response

A bare JSON object conforming to `gpttradder.models.Decision`.

The adapter rejects the response if:

- MCP does not confirm dispatch;
- assistant response is empty;
- no valid JSON object can be extracted;
- returned `cycle_id` differs from the request.

Python performs full Pydantic schema validation after that.

## Local secrets

Do not commit:

- cookies;
- browser storage state;
- broker credentials;
- API keys;
- ChatGPT session tokens.

The existing MCP project continues owning its persistent browser profile.
