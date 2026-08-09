You are the implementation and operations agent for the local project:

C:\Users\tarik\Desktop\GPTTRADDER-0.3.0

Your task is to take full ownership of configuring, testing, debugging, and launching GPTTRADDER on this Windows machine.

Do not give me instructions to execute manually when you can execute them yourself.

Do not stop after finding an error. Diagnose it, fix it, rerun the failed step, and continue until the system is operational or you reach a blocker that genuinely requires information or credentials from me.

## SYSTEM GOAL

GPTTRADDER is a DEMO-only automated trading system for:

- BTC
- XAU / Gold

Architecture:

MT5 Demo
→ market/account collector
→ deterministic validation/risk engine
→ existing Playwright/MCP ChatGPT bridge
→ ChatGPT makes the trading decision
→ structured Decision JSON
→ deterministic validation
→ MT5 Demo execution
→ logging/dashboard/watchdog

Important ownership rules:

- ChatGPT is the ONLY trading decision maker.
- DeepSeek must NEVER create, modify, infer, repair, or replace trading decisions.
- DeepSeek, if used at all, is orchestration only.
- Deterministic code handles validation, safety, broker execution, logging, scheduling, retries and deduplication.
- REAL trading must remain disabled.
- Never remove or bypass the Demo account guard.

## CURRENT PROJECT

Project directory:

C:\Users\tarik\Desktop\GPTTRADDER-0.3.0

A Python virtual environment has already been created:

.venv

The package installation already succeeded.

Installed successfully include:

- gpttradder 0.3.0
- MetaTrader5 5.0.6090
- FastAPI
- Pydantic
- pytest
- httpx
- uvicorn
- required development dependencies

The first smoke test failed because `.env` contained list fields in CSV format.

The error was:

pydantic_settings.exceptions.SettingsError:
error parsing value for field "symbols"

The required `.env` values should use JSON array syntax:

GPTTRADDER_SYMBOLS=["BTCUSD","XAUUSD"]
GPTTRADDER_DECISION_RETRY_DELAYS=[0,15,30,60]

Fix this first.

Also inspect the complete `.env.example` and `.env` for any other Pydantic complex/list fields that need JSON syntax.

Do not assume these are the only two.

## PHASE 1 — LOCAL CONFIGURATION

1. Enter:

C:\Users\tarik\Desktop\GPTTRADDER-0.3.0

2. Use the existing:

.venv

3. Verify:

python --version
pip show gpttradder
pip show MetaTrader5

4. Fix `.env`.

5. Ensure the initial broker remains:

GPTTRADDER_BROKER=simulated

Do NOT connect MT5 for execution yet.

6. Verify all safety settings, including:

DEMO ONLY
3% daily-loss hard stop
10% trailing drawdown
real trading disabled

If any config could accidentally enable real trading, fail closed and fix it.

## PHASE 2 — FULL LOCAL TESTS

Run:

gpttradder smoke --mock-decision

If it fails:

- inspect traceback
- identify root cause
- modify code/config if appropriate
- add regression test when the failure represents a code defect
- rerun

Then run the full test suite:

pytest -q

All tests must pass.

Also run:

python -m compileall -q src tests

Run relevant Node bridge tests if Node is installed.

Inspect package/version consistency.

Do not continue to real external integrations while local tests are failing.

## PHASE 3 — LOCATE EXISTING PLAYWRIGHT/MCP PROJECT

The user already has an operational ChatGPT browser MCP project.

Likely project/repository name:

chatgpt-zcode-browser-mcp

GitHub repository:

Tariq990/chatgpt-zcode-browser-mcp

Find its LOCAL Windows directory automatically if possible.

Search likely locations such as:

C:\Users\tarik\Desktop
C:\Users\tarik\Documents
C:\Users\tarik\Projects
C:\Users\tarik
and existing development directories.

Do NOT modify or destroy the working browser profile/cookies/session.

The existing MCP already handles authenticated ChatGPT through Playwright and persistent browser state.

GPTTRADDER should integrate with it rather than rebuilding browser authentication.

Set:

GPTTRADDER_BROWSER_MCP_DIR=<actual local path>

