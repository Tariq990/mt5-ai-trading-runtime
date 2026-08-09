// ChatGPT web renders some punctuation in assistant replies with Markdown
// escapes (e.g. cycle\_id). `\_` is not a valid JSON escape, so those replies
// must be Markdown-unescaped before extraction. The patterns below never
// appear in valid JSON, so the rewrite is safe for real JSON strings too.
const MARKDOWN_ESCAPES = [
  ['\\_', '_'],
  ['\\*', '*'],
  ['\\#', '#'],
  ['\\-', '-'],
  ['\\>', '>'],
];

export function unescapeMarkdown(text) {
  let out = String(text ?? '');
  for (const [from, to] of MARKDOWN_ESCAPES) out = out.split(from).join(to);
  return out;
}

export function extractJson(text) {
  const raw = unescapeMarkdown(text).trim();
  if (!raw) throw new Error('ChatGPT returned an empty response');
  try { return JSON.parse(raw); } catch {}

  const fenced = raw.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fenced) {
    try { return JSON.parse(fenced[1].trim()); } catch {}
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
        try { return JSON.parse(candidate); } catch { start = -1; }
      }
    }
  }
  throw new Error('No valid Decision JSON object found in ChatGPT response');
}
