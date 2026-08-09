import test from 'node:test';
import assert from 'node:assert/strict';
import {
  compactCandleRows,
  candleDigest,
  buildDecisionMessage,
  digestSizeReduction,
} from './digest.mjs';

function row(ts, open, high, low, close, volume) {
  return { ts, open, high, low, close, volume };
}

const packet = {
  cycle_id: 'cycle-1',
  collected_at: '2026-08-09T19:00:00.000Z',
  account: { balance: 10000, equity: 10100, margin: 10, free_margin: 10090, currency: 'USD', mode: 'netting', daily_pnl: 100 },
  exposure: { long_notional: 5000, short_notional: 0, currency: 'USD' },
  symbols: {
    BTC: {
      symbol: 'BTC',
      quote: { ts: '2026-08-09T19:00:00.000Z', bid: 65159.95, ask: 65171.95, spread: 12.0, mode: 'LONGONLY' },
      contract: {
        broker_symbol: 'BTCUSDm', trade_contract_size: 1.0, trade_tick_size: 0.01,
        trade_tick_value: 0.01, volume_min: 0.01, volume_max: 100, volume_step: 0.01,
      },
      candles: {
        '1m': [row(1, 100, 110, 90, 105, 2.5), row(2, 105, 115, 95, 110, 1.0)],
        '5m': [row(3, 110, 120, 100, 115, 4.0)],
      },
    },
    ETH: {
      symbol: 'ETH',
      quote: { ts: '2026-08-09T19:00:00.000Z', bid: 3000.5, ask: 3001.0, spread: 0.5 },
      contract: { broker_symbol: 'ETHUSDm' },
      candles: { '1m': [row(4, 3000, 3010, 2990, 3005, 9.0)] },
    },
    XAU: { symbol: 'XAU', quote: null, contract: { broker_symbol: 'XAUUSD.a' }, candles: {} },
  },
  positions: [],
  orders: [],
  recent_trades: [],
  decision_history: [{ cycle_id: 'cycle-0', decision: 'WAIT', reason_summary: 'flat' }],
};

test('compactCandleRows renders full precision rows joined by ;', () => {
  const rendered = compactCandleRows([row(1, 100.123456789, 110, 90, 105.5, 2.5)]);
  assert.equal(rendered, '1,100.123456789,110,90,105.5,2.5');
});

test('compactCandleRows survives round-trip: values parse back identical', () => {
  const rows = [row(1, 100.123456789, 110, 90, 105.5, 2.5), row(2, 105, 115, 95, 110, 1.0)];
  const parts = compactCandleRows(rows).split(';').map((p) => p.split(',').map(Number));
  assert.deepEqual(parts, [
    [1, 100.123456789, 110, 90, 105.5, 2.5],
    [2, 105, 115, 95, 110, 1.0],
  ]);
});

test('candleDigest covers every symbol and timeframe, sorted', () => {
  const digest = candleDigest(packet.symbols);
  assert.ok(digest.includes('BTC (BTCUSDm) bid=65159.95 ask=65171.95'));
  assert.ok(digest.includes('ETH (ETHUSDm)'));
  assert.ok(digest.includes('XAU (XAUUSD.a)'));
  assert.ok(digest.includes('1m:1,100,110,90,105,2.5;2,105,115,95,110,1'));
  assert.ok(digest.includes('5m:3,110,120,100,115,4'));
  const lines = digest.split('\n');
  assert.ok(lines.indexOf('1m:...') < 0); // never fuse rows into the same line as headers
});

test('buildDecisionMessage embeds digest and JSON state without candle bloat', () => {
  const message = buildDecisionMessage('PROMPT', packet);
  assert.ok(message.startsWith('PROMPT\n\n--- LIVE MARKET PACKET (compact digest'));
  assert.ok(message.includes('"account":{"balance":10000'));
  assert.ok(message.includes('"decision_history"'));
  assert.ok(!message.includes('"candles"'));
});

