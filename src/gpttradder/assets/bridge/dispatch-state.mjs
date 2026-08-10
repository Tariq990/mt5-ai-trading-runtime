// Explicit ChatGPT bridge dispatch-state classification (Fix 6).
//
// The browser MCP's chatgpt_send result is authoritative for at-most-once
// dispatch. Its durable turn row carries write-once dispatch evidence
// (send_dispatch_intent_at / send_dispatched_at / send_confirmed /
// pre_send_anchor_id) that survives restarts:
//
// - A turn WITH evidence reached the conversation. The MCP never re-dispatches
//   it; a repeat call with the same client_message_id returns the stored turn
//   (cached read). Such turns are DISPATCHED_* and are never retried as fresh
//   sends by the runtime — they are reconciled instead (bounded polling for
//   the late response, then fail-closed with a full audit trail).
// - A turn WITHOUT evidence (failed_before_send, ui_not_ready, busy,
//   challenge_required, ...) proves nothing was dispatched and is safe to
//   re-dispatch with the SAME byte-identical frozen payload.
//
// These states are what the Python runtime uses to decide between "safe
// retry", "bounded reconcile" and "fail closed". This module is pure and
// unit-tested; server.mjs only maps MCP results through it.

export const DISPATCH_STATE_RESPONSE_RECEIVED = 'RESPONSE_RECEIVED';
export const DISPATCH_STATE_DISPATCHED_CONFIRMED = 'DISPATCHED_CONFIRMED';
export const DISPATCH_STATE_DISPATCHED_UNCONFIRMED = 'DISPATCHED_UNCONFIRMED';
export const DISPATCH_STATE_FAILED_BEFORE_DISPATCH = 'FAILED_BEFORE_DISPATCH';
export const DISPATCH_STATE_NOT_FOUND = 'NOT_FOUND';

export const DISPATCH_STATES = Object.freeze([
  DISPATCH_STATE_RESPONSE_RECEIVED,
  DISPATCH_STATE_DISPATCHED_CONFIRMED,
  DISPATCH_STATE_DISPATCHED_UNCONFIRMED,
  DISPATCH_STATE_FAILED_BEFORE_DISPATCH,
  DISPATCH_STATE_NOT_FOUND,
]);

// Statuses the MCP itself treats as retryable pre-send failures
// (ChatGPTBridge.#canRetryBeforeSend): the durable row carries no dispatch
// evidence, so re-dispatching the same client_message_id cannot duplicate a
// ChatGPT turn.
const PRE_SEND_SAFE_STATUSES = new Set([
  'failed_before_send',
  'needs_login',
  'challenge_required',
  'ui_not_ready',
  'browser_closed',
  'page_crashed',
  'busy',
  'generating',
]);

export function hasDispatchEvidence(result) {
  return Boolean(
    result?.send_confirmed ||
      result?.send_dispatch_intent_at ||
      result?.send_dispatched_at ||
      result?.pre_send_anchor_id != null,
  );
}

// Classify one chatgpt_send result (asResult shape) into the canonical
// dispatch state + retryability contract consumed by the runtime.
export function classifySendResult(result) {
  const status = result?.status || null;
  const responseText = result?.text || '';
  if (status === 'completed' && responseText.trim()) {
    return {
      dispatch_state: DISPATCH_STATE_RESPONSE_RECEIVED,
      retryable: false,
      response_found: true,
      found_in_conversation: true,
      response_text: responseText,
      status,
      error_code: result?.error_code || null,
    };
  }
  if (result?.send_confirmed) {
    return {
      dispatch_state: DISPATCH_STATE_DISPATCHED_CONFIRMED,
      retryable: false,
      response_found: false,
      found_in_conversation: true,
      response_text: '',
      status,
      error_code: result?.error_code || null,
    };
  }
  if (hasDispatchEvidence(result)) {
    return {
      dispatch_state: DISPATCH_STATE_DISPATCHED_UNCONFIRMED,
      retryable: false,
      response_found: false,
      found_in_conversation: true,
      response_text: '',
      status,
      error_code: result?.error_code || null,
    };
  }
  if (!status) {
    // No turn row at all: nothing can be proven either way. Never guess a
    // fresh dispatch here — a wiped MCP store with an already-delivered
    // message must not create a duplicate ChatGPT turn.
    return {
      dispatch_state: DISPATCH_STATE_NOT_FOUND,
      retryable: false,
      response_found: false,
      found_in_conversation: false,
      response_text: '',
      status: null,
      error_code: result?.error_code || null,
    };
  }
  if (PRE_SEND_SAFE_STATUSES.has(status)) {
    return {
      dispatch_state: DISPATCH_STATE_FAILED_BEFORE_DISPATCH,
      retryable: true,
      response_found: false,
      found_in_conversation: false,
      response_text: '',
      status,
      error_code: result?.error_code || null,
    };
  }
  // running / cancelled / dispatched / sent / any unknown status without a
  // proven reply: the MCP will not re-dispatch these, and we must not guess.
  return {
    dispatch_state: DISPATCH_STATE_DISPATCHED_UNCONFIRMED,
    retryable: false,
    response_found: false,
    found_in_conversation: Boolean(result?.conversation_url),
    response_text: '',
    status,
    error_code: result?.error_code || null,
  };
}
