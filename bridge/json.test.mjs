import test from 'node:test';
import assert from 'node:assert/strict';
import { extractJson, normalizeMarkdownEscapes } from './json.mjs';

test('parses bare JSON', () => {
  assert.deepEqual(extractJson('{"decision":"WAIT"}'), { decision: 'WAIT' });
});

test('parses fenced JSON', () => {
  assert.deepEqual(extractJson('```json\n{"decision":"WAIT"}\n```'), { decision: 'WAIT' });
});

test('extracts JSON around incidental prose and braces in strings', () => {
  const value = extractJson('prefix {"reason":"x { y }","decision":"WAIT"} suffix');
  assert.equal(value.reason, 'x { y }');
});

test('normalizes markdown-escaped underscores inside JSON', () => {
  const value = extractJson('{"decision":"WAIT","cycle\\_id":"abc-123","reason":"fair\\_value check \\*done\\*"}');
  assert.equal(value.cycle_id, 'abc-123');
  assert.equal(value.reason, 'fair_value check *done*');
});

test('parses markdown-escaped fenced JSON as written by web ChatGPT', () => {
  const raw = [
    'Here is your decision:',
    '```json',
    '{"decision":"WAIT","cycle\\_id":"c1","reason":"BTC \\_ above candles; WAIT per contract"}',
    '```',
    'Let me know if you need anything else.',
  ].join('\n');
  const value = extractJson(raw);
  assert.equal(value.decision, 'WAIT');
  assert.equal(value.cycle_id, 'c1');
});

test('preserves valid JSON escapes while normalizing markdown escaping', () => {
  assert.equal(normalizeMarkdownEscapes('{"a":"x\\ny","b":"line\\_x","c":"\\"q\\""}'), '{"a":"x\\ny","b":"line_x","c":"\\"q\\""}');
  const value = extractJson('{"a":"x\\ny","b":"line\\_x"}');
  assert.equal(value.a, 'x\ny');
  assert.equal(value.b, 'line_x');
});

test('rejects missing JSON', () => {
  assert.throws(() => extractJson('no decision here'));
});
