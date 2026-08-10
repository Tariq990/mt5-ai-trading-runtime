import test from 'node:test';
import assert from 'node:assert/strict';
import {
  classifySendResult,
  hasDispatchEvidence,
  DISPATCH_STATE_RESPONSE_RECEIVED,
  DISPATCH_STATE_DISPATCHED_CONFIRMED,
  DISPATCH_STATE_DISPATCHED_UNCONFIRMED,
  DISPATCH_STATE_FAILED_BEFORE_DISPATCH,
  DISPATCH_STATE_NOT_FOUND,
} from './dispatch-state.mjs';

test('completed turn with text is RESPONSE_RECEIVED and not retryable', () => {
  const state = classifySendResult({ status: 'completed', text: '{"decision":"WAIT"}', send_confirmed: 1 });
  assert.equal(state.dispatch_state, DISPATCH_STATE_RESPONSE_RECEIVED);
  assert.equal(state.retryable, false);
  assert.equal(state.response_found, true);
  assert.equal(state.found_in_conversation, true);
});

test('send_confirmed without text is DISPATCHED_CONFIRMED and never re-dispatched', () => {
  const state = classifySendResult({ status: 'dispatched', send_confirmed: 1, text: '', send_dispatched_at: 'x' });
  assert.equal(state.dispatch_state, DISPATCH_STATE_DISPATCHED_CONFIRMED);
  assert.equal(state.retryable, false);
  assert.equal(state.response_found, false);
});

test('unknown_after_send with dispatch evidence is DISPATCHED_UNCONFIRMED', () => {
  const state = classifySendResult({
    status: 'unknown_after_send',
    send_confirmed: 0,
    send_dispatch_intent_at: 't1',
    send_dispatched_at: 't2',
    pre_send_anchor_id: 12,
    error_code: 'timeout',
  });
  assert.equal(state.dispatch_state, DISPATCH_STATE_DISPATCHED_UNCONFIRMED);
  assert.equal(state.retryable, false);
  assert.equal(state.found_in_conversation, true);
});

test('failed_before_send without evidence is FAILED_BEFORE_DISPATCH and safely retryable', () => {
  const state = classifySendResult({ status: 'failed_before_send', send_confirmed: 0, error_code: 'unexpected_error' });
  assert.equal(state.dispatch_state, DISPATCH_STATE_FAILED_BEFORE_DISPATCH);
  assert.equal(state.retryable, true);
  assert.equal(state.found_in_conversation, false);
});

test('pre-send UI/intervention statuses are retryable', () => {
  for (const status of ['needs_login', 'challenge_required', 'ui_not_ready', 'browser_closed', 'page_crashed', 'busy', 'generating']) {
    const state = classifySendResult({ status, send_confirmed: 0 });
    assert.equal(state.dispatch_state, DISPATCH_STATE_FAILED_BEFORE_DISPATCH, status);
    assert.equal(state.retryable, true, status);
  }
});

test('running with dispatch evidence is DISPATCHED_UNCONFIRMED', () => {
  const state = classifySendResult({ status: 'running', send_confirmed: 0, send_dispatched_at: 't' });
  assert.equal(state.dispatch_state, DISPATCH_STATE_DISPATCHED_UNCONFIRMED);
  assert.equal(state.retryable, false);
});

test('cancelled without evidence fails closed (never re-dispatched)', () => {
  const state = classifySendResult({ status: 'cancelled', send_confirmed: 0 });
  assert.equal(state.dispatch_state, DISPATCH_STATE_DISPATCHED_UNCONFIRMED);
  assert.equal(state.retryable, false);
});

test('empty result is NOT_FOUND and fails closed', () => {
  const state = classifySendResult({});
  assert.equal(state.dispatch_state, DISPATCH_STATE_NOT_FOUND);
  assert.equal(state.retryable, false);
  assert.equal(state.found_in_conversation, false);
});

test('hasDispatchEvidence reflects durable write-once intent', () => {
  assert.equal(hasDispatchEvidence({ send_dispatch_intent_at: 'x' }), true);
  assert.equal(hasDispatchEvidence({ send_dispatched_at: 'x' }), true);
  assert.equal(hasDispatchEvidence({ send_confirmed: 1 }), true);
  assert.equal(hasDispatchEvidence({ pre_send_anchor_id: 3 }), true);
  assert.equal(hasDispatchEvidence({ status: 'failed_before_send' }), false);
  assert.equal(hasDispatchEvidence({}), false);
});