Do not guess the path if it can be discovered.

## PHASE 4 — CHATGPT CONVERSATION

GPTTRADDER needs a dedicated ChatGPT session/conversation.

Check whether a configured GPTTRADDER session already exists in the MCP.

Preferred session key:

gpttradder

If an existing dedicated GPTTRADDER ChatGPT conversation exists, reuse it.

Otherwise create/bind a dedicated conversation through the existing MCP if its tools support that.

Do not reuse an unrelated normal conversation.

Configure:

GPTTRADDER_CHATGPT_SESSION_KEY=gpttradder

and the correct conversation URL/session binding.

Do not expose cookies or authentication secrets in logs or Git.

## PHASE 5 — START AND VERIFY THE DECISION BRIDGE

Start the GPTTRADDER bridge using the correct packaged/local bridge implementation.

Expected local endpoint:

http://127.0.0.1:8787

Verify:

GET /health

The bridge must successfully communicate with the existing Playwright/MCP system.

Then run:

gpttradder verify-bridge

This test MUST remain safe:

- it may use the real Playwright/ChatGPT path
- but execution must remain on the simulated broker

Expected flow:

GPTTRADDER
→ local decision bridge
→ existing MCP
→ Playwright
→ ChatGPT
→ Decision JSON
→ parser
→ simulator

Validate:

- message reaches ChatGPT
- response completes
- valid JSON is extracted
- cycle_id matches
- duplicate protection works
- no MT5 trade is placed

If ChatGPT returns prose or malformed JSON, improve the integration/prompt/parser safely rather than allowing invalid execution.

## PHASE 6 — MT5 DEMO DISCOVERY

Only after Phase 1–5 pass:

Check whether MetaTrader 5 terminal is installed and running.

Inspect the active account.

It MUST be a Demo account.

If it is a real account:

STOP all broker-write testing immediately.

Do not bypass the Demo guard.

Determine the exact broker symbol names for:

BTC
Gold/XAU

Do not assume:

BTCUSD
XAUUSD

The broker may use suffixes such as:

BTCUSDm
BTCUSD.a
XAUUSDm
XAUUSD.a

Discover the actual symbols programmatically through MetaTrader5 Python if possible.

Update:

GPTTRADDER_SYMBOLS=[...]

using valid JSON array syntax.

Set:

GPTTRADDER_BROKER=mt5

only after confirming the account is Demo.

## PHASE 7 — PREFLIGHT

Run:

gpttradder preflight

The result should validate at least:

- MT5 connected
- account is Demo
- exact symbols resolve
- quotes work
- candle data works
- positions can be read
- pending orders can be read
- recent history can be read
- persistent risk state works
- ChatGPT bridge health works
- account hedging/netting mode identified

Do not ignore FAIL results.

Fix every actionable failure and rerun.

## PHASE 8 — SAFE MT5 WRITE TEST

After preflight is fully clean, run:

gpttradder verify-mt5-write

The write verification must ONLY operate on Demo.

The intended test is:

create a very small pending order sufficiently far from market
→ verify broker accepted it
→ cancel it immediately
→ verify cancellation

Before running, inspect the implementation to ensure:

- it cannot use a real account
- order size respects broker minimum
- the pending price is safely away from current price
- cleanup/cancel happens even if intermediate verification fails

If `verify-mt5-write` is missing or defective, implement/fix it and add tests.

Never place an uncontrolled Market order merely to test connectivity.

## PHASE 9 — DASHBOARD

Verify the integrated dashboard.

Start it using the project's intended runtime.

Expected local dashboard address is approximately:

http://127.0.0.1:8765/dashboard

Verify it displays relevant runtime data such as:

- service health
- broker status
- balance/equity
- daily PnL
- trailing drawdown
- positions
- pending orders
- recent decisions
- executions
- bridge status
- watchdog/runtime status

Fix broken endpoints or UI data wiring.

Do not require an external CDN for basic dashboard operation if the current project is designed to be local/offline.

## PHASE 10 — WATCHDOG

Verify the watchdog supervises BOTH:

