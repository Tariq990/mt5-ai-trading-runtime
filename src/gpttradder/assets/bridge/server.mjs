import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { loadMcpSdk } from './sdk-loader.mjs';
import { extractJson } from './json.mjs';
import { buildDecisionMessage } from './digest.mjs';

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
    const send = toolPayload(await client.callTool({
      name: 'chatgpt_send',
      arguments: {
        session_key: sessionKey,
        message,
        message_sha256: messageSha,
        client_message_id: `gpttradder:${cycleId}`,
        timeout_seconds: timeoutSeconds,
      },
    }));
    if (!send?.send_confirmed) throw new Error('ChatGPT bridge did not confirm message dispatch');
    const decision = extractJson(send.text);
    if (String(decision.cycle_id) !== String(cycleId)) {
      throw new Error(`Decision cycle_id mismatch: expected ${cycleId}, received ${decision.cycle_id}`);
    }
    res.writeHead(200);
    res.end(JSON.stringify(decision));
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
