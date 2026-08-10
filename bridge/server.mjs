import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { loadMcpSdk } from './sdk-loader.mjs';
import { extractJson } from './json.mjs';
import { buildDecisionMessage } from './digest.mjs';
import { classifySendResult, DISPATCH_STATE_RESPONSE_RECEIVED, DISPATCH_STATE_FAILED_BEFORE_DISPATCH } from './dispatch-state.mjs';

function sha256Hex(text) {
  return createHash('sha256').update(text).digest('hex');
}

// Per-cycle fingerprint of the rendered decision message. Sending the same
// client_message_id with different text is rejected by the ChatGPT bridge's
// dedup; this guard makes any such regression fail loudly BEFORE dispatch.
const renderedShas = new Map();

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const port = Number(process.env.GPTTRADDER_BRIDGE_PORT || 8787);
const host = '127.0.0.1';
const mcpDir = process.env.GPTTRADDER_BROWSER_MCP_DIR;
const sessionKey = process.env.GPTTRADDER_CHATGPT_SESSION_KEY || 'gpttradder';
const conversationUrl = process.env.GPTTRADDER_CHATGPT_CONVERSATION_URL || '';
const timeoutSeconds = Number(process.env.GPTTRADDER_CHATGPT_TIMEOUT_SECONDS || 180);
// Review channel: a SEPARATE ChatGPT session that only analyses/advisory.
// It must NEVER share the trading session/conversation.
const reviewSessionKey = process.env.GPTTRADDER_CHATGPT_REVIEW_SESSION_KEY || 'gpttradder-review';
const reviewConversationUrl = process.env.GPTTRADDER_CHATGPT_REVIEW_CONVERSATION_URL || '';
const reviewTimeoutSeconds = Number(process.env.GPTTRADDER_CHATGPT_REVIEW_TIMEOUT_SECONDS || 90);

if (!mcpDir) {
  throw new Error('GPTTRADDER_BROWSER_MCP_DIR is required (example: C:\\Tools\\chatgpt-zcode-browser-mcp).');
}
if (!fs.existsSync(path.join(mcpDir, 'src', 'server.mjs'))) {
  throw new Error(`Browser MCP server not found under ${mcpDir}`);
}

const { Client, StdioClientTransport } = await loadMcpSdk(mcpDir);


const decisionPromptPath = process.env.GPTTRADDER_DECISION_PROMPT_PATH || path.join(root, 'prompts', 'chatgpt_decision_prompt.md');
const decisionContract = fs.readFileSync(decisionPromptPath, 'utf8');
const client = new Client({ name: 'gpttradder-http-adapter', version: '1.0.0' });
const transport = new StdioClientTransport({
  command: process.execPath,
  args: ['--disable-warning=ExperimentalWarning', 'src/server.mjs'],
  cwd: mcpDir,
  env: process.env,
  stderr: 'inherit',
});
await client.connect(transport);

function toolPayload(result) {
  const item = result?.content?.find?.((entry) => entry?.type === 'text');
  if (!item?.text) throw new Error('MCP tool returned no text content');
  const parsed = JSON.parse(item.text);
  if (result?.isError || parsed?.status === 'error') {
    throw new Error(parsed?.error || parsed?.error_message || 'MCP tool failed');
  }
  return parsed;
}

async function ensureSession() {
  const list = toolPayload(await client.callTool({ name: 'chatgpt_list_sessions', arguments: {} }));
  const sessions = list?.sessions || [];
  const existing = sessions.find((s) => s.session_key === sessionKey);
  if (existing) {
    if (conversationUrl && existing.conversation_url !== conversationUrl) {
      toolPayload(await client.callTool({
        name: 'chatgpt_bind_session',
        arguments: { session_key: sessionKey, conversation_url: conversationUrl, title: 'GPTTRADDER' },
      }));
    }
    return;
  }
  if (!conversationUrl) {
    throw new Error(`Session '${sessionKey}' is not bound. Set GPTTRADDER_CHATGPT_CONVERSATION_URL once.`);
  }
  toolPayload(await client.callTool({
    name: 'chatgpt_bind_session',
    arguments: { session_key: sessionKey, conversation_url: conversationUrl, title: 'GPTTRADDER' },
  }));
}

