// Transport-layer compaction for the ChatGPT decision message.
//
// The full MarketPacket (JSON) easily exceeds ~450KB with five instruments, and
// ChatGPT web truncates or errors on oversized messages. This module renders the
// packet into a compact text digest WITHOUT altering any numeric value: prices,
// volumes, sizes and remote quotes pass through JS's shortest round-trip float
// formatting unchanged. It is transport formatting only — the durable packet in
// SQLite stays complete, and every value here is byte-identical to the packet
// when parsed back.
//
// CANDLE_BUDGET_PER_TF: the digest sends only the most recent N candles per
// symbol/timeframe (the oldest rows are dropped from the MESSAGE only; the
// packet in the database keeps the full candle_limit=120). This keeps the
// message inside ChatGPT web's reliable input band (~185KB worked every time on
// this machine; 260KB+ fails intermittently once the thread accumulates).
// Live 5-symbol packet at budget 120 measured 267,000 bytes; budget 75 lands
// at ~167,000 bytes.

export const CANDLE_BUDGET_PER_TF = 75;

export function compactCandleRows(rows, budget = CANDLE_BUDGET_PER_TF) {
  // rows: [{ts, open, high, low, close, volume?}, ...]
  // Out: "ts,open,high,low,close,volume; ts,..."
  const kept = Array.isArray(rows) ? rows.slice(-budget) : [];
  return kept
    .map((c) => [c.ts, c.open, c.high, c.low, c.close, c.volume ?? 0].join(','))
    .join(';');
}

export function quoteAgeSeconds(ts) {
  if (!ts) return null;
  const at = new Date(ts).getTime();
  if (Number.isNaN(at)) return null;
  return (Date.now() - at) / 1000;
}

export function candleDigest(symbols) {
  // symbols: dict symbol -> {symbol, quote, candles, contract, market}
  const lines = [];
  for (const sym of Object.keys(symbols || {}).sort()) {
    const data = symbols[sym] || {};
    const q = data.quote || {};
    const c = data.contract || {};
    const age = quoteAgeSeconds(q.ts);
    lines.push(
      `${sym} (${c.broker_symbol || '?'}) bid=${q.bid ?? '?'} ask=${q.ask ?? '?'} spread=${q.spread ?? '?'} quote_age_s=${age == null ? '?' : Math.round(age)}`,
    );
    if (c.volume_min != null) {
      lines.push(
        `contract: size=${c.trade_contract_size ?? '?'} tick_size=${c.trade_tick_size ?? '?'} tick_value=${c.trade_tick_value ?? '?'} vol_min=${c.volume_min} vol_max=${c.volume_max ?? '?'} vol_step=${c.volume_step ?? '?'} trade_mode=${c.trade_mode_label ?? '?'} stops_level=${c.trade_stops_level ?? '?'} freeze_level=${c.trade_freeze_level ?? '?'}`,
      );
    } else if (c.broker_symbol) {
      lines.push(`contract: size=${c.trade_contract_size ?? '?'} tick_size=${c.trade_tick_size ?? '?'} tick_value=${c.trade_tick_value ?? '?'}`);
    }
    for (const tf of Object.keys(data.candles || {}).sort()) {
      const rows = data.candles[tf] || [];
      const rendered = compactCandleRows(rows);
      lines.push(`${tf}:${rendered}`);
    }
  }
  return lines.join('\n');
}

export function buildDecisionMessage(contract, packet) {
  const packetForJson = { ...packet };
  delete packetForJson.symbols;
  const preamble = [
    '--- LIVE MARKET PACKET (compact digest; candle rows are ts,open,high,low,close,volume) ---',
    candleDigest(packet.symbols),
    '--- ACCOUNT / POSITIONS / STATE (JSON) ---',
    JSON.stringify(packetForJson),
  ].join('\n');
  return `${contract}\n\n${preamble}\n\nReturn exactly one Decision JSON object and nothing else.`;
}

export function digestSizeReduction(packet) {
  const message = buildDecisionMessage('PROMPT', packet);
  const jsonSize = Buffer.byteLength(JSON.stringify(packet));
  return 1 - message.length / jsonSize;
}