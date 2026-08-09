import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

export async function loadMcpSdk(mcpDir) {
  // Prefer a local bridge dependency if present, but normally reuse the SDK
  // already installed by the existing browser MCP project. This removes a
  // second npm-install requirement from GPTTRADDER.
  try {
    const [{ Client }, { StdioClientTransport }] = await Promise.all([
      import('@modelcontextprotocol/sdk/client/index.js'),
      import('@modelcontextprotocol/sdk/client/stdio.js'),
    ]);
    return { Client, StdioClientTransport, source: 'local' };
  } catch (localError) {
    try {
      const requireFromMcp = createRequire(path.join(mcpDir, 'package.json'));
      const clientPath = requireFromMcp.resolve('@modelcontextprotocol/sdk/client/index.js');
      const stdioPath = requireFromMcp.resolve('@modelcontextprotocol/sdk/client/stdio.js');
      const [{ Client }, { StdioClientTransport }] = await Promise.all([
        import(pathToFileURL(clientPath).href),
        import(pathToFileURL(stdioPath).href),
      ]);
      return { Client, StdioClientTransport, source: 'browser-mcp' };
    } catch (mcpError) {
      throw new Error(
        `MCP SDK unavailable. Install dependencies in the existing browser MCP project (${mcpDir}). ` +
        `Local error: ${localError.message}; MCP error: ${mcpError.message}`
      );
    }
  }
}