async function ensureReviewSession() {
  if (!reviewConversationUrl) {
    throw new Error('GPTTRADDER_CHATGPT_REVIEW_CONVERSATION_URL is not set; review channel disabled');
  }
  if (reviewSessionKey === sessionKey) {
    throw new Error('review session key must differ from the trading session key');
  }
  const list = toolPayload(await client.callTool({ name: 'chatgpt_list_sessions', arguments: {} }));
  const sessions = list?.sessions || [];
  const existing = sessions.find((s) => s.session_key === reviewSessionKey);
  if (existing) {
    if (existing.conversation_url !== reviewConversationUrl) {
      toolPayload(await client.callTool({
        name: 'chatgpt_bind_session',
        arguments: { session_key: reviewSessionKey, conversation_url: reviewConversationUrl, title: 'GPTTRADDER REVIEW' },
      }));
    }
    return;
  }
  toolPayload(await client.callTool({
    name: 'chatgpt_bind_session',
    arguments: { session_key: reviewSessionKey, conversation_url: reviewConversationUrl, title: 'GPTTRADDER REVIEW' },
  }));
}

function readBody(req, maxBytes = 10 * 1024 * 1024) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      size += chunk.length;
      if (size > maxBytes) {
        reject(new Error('request body too large'));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

await ensureSession();
// The review session is best-effort: the bridge still works for trading
// decisions even when the review conversation is not configured.
if (reviewConversationUrl) {
  await ensureReviewSession().catch((error) => {
    console.error(`Review session setup skipped: ${error.message}`);
  });
}

const server = http.createServer(async (req, res) => {
  res.setHeader('content-type', 'application/json; charset=utf-8');
  if (req.method === 'GET' && req.url === '/health') {
    try {
      const status = toolPayload(await client.callTool({ name: 'chatgpt_status', arguments: {} }));
      res.writeHead(200);
      res.end(JSON.stringify({ ok: true, session_key: sessionKey, bridge: status }));
    } catch (error) {
      res.writeHead(503);
      res.end(JSON.stringify({ ok: false, error: error.message }));
    }
    return;
  }

  if (req.method === 'POST' && req.url === '/review') {
    try {
      if (!reviewConversationUrl) {
        throw new Error('review channel is not configured (GPTTRADDER_CHATGPT_REVIEW_CONVERSATION_URL missing)');
      }
      const body = JSON.parse(await readBody(req));
      const eventId = body?.client_message_id;
      const message = body?.message;
      const requestedSession = body?.session_key;
      if (!eventId || typeof message !== 'string' || !message.trim()) {
        throw new Error('client_message_id and message are required');
      }
      if (requestedSession !== reviewSessionKey) {
        throw new Error(
          `review messages must use session_key '${reviewSessionKey}' (got '${requestedSession}'); ` +
            'the trading session can never be used for review messages',
        );
      }
      if (!eventId.startsWith('gpttradder-review:')) {
        throw new Error(`invalid review client_message_id prefix: ${eventId}`);
      }
      const messageSha = sha256Hex(message);
      const send = toolPayload(await client.callTool({
        name: 'chatgpt_send',
        arguments: {
          session_key: reviewSessionKey,
          message,
          message_sha256: messageSha,
          client_message_id: eventId,
          timeout_seconds: body?.timeout_seconds ? Number(body.timeout_seconds) : reviewTimeoutSeconds,
        },
      }));
      const classified = classifySendResult(send);
      if (classified.dispatch_state !== DISPATCH_STATE_RESPONSE_RECEIVED) {
        throw new Error(
          `ChatGPT review bridge: ${classified.status || 'no result'} (${classified.error_code || 'no error code'})`,
        );
      }
      res.writeHead(200);
      res.end(JSON.stringify({ ok: true, send_confirmed: true, event_id: eventId, response: classified.response_text }));
    } catch (error) {
      res.writeHead(502);
      res.end(JSON.stringify({ ok: false, error: error?.message || String(error) }));
    }
    return;
  }

  if (req.method !== 'POST' || req.url !== '/decision') {
    res.writeHead(404);
    res.end(JSON.stringify({ error: 'not found' }));
    return;
  }

  try {
    const body = JSON.parse(await readBody(req));
    const packet = body?.market_packet;
    const cycleId = packet?.cycle_id;
    if (!packet || !cycleId) throw new Error('market_packet.cycle_id is required');

    const message = buildDecisionMessage(decisionContract, packet);
    const messageSha = sha256Hex(message);
    const previousSha = renderedShas.get(cycleId);
    if (previousSha && previousSha !== messageSha) {
      throw new Error(
        `message changed between attempts for cycle ${cycleId} (sha ${previousSha} -> ${messageSha}); ` +
          'retries must reuse the byte-identical frozen message. Aborting this send.',
      );
    }
    renderedShas.set(cycleId, messageSha);
    if (renderedShas.size > 10000) renderedShas.clear();

    let send;
    try {
      send = toolPayload(await client.callTool({
        name: 'chatgpt_send',
        arguments: {
          session_key: sessionKey,
          message,
          message_sha256: messageSha,
          client_message_id: `gpttradder:${cycleId}`,
          timeout_seconds: timeoutSeconds,
        },
      }));
    } catch (error) {
      // MCP call failed before returning a turn. Nothing was dispatched by
      // THIS call; a retry with the same client_message_id is safe because
      // the MCP's durable dedup never re-dispatches a turn that already has
      // dispatch evidence. The runtime decides from the structured state.
      res.writeHead(502);
      res.end(JSON.stringify({
        ok: false,
        error: error?.message || String(error),
        dispatch_state: DISPATCH_STATE_FAILED_BEFORE_DISPATCH,
        retryable: true,
        status: null,
        error_code: 'mcp_call_failed',
        cycle_id: cycleId,
        client_message_id: `gpttradder:${cycleId}`,
        message_sha256: messageSha,
        found_in_conversation: false,
        response_found: false,
      }));
      return;
    }

    const classified = classifySendResult(send);
    if (classified.dispatch_state === DISPATCH_STATE_RESPONSE_RECEIVED) {
      const decision = extractJson(classified.response_text);
      if (String(decision.cycle_id) !== String(cycleId)) {
        throw new Error(`Decision cycle_id mismatch: expected ${cycleId}, received ${decision.cycle_id}`);
      }
      res.writeHead(200);
      res.end(JSON.stringify(decision));
      return;
    }

    // Anything else: report the canonical dispatch state so the runtime can
    // decide between safe retry (nothing dispatched), bounded reconciliation
    // (dispatched, reply pending) and fail-closed (cannot prove either way).
    res.writeHead(502);
    res.end(JSON.stringify({
      ok: false,
      error: `ChatGPT bridge: ${classified.status || 'no result'} (${classified.error_code || 'no error code'})`,
      dispatch_state: classified.dispatch_state,
      retryable: classified.retryable,
      status: classified.status,
      error_code: classified.error_code,
      cycle_id: cycleId,
      client_message_id: `gpttradder:${cycleId}`,
      message_sha256: messageSha,
      found_in_conversation: classified.found_in_conversation,
      response_found: classified.response_found,
    }));
  } catch (error) {
    res.writeHead(502);
    res.end(JSON.stringify({ error: error?.message || String(error) }));
  }
});

server.listen(port, host, () => {
  console.error(`GPTTRADDER decision bridge listening on http://${host}:${port}`);
});

async function shutdown() {
  server.close();
  await client.close().catch(() => {});
  process.exit(0);
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
