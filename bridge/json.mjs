const JSON_ESCAPES = new Set(['"', '\\', '/', 'b', 'f', 'n', 'r', 't', 'u']);

// ChatGPT markdown output escapes punctuation inside JSON (e.g. cycle\_id).
// Valid JSON escapes are preserved; any other backslash is markdown escaping
// and is dropped so JSON.parse can accept the payload.
export function normalizeMarkdownEscapes(text) {
  let out = '';
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (ch === '\\' && i + 1 < text.length) {
      const next = text[i + 1];
      if (JSON_ESCAPES.has(next)) {
        out += ch + next;
        i += 1;
      } else {
        out += next;
        i += 1;
      }
    } else {
      out += ch;
    }
  }
  return out;
}

function parseJson(text) {
  try { return JSON.parse(text); } catch {}
  return JSON.parse(normalizeMarkdownEscapes(text));
}

export function extractJson(text) {
  const raw = String(text || '').trim();
  if (!raw) throw new Error('ChatGPT returned an empty response');
  try { return parseJson(raw); } catch {}

  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fenced) {
    try { return parseJson(fenced[1].trim()); } catch {}
  }

  let depth = 0;
  let start = -1;
  let inString = false;
  let escaped = false;
  for (let i = 0; i < raw.length; i += 1) {
    const ch = raw[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') { inString = true; continue; }
    if (ch === '{') {
      if (depth === 0) start = i;
      depth += 1;
    } else if (ch === '}') {
      depth -= 1;
      if (depth === 0 && start >= 0) {
        const candidate = raw.slice(start, i + 1);
        try { return parseJson(candidate); } catch { start = -1; }
      }
    }
  }
  throw new Error('No valid Decision JSON object found in ChatGPT response');
}
