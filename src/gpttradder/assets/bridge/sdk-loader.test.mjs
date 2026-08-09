import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { loadMcpSdk } from './sdk-loader.mjs';

test('falls back to SDK installed by the existing browser MCP project', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gpttradder-sdk-'));
  try {
    fs.writeFileSync(path.join(root, 'package.json'), '{"type":"module"}');
    const sdk = path.join(root, 'node_modules', '@modelcontextprotocol', 'sdk');
    fs.mkdirSync(path.join(sdk, 'client'), { recursive: true });
    fs.writeFileSync(path.join(sdk, 'package.json'), JSON.stringify({
      name: '@modelcontextprotocol/sdk',
      version: 'test',
      type: 'module',
      exports: {
        './client/index.js': './client/index.js',
        './client/stdio.js': './client/stdio.js'
      }
    }));
    fs.writeFileSync(path.join(sdk, 'client', 'index.js'), 'export class Client {}\n');
    fs.writeFileSync(path.join(sdk, 'client', 'stdio.js'), 'export class StdioClientTransport {}\n');

    const loaded = await loadMcpSdk(root);
    assert.equal(loaded.source, 'browser-mcp');
    assert.equal(typeof loaded.Client, 'function');
    assert.equal(typeof loaded.StdioClientTransport, 'function');
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});
