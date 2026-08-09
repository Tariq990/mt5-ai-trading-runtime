from __future__ import annotations

from html import escape


def dashboard_html() -> str:
    # Self-contained/no-CDN dashboard so it keeps working during internet outages.
    return r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>GPTTRADDER Dashboard</title>
<style>
:root{color-scheme:dark;--bg:#0b0d10;--card:#141820;--line:#242b36;--muted:#9aa5b1;--good:#4ade80;--bad:#fb7185;--warn:#fbbf24;--text:#eef2f7}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,Segoe UI,Arial;background:var(--bg);color:var(--text)}
main{max-width:1280px;margin:auto;padding:24px}.top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:20px}
h1{font-size:23px;margin:0}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}.value{font-size:25px;font-weight:700;margin-top:6px}.good{color:var(--good)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.section{margin-top:16px}.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px 7px;border-bottom:1px solid var(--line);font-size:12px}th{color:var(--muted)}pre{white-space:pre-wrap;word-break:break-word;margin:0;font:12px ui-monospace,Consolas,monospace}.pill{display:inline-block;padding:3px 8px;border-radius:999px;background:#222936;border:1px solid #303949}.status{height:9px;width:9px;border-radius:50%;display:inline-block;margin-right:6px;background:var(--warn)}
@media(max-width:900px){.grid{grid-template-columns:repeat(2,1fr)}.two{grid-template-columns:1fr}}@media(max-width:520px){.grid{grid-template-columns:1fr}main{padding:14px}}
</style>
</head>
<body><main>
<div class="top"><div><h1>GPTTRADDER</h1><div class="muted">DEMO runtime monitor</div></div><div><span id="dot" class="status"></span><span id="live">Loading…</span></div></div>
<div class="grid">
 <div class="card"><div class="muted">Equity</div><div class="value" id="equity">—</div><div id="balance" class="muted">—</div></div>
 <div class="card"><div class="muted">Daily PnL</div><div class="value" id="pnl">—</div><div class="muted">3% hard entry lock</div></div>
 <div class="card"><div class="muted">Trailing DD</div><div class="value" id="dd">—</div><div class="muted">10% from peak</div></div>
 <div class="card"><div class="muted">Today</div><div class="value" id="trades">—</div><div id="cycles" class="muted">—</div></div>
</div>
<div class="two section">
 <div class="card"><h3>System</h3><div id="system"></div></div>
 <div class="card"><h3>Market</h3><div id="market"></div></div>
</div>
<div class="card section"><h3>Portfolio exposure</h3><div id="exposure"></div></div>
<div class="two section">
 <div class="card"><h3>Open positions</h3><div style="overflow:auto"><table><thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Current</th><th>PnL</th></tr></thead><tbody id="positions"></tbody></table></div></div>
 <div class="card"><h3>Pending orders</h3><div style="overflow:auto"><table><thead><tr><th>Symbol</th><th>Side</th><th>Type</th><th>Size</th><th>Price</th></tr></thead><tbody id="orders"></tbody></table></div></div>
</div>
<div class="card section"><h3>Recent decisions</h3><div style="overflow:auto"><table><thead><tr><th>Time</th><th>Decision</th><th>Symbol</th><th>Execution</th><th>Reason</th></tr></thead><tbody id="decisions"></tbody></table></div></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const n=(v,d=2)=>v==null?'—':Number(v).toLocaleString(undefined,{maximumFractionDigits:d});
function age(v){if(v==null)return'no heartbeat';if(v<2)return'now';if(v<60)return Math.round(v)+'s ago';return Math.round(v/60)+'m ago'}
function row(vals){return '<tr>'+vals.map(v=>'<td>'+v+'</td>').join('')+'</tr>'}
async function refresh(){try{const r=await fetch('/api/dashboard',{cache:'no-store'});if(!r.ok)throw new Error(r.status);const x=await r.json();const a=x.account||{};document.querySelector('#equity').textContent=n(a.equity);document.querySelector('#balance').textContent='Balance '+n(a.balance);let pnl=Number(a.daily_pnl||0);let pe=document.querySelector('#pnl');pe.textContent=(pnl>=0?'+':'')+n(pnl);pe.className='value '+(pnl>=0?'good':'bad');let dd=Number(a.trailing_drawdown_pct||0);let de=document.querySelector('#dd');de.textContent=n(dd)+'%';de.className='value '+(dd>=8?'bad':dd>=5?'warn':'good');const day=x.day||{};document.querySelector('#trades').textContent=(day.execution_counts?.FILLED||0)+' filled';document.querySelector('#cycles').textContent=(day.cycles||0)+' cycles / '+(day.decisions||0)+' decisions';document.querySelector('#system').innerHTML=`<p><span class="pill">Runtime: ${esc(age(x.runtime_heartbeat_age))}</span></p><p>Watchdog: ${esc(x.watchdog_status)} (${esc(age(x.watchdog_heartbeat_age))})</p><p>Bridge: ${esc(x.bridge_status)}</p><p>Last cycle: ${esc(x.latest_cycle?.status||'—')}</p>`;let market='';for(const [sym,m] of Object.entries(x.symbols||{})){let q=m.quote||{};let c=m.contract||{};let st=(x.symbol_status||{})[sym]||{};let qage=q.ts?parseFloat(((Date.now()-new Date(q.ts).getTime())/1000).toFixed(0)):null;let ok=qage!=null&&qage>=0&&qage<90&&st.status==='TRADABLE';
let exe=!!st.executable_now;
market+=`<p><b>${esc(sym)}</b> <span class="pill">${esc(c.broker_symbol||'—')}</span> &nbsp; Bid ${n(q.bid)} &nbsp; Ask ${n(q.ask)} &nbsp; Spread ${n(q.spread,5)} &nbsp; <span class="${ok?'good':'warn'}">${esc(age(qage))}</span> &nbsp; <span class="${exe?'good':'warn'}">${exe?'EXECUTABLE':'NOT EXECUTABLE'}</span></p>`;market+=`<p class="muted">${esc(st.status||'—')}${st.status&&st.status!=='TRADABLE'&&st.reason?' ('+esc(st.reason)+')':''} &middot; session ${st.market_session_open?'open':'closed'} &middot; trade mode ${esc(c.trade_mode_label||'—')} &middot; quote fresh ${st.quote_fresh?'yes':'no'} &middot; Vol ${n(c.volume_min,3)}-${n(c.volume_max)} step ${n(c.volume_step,3)} &middot; CSize ${n(c.trade_contract_size)} &middot; TickVal ${n(c.trade_tick_value,5)}</p>`}document.querySelector('#market').innerHTML=market||'<span class="muted">No packet yet</span>';let ex=x.exposure||{};let usdDir=ex.usd_direction==null?'—':(ex.usd_direction>0?'net short USD':'net long USD');let ehtml=`<p class="muted">Total gross ${n(ex.total_gross_exposure)} &middot; Total net ${n(ex.total_net_exposure)} &middot; ${esc(usdDir)}</p>`;ehtml+='<table><thead><tr><th>Group</th><th>Gross</th><th>Net</th><th>Positions</th></tr></thead><tbody>';for(const [g,eg] of Object.entries(ex.groups||{})){ehtml+=row([esc(eg.group),n(eg.gross_exposure),n(eg.net_exposure),n((eg.position_count)||0,0)])}ehtml+='</tbody></table>';document.querySelector('#exposure').innerHTML=ehtml;document.querySelector('#positions').innerHTML=(x.positions||[]).map(p=>row([esc(p.symbol),esc(p.side),n(p.size),n(p.entry_price),n(p.current_price),n(p.unrealized_pnl)])).join('')||row(['—','','','','','']);document.querySelector('#orders').innerHTML=(x.pending_orders||[]).map(o=>row([esc(o.symbol),esc(o.side),esc(o.order_type),n(o.size),n(o.price)])).join('')||row(['—','','','','']);document.querySelector('#decisions').innerHTML=(x.recent_decisions||[]).map(d=>row([esc(d.created_at),esc(d.decision),esc(d.symbol||'—'),esc(d.execution_status||'—'),esc(d.reason||'')])).join('')||row(['—','','','','']);document.querySelector('#dot').style.background='#4ade80';document.querySelector('#live').textContent='Live';}catch(e){document.querySelector('#dot').style.background='#fb7185';document.querySelector('#live').textContent='Disconnected';}}
refresh();setInterval(refresh,5000);
</script></main></body></html>'''
