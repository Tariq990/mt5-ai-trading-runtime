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

test('rejects missing JSON', () => {
  assert.throws(() => extractJson('no decision here'));
});
