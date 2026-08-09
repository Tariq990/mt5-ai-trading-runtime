import test from 'node:test';
import assert from 'node:assert/strict';
import { extractJson } from './json.mjs';

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

test('parses markdown-escaped JSON keys (ChatGPT web rendering)', () => {
  const value = extractJson('{"cycle\\_id":"c-1","decision":"WAIT","valid\\_until":"x"}');
  assert.deepEqual(value, { cycle_id: 'c-1', decision: 'WAIT', valid_until: 'x' });
});

test('parses markdown-escaped JSON inside fenced block', () => {
  const value = extractJson('```json\n{"decision\\_":"WAIT"}\n```');
  assert.deepEqual(value, { decision_: 'WAIT' });
});

test('unescapes escaped punctuation in arrays and object braces', () => {
  const value = extractJson(
    '{"order":{"entry":65170,"acceptable\\_price\\_range":\\[65170,65190\\]},"decision":"LONG","take\\_profit":\\[{"price":65245,"close\\_percent":30}\\]}',
  );
  assert.deepEqual(value, {
    order: { entry: 65170, acceptable_price_range: [65170, 65190] },
    decision: 'LONG',
    take_profit: [{ price: 65245, close_percent: 30 }],
  });
});

test('rejects missing JSON', () => {
  assert.throws(() => extractJson('no decision here'));
});