- GPTTRADDER runtime
- ChatGPT/Playwright bridge

Test failure behavior safely.

Simulate or cause controlled child-process failure and verify the watchdog detects and restarts the appropriate service.

The watchdog must NEVER invent or execute a fallback trading decision while ChatGPT is unavailable.

If ChatGPT/bridge is down:

- no new trade
- existing broker-side protections remain
- log the failure
- retry/recover service

## PHASE 11 — TELEGRAM

Inspect Telegram integration.

If Telegram credentials are not configured:

do NOT block the rest of the deployment.

Report exactly which values would be needed later, such as token/chat ID.

Ensure Telegram failure cannot stop trading runtime or crash the watchdog.

Verify deduplication/rate-limiting behavior through mocks/tests.

Never print Telegram tokens.

## PHASE 12 — DAILY REPORTING

Verify daily performance reporting.

Test generation from the local database.

It should be idempotent: the same daily report should not be repeatedly sent/generated accidentally.

Include useful metrics where available:

- trades
- wins/losses
- PnL
- drawdown
- BTC vs XAU performance
- WAIT decisions
- executions/rejections
- system failures

## PHASE 13 — WINDOWS AUTOSTART

Inspect the Windows Task Scheduler integration.

Only install autostart after:

- smoke passes
- pytest passes
- bridge verification passes
- MT5 Demo preflight passes
- Demo write/cancel test passes
- watchdog works

Use the project's:

gpttradder autostart-install

or its actual equivalent.

Verify the created task rather than assuming success.

The startup target should preferably launch the watchdog, which then supervises the runtime and bridge.

Do not create duplicate Windows scheduled tasks if one already exists.

## PHASE 14 — FINAL CONTROLLED RUNTIME TEST

Before leaving it continuously running:

start GPTTRADDER on Demo for a controlled test.

Verify:

- BTC and Gold data flow
- regular cycles work
- event monitor works
- ChatGPT receives packets
- Decision JSON validates
- WAIT is logged correctly
- trade decision cannot execute twice
- risk limits remain active
- execution results return from broker
- dashboard updates
- watchdog remains healthy

Do not disable the 3% daily or 10% trailing drawdown protections.

Do not switch to real-money trading.

## IMPORTANT SAFETY RULES

Never:

- enable real trading
- remove the Demo account guard
- expose cookies/API keys/passwords
- let DeepSeek make trading decisions
- let malformed ChatGPT output reach execution
- blindly retry a rejected broker order
- execute the same decision_id twice
- use stale broker prices for Market execution
- delete working Playwright browser state
- commit `.env`, cookies, credentials, storage state or tokens to Git

## CODE CHANGES

You are authorized to modify the LOCAL GPTTRADDER project when needed to fix defects discovered during setup.

For every meaningful bug:

1. identify root cause
2. fix it
3. add or update a regression test
4. rerun relevant tests
5. keep existing safety guarantees

Do not rewrite working components unnecessarily.

## REPORTING STYLE

Work autonomously.

Do not stop after every successful command asking me what to do next.

Continue through all safe phases.

Only ask me when a genuinely unavailable user-specific value is required, for example:

- I must manually log in to MT5
- I must manually authenticate ChatGPT
- Telegram token/chat ID is required and cannot be discovered
- a broker choice/account is ambiguous

At the end, give me one concise readiness report:

LOCAL TESTS:
PASS/FAIL

CHATGPT BRIDGE:
PASS/FAIL

MT5 DEMO READ:
PASS/FAIL

MT5 DEMO WRITE/CANCEL:
PASS/FAIL

DASHBOARD:
PASS/FAIL

WATCHDOG:
PASS/FAIL

DAILY REPORT:
PASS/FAIL

TELEGRAM:
PASS / NOT CONFIGURED / FAIL

WINDOWS AUTOSTART:
PASS/NOT INSTALLED/FAIL

CONTINUOUS DEMO RUNTIME:
READY / NOT READY

Also list:

- every file you changed
- every bug you fixed
- final test counts
- remaining blockers
- exact command that starts the complete system

Do not claim 100% readiness unless every required external Demo test has actually passed.