test('digest is dramatically smaller than full JSON for candle-heavy packets', () => {
  const heavy = structuredClone(packet);
  for (const sym of Object.keys(heavy.symbols)) {
    const candles = {};
    for (const tf of ['1m', '5m', '15m', '30m', '1h', '4h', '1d']) {
      candles[tf] = Array.from({ length: 120 }, (_, i) => row(i + 1, 100 + i / 7, 120 + i / 5, 90 + i / 9, 110 + i / 6, 1 + i / 10));
    }
    heavy.symbols[sym].candles = candles;
  }
  const reduction = digestSizeReduction(heavy);
  assert.ok(reduction > 0.3, `expected >30% reduction, got ${(reduction * 100).toFixed(1)}%`);
  const jsonBytes = Buffer.byteLength(JSON.stringify(heavy));
  const message = buildDecisionMessage('PROMPT', heavy);
  assert.ok(Buffer.byteLength(message) < jsonBytes * 0.7);
});

test('digest keeps messages inside the reliable input band (~185KB) for a 5-symbol packet', () => {
  const heavy = structuredClone(packet);
  for (const sym of ['BTC', 'ETH', 'XAU', 'EURUSD', 'GBPUSD']) {
    const candles = {};
    for (const tf of ['1m', '5m', '15m', '30m', '1h', '4h', '1d']) {
      candles[tf] = Array.from({ length: 120 }, (_, i) => row(1700000000 + i * 60, 65100.12 + i * 0.01, 65200.45 + i * 0.01, 64800.33 + i * 0.01, 65110.27 + i * 0.01, 3.41 + i * 0.01));
    }
    heavy.symbols[sym] = { symbol: sym, quote: { ts: '2026-08-09T19:00:00.000Z', bid: 65150, ask: 65160, spread: 10 }, contract: { broker_symbol: sym + 'm' }, candles };
  }
  const message = buildDecisionMessage('PROMPT', heavy);
  assert.ok(Buffer.byteLength(message) <= 185_000, `message too large: ${Buffer.byteLength(message)}`);
});

test('candle budget keeps only the most recent rows in the message', () => {
  const rows = Array.from({ length: 120 }, (_, i) => row(i, 100, 110, 90, 105, 1));
  const rendered = compactCandleRows(rows, 5).split(';').map((p) => Number(p.split(',')[0]));
  assert.deepEqual(rendered, [115, 116, 117, 118, 119]);
  const full = compactCandleRows(rows, 120).split(';');
  assert.equal(full.length, 120);
});

test('buildDecisionMessage tolerates missing quotes and empty candles', () => {
  const message = buildDecisionMessage('PROMPT', packet);
  assert.ok(message.includes('XAU (XAUUSD.a) bid=? ask=?'));
});

test('buildDecisionMessage is fully deterministic: repeated renders are byte-identical', () => {
  const first = buildDecisionMessage('PROMPT', packet);
  const second = buildDecisionMessage('PROMPT', packet);
  assert.equal(first, second);
  assert.ok(first.includes('quote_age_s=0'), 'ages must be computed against the fixed packet reference time, not Date.now()');
});

test('quote ages are computed against the packet reference instant, not wall clock', () => {
  const stamped = structuredClone(packet);
  stamped.packet_created_at = '2099-01-01T00:00:00.000Z'; // far future reference
  const message = buildDecisionMessage('PROMPT', stamped);
  const match = message.match(/BTC \(BTCUSDm\).*quote_age_s=(-?\d+)/);
  assert.ok(match, 'BTC digest line must carry a numeric quote_age_s');
  const age = Number(match[1]);
  assert.ok(age > 2_000_000_000, `age must follow the fixed reference instant (~2.28e9s), got ${age}`);
  const again = buildDecisionMessage('PROMPT', stamped);
  assert.equal(message, again, 'the far-future reference must not drift between renders');
});