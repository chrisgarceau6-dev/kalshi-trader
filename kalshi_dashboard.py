#!/usr/bin/env python3
"""Kalshi trader dashboard — Render hosted.
Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY (base64 PEM), PORT (set by Render)
"""
import base64, os, time
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify

BASE = Path(__file__).parent

def _load_dotenv():
    f = BASE / ".env"
    if not f.exists(): return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, _, v = line.partition("=")
        k = k.strip(); v = v.strip().strip('"').strip("'")
        if k and k not in os.environ: os.environ[k] = v
_load_dotenv()

def _ensure_key():
    if os.environ.get("KALSHI_PRIVATE_KEY_PATH"): return
    raw = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
    if not raw: return
    p = Path("/tmp/kd_key.pem")
    if raw.startswith("-----"):
        # Raw PEM pasted directly — write as-is, restore newlines if stripped
        pem = raw.replace("\\n", "\n")
        p.write_text(pem + "\n"); p.chmod(0o600)
    else:
        b64 = raw.replace("\n","").replace("\r","").replace(" ","")
        b64 += "=" * (-len(b64) % 4)
        p.write_bytes(base64.b64decode(b64)); p.chmod(0o600)
    os.environ["KALSHI_PRIVATE_KEY_PATH"] = str(p)

try:
    from kalshi_auth import get as _raw; HAS_AUTH = True
except ImportError:
    HAS_AUTH = False; _raw = None

_last_err = {}
def kalshi(path, params=None):
    _ensure_key()
    if not HAS_AUTH:
        _last_err[path] = "kalshi_auth not importable"
        return None
    try:
        code, r = _raw(path, params)
        if code == 200: return r
        _last_err[path] = f"HTTP {code}: {str(r)[:120]}"
        return None
    except Exception as e:
        _last_err[path] = str(e)[:120]
        return None

_cache = {}
def cached(key, ttl, fn):
    now = time.time()
    if key in _cache and now - _cache[key]["t"] < ttl: return _cache[key]["v"]
    v = fn(); _cache[key] = {"t": now, "v": v}; return v

def get_balance():
    def _f():
        r = kalshi("/portfolio/balance")
        return float(r["balance_dollars"]) if r and "balance_dollars" in r else None
    return cached("bal", 30, _f)

def get_settlements():
    def _f():
        out, cursor, pages = [], None, 0
        while pages < 5:
            params = {"limit": 200}
            if cursor: params["cursor"] = cursor
            r = kalshi("/portfolio/settlements", params)
            if not r: break
            batch = r.get("settlements", [])
            if not batch: break
            pages += 1
            for s in batch:
                rev  = int(s.get("revenue", 0)) / 100.0
                yc   = float(s.get("yes_total_cost_dollars", 0) or 0)
                nc   = float(s.get("no_total_cost_dollars",  0) or 0)
                fee  = float(s.get("fee_cost", 0) or 0)
                side = "yes" if yc > 0.001 else ("no" if nc > 0.001 else "?")
                out.append({
                    "ticker": s.get("ticker", ""),
                    "series": s.get("ticker", "").split("-")[0],
                    "side":   side,
                    "pnl":    round(rev - yc - nc - fee, 2),
                    "won":    rev > 0.01,
                    "cost":   round(yc + nc, 2),
                    "rev":    round(rev, 2),
                    "fee":    round(fee, 2),
                    "ts":     s.get("settled_time", ""),
                })
            cursor = r.get("cursor")
            if not cursor: break
            time.sleep(0.05)
        out.reverse(); return out
    return cached("sett", 120, _f)

def get_market(ticker):
    r = kalshi(f"/markets/{ticker}")
    if not r: return {}
    m = r.get("market", r)
    return {
        "yes_ask":    m.get("yes_ask"),
        "yes_bid":    m.get("yes_bid"),
        "close_time": m.get("close_time", ""),
        "title":      m.get("subtitle", m.get("title", "")),
    }

def get_positions():
    def _f():
        r = kalshi("/portfolio/positions", {"settlement_status": "unsettled", "limit": 200})
        if not r: return []
        out = []
        for p in r.get("market_positions", []):
            if not p.get("ticker"): continue
            ticker = p.get("ticker", "")
            mkt = get_market(ticker)
            out.append({"ticker":     ticker,
                        "contracts":  p.get("position", p.get("resting_orders_count", 1)),
                        "yes_ask":    mkt.get("yes_ask"),
                        "yes_bid":    mkt.get("yes_bid"),
                        "close_time": mkt.get("close_time", ""),
                        "title":      mkt.get("title", "")})
        return out
    return cached("pos", 15, _f)

app = Flask(__name__)

@app.route("/api/data")
def api_data():
    return jsonify({
        "balance":     get_balance(),
        "settlements": get_settlements(),
        "positions":   get_positions(),
        "blackout":    [15, 17],
        "ts":          datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "errors":      dict(_last_err),
        "key_set":     bool(os.environ.get("KALSHI_PRIVATE_KEY_PATH") or os.environ.get("KALSHI_PRIVATE_KEY")),
        "key_id_set":  bool(os.environ.get("KALSHI_API_KEY_ID")),
    })

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kalshi</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#000;color:#fff;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:0 16px 40px}
#blackout{display:none;background:#f59e0b;color:#000;text-align:center;padding:10px 16px;font-size:13px;font-weight:700;margin:0 -16px;letter-spacing:.4px}
.hero{text-align:center;padding:28px 0 4px}
.hero-bal{font-size:52px;font-weight:700;letter-spacing:-1.5px;line-height:1}
.hero-chg{font-size:16px;margin-top:6px;font-weight:500}
.hero-ts{font-size:12px;color:#6b7280;margin-top:6px}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#22c55e;margin-right:5px;animation:pulse 2s infinite;vertical-align:middle}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.ranges{display:flex;justify-content:center;gap:2px;margin:20px 0 4px}
.ranges button{background:none;border:none;color:#6b7280;font-size:14px;font-weight:500;padding:7px 14px;border-radius:20px;cursor:pointer;font-family:inherit;transition:all .15s}
.ranges button.active{background:#1c1c1c;color:#fff}
.chart-wrap{position:relative;height:210px;margin:0 -4px 4px}
.divider{height:1px;background:#1c1c1c;margin:20px 0}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:#1c1c1c;border-radius:14px;overflow:hidden;margin:16px 0}
.stat{background:#111;padding:16px 14px}
.stat-lbl{font-size:11px;color:#6b7280;margin-bottom:6px;text-transform:uppercase;letter-spacing:.6px}
.stat-val{font-size:22px;font-weight:600;line-height:1}
h3{font-size:12px;color:#6b7280;text-transform:uppercase;letter-spacing:.8px;margin:22px 0 10px;font-weight:600}
.pos-row{display:flex;align-items:center;padding:14px 0;border-bottom:1px solid #111}
.pos-row:last-child{border-bottom:none}
.pos-ticker{font-size:14px;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pos-sub{font-size:12px;color:#6b7280;margin-top:2px}
.badge{font-size:11px;font-weight:800;padding:3px 9px;border-radius:12px;flex-shrink:0}
.badge-w{background:#14532d;color:#22c55e}
.badge-l{background:#450a0a;color:#ef4444}
.trade-row{display:flex;align-items:center;gap:10px;padding:12px 0;border-bottom:1px solid #111}
.trade-row:last-child{border-bottom:none}
.trade-series{font-size:13px;flex:1;color:#ccc}
.trade-pnl{font-size:14px;font-weight:600;margin-left:auto}
.g{color:#22c55e}.r{color:#ef4444}.m{color:#6b7280}
.empty{color:#6b7280;font-size:13px;padding:20px 0;text-align:center}
.pos-ask{font-size:16px;font-weight:700;color:#22c55e;text-align:right}
.pos-time{font-size:11px;color:#f59e0b;text-align:right;margin-top:2px;font-weight:600}
.pos-grid{width:100%;display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-top:12px;padding-top:12px;border-top:1px solid #1c1c1c}
.pos-grid-cell .lbl{font-size:10px;color:#6b7280;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.pos-grid-cell .val{font-size:15px;font-weight:600}
.pos-question{width:100%;font-size:12px;color:#9ca3af;margin-top:8px;line-height:1.45;font-style:italic}
.trade-detail{width:100%;padding:8px 0 2px;border-top:1px solid #1c1c1c;margin-top:8px}
.trade-grid{display:grid;grid-template-columns:1fr 1fr;gap:5px 20px;font-size:11px;color:#6b7280;margin-top:6px}
.trade-grid strong{font-weight:600}
</style>
</head>
<body>
<div id="blackout">BLACKOUT HOUR — strategy paused</div>
<div class="hero">
  <div class="hero-bal" id="bal">—</div>
  <div class="hero-chg" id="chg">—</div>
  <div class="hero-ts"><span class="dot"></span><span id="ts">loading...</span></div>
</div>
<div class="ranges">
  <button data-r="1H">1H</button>
  <button data-r="1D" class="active">Today</button>
  <button data-r="1W">1W</button>
  <button data-r="1M">1M</button>
  <button data-r="ALL">All</button>
</div>
<div class="chart-wrap"><canvas id="chart"></canvas></div>
<div class="stats">
  <div class="stat"><div class="stat-lbl">Today P&L</div><div class="stat-val" id="s-tpnl">—</div></div>
  <div class="stat"><div class="stat-lbl">Today WR</div><div class="stat-val" id="s-twr">—</div></div>
  <div class="stat"><div class="stat-lbl">Trades</div><div class="stat-val m" id="s-tn">—</div></div>
  <div class="stat"><div class="stat-lbl">Hour P&L</div><div class="stat-val" id="s-hpnl">—</div></div>
  <div class="stat"><div class="stat-lbl">Hour WR</div><div class="stat-val" id="s-hwr">—</div></div>
  <div class="stat"><div class="stat-lbl">Open</div><div class="stat-val" id="s-open">—</div></div>
</div>
<h3>Open Positions</h3>
<div id="positions"><div class="empty">No open positions</div></div>
<h3>Recent Trades</h3>
<div id="trades"><div class="empty">Loading...</div></div>
<script>
let chart=null, range='1D', last=null, expandedTrades=new Set();

const LABELS={'1H':'Last 1h','1D':'Today','1W':'Last 7d','1M':'Last 30d','ALL':'All time'};

function cutoff(r){
  const now=Date.now();
  if(r==='1H')return now-3600000;
  if(r==='1D'){const d=new Date();d.setUTCHours(0,0,0,0);return d.getTime();}
  if(r==='1W')return now-7*86400000;
  if(r==='1M')return now-30*86400000;
  return 0;
}

function fmt(n){
  if(n==null)return'—';
  return(n>=0?'+':'-')+'$'+Math.abs(n).toFixed(2);
}
function wr(wins,n){return n?((wins/n)*100).toFixed(1)+'%':'—';}
function cls(n){return n>0?'g':n<0?'r':'m';}

function timeLeft(closeTime){
  if(!closeTime)return'';
  const ms=new Date(closeTime).getTime()-Date.now();
  if(ms<=0)return'settling…';
  const s=Math.floor(ms/1000);
  if(s<60)return s+'s left';
  return Math.floor(s/60)+'m '+(s%60)+'s left';
}

function buildChart(labels,vals){
  const last=vals.length?vals[vals.length-1]:0;
  const color=last>=0?'#22c55e':'#ef4444';
  if(chart){
    chart.data.labels=labels;
    chart.data.datasets[0].data=vals;
    chart.data.datasets[0].borderColor=color;
    chart.data.datasets[0].backgroundColor=color+'1a';
    chart.update('none');return;
  }
  const ctx=document.getElementById('chart').getContext('2d');
  chart=new Chart(ctx,{
    type:'line',
    data:{labels,datasets:[{data:vals,borderColor:color,backgroundColor:color+'1a',
      borderWidth:2,pointRadius:0,pointHoverRadius:4,fill:true,tension:0.15}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},
        tooltip:{callbacks:{label:c=>'$'+c.parsed.y.toFixed(2)},
          displayColors:false,backgroundColor:'#1c1c1c',titleColor:'#6b7280',bodyColor:'#fff'}},
      scales:{
        x:{ticks:{color:'#6b7280',maxTicksLimit:6,font:{size:10},maxRotation:0},grid:{color:'#111'}},
        y:{ticks:{color:'#6b7280',callback:v=>'$'+v.toFixed(0),font:{size:10}},grid:{color:'#111'}}
      }}
  });
}

function render(d){
  const bal=d.balance;
  document.getElementById('bal').textContent=bal!=null?'$'+bal.toFixed(2):'—';

  const utcHr=new Date().getUTCHours();
  document.getElementById('blackout').style.display=
    (d.blackout||[]).includes(utcHr)?'block':'none';

  const now=new Date(d.ts+'Z');
  document.getElementById('ts').textContent=
    now.toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',
    timeZone:'UTC',hour12:false})+' UTC · 30s refresh';

  const sett=d.settlements||[];
  const cut=cutoff(range);
  const inRange=sett.filter(s=>new Date(s.ts).getTime()>=cut);

  // Chart — balance over time (reconstruct from current balance + full settlement history)
  const allPnl=sett.reduce((a,s)=>a+s.pnl,0);
  const balStart=(bal!=null?bal:0)-allPnl;
  let runBal=balStart;
  const balSeries=sett.map(s=>{runBal+=s.pnl;return{ts:s.ts,bal:runBal};});
  const inRangeBal=balSeries.filter(x=>new Date(x.ts).getTime()>=cut);
  const labels=[],vals=[];
  for(const x of inRangeBal){
    const dt=new Date(x.ts);
    let lbl;
    if(range==='1H'||range==='1D'){
      lbl=dt.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',timeZone:'UTC',hour12:false});
    } else {
      lbl=dt.toLocaleDateString([],{month:'short',day:'numeric',timeZone:'UTC'});
    }
    labels.push(lbl); vals.push(+x.bal.toFixed(2));
  }
  buildChart(labels,vals);

  // Hero change
  const rangePnl=inRange.reduce((a,s)=>a+s.pnl,0);
  const chgEl=document.getElementById('chg');
  chgEl.textContent=fmt(rangePnl)+' ('+LABELS[range]+')';
  chgEl.className='hero-chg '+(rangePnl>=0?'g':'r');

  // Today stats
  const todayCut=cutoff('1D');
  const ts=sett.filter(s=>new Date(s.ts).getTime()>=todayCut);
  const tpnl=ts.reduce((a,s)=>a+s.pnl,0);
  const twin=ts.filter(s=>s.won).length;
  set('s-tpnl',fmt(tpnl),cls(tpnl));
  set('s-twr',wr(twin,ts.length),'');
  set('s-tn',ts.length,'m');

  // Hour stats
  const hrCut=cutoff('1H');
  const hs=sett.filter(s=>new Date(s.ts).getTime()>=hrCut);
  const hpnl=hs.reduce((a,s)=>a+s.pnl,0);
  const hwin=hs.filter(s=>s.won).length;
  set('s-hpnl',fmt(hpnl),cls(hpnl));
  set('s-hwr',wr(hwin,hs.length),'');

  // Open positions
  const pos=d.positions||[];
  set('s-open',pos.length,pos.length>0?'g':'m');
  const posEl=document.getElementById('positions');
  if(pos.length){
    posEl.innerHTML=pos.map(p=>{
      const tl=timeLeft(p.close_time);
      const spread=p.yes_ask!=null&&p.yes_bid!=null?p.yes_ask-p.yes_bid:null;
      const payout=(p.contracts||0).toFixed(2);
      const estVal=p.yes_bid!=null?((p.contracts||0)*p.yes_bid/100).toFixed(2):null;
      const mktVal=p.yes_ask!=null?((p.contracts||0)*p.yes_ask/100).toFixed(2):null;
      return`<div class="pos-row" style="flex-wrap:wrap;align-items:flex-start">
        <div style="flex:1;min-width:0">
          <div class="pos-ticker">${p.ticker.split('-')[0]}</div>
          <div class="pos-sub" style="font-size:11px">${p.ticker}</div>
        </div>
        <div style="flex-shrink:0;text-align:right">
          <div class="pos-ask">${tl||'LIVE'}</div>
          <div class="pos-time">● LIVE</div>
        </div>
        ${p.title?`<div class="pos-question">${p.title}</div>`:''}
        <div class="pos-grid">
          <div class="pos-grid-cell"><div class="lbl">Contracts</div><div class="val">${p.contracts}</div></div>
          <div class="pos-grid-cell"><div class="lbl">Ask</div><div class="val">${p.yes_ask!=null?p.yes_ask+'¢':'—'}</div></div>
          <div class="pos-grid-cell"><div class="lbl">Bid</div><div class="val">${p.yes_bid!=null?p.yes_bid+'¢':'—'}</div></div>
          <div class="pos-grid-cell"><div class="lbl">Spread</div><div class="val">${spread!=null?spread+'¢':'—'}</div></div>
          <div class="pos-grid-cell"><div class="lbl">Win Payout</div><div class="val g">+$${payout}</div></div>
          <div class="pos-grid-cell"><div class="lbl">Mkt Value</div><div class="val">${mktVal?'~$'+mktVal:'—'}</div></div>
        </div>
      </div>`;
    }).join('');
  } else {
    posEl.innerHTML='<div class="empty">No open positions</div>';
  }

  // Recent trades (newest first, last 60)
  const recent=sett.slice(-60).reverse();
  const trEl=document.getElementById('trades');
  if(recent.length){
    trEl.innerHTML=recent.map(s=>{
      const key=s.ticker+'|'+s.ts;
      const exp=expandedTrades.has(key);
      const dt=new Date(s.ts);
      const timeStr=dt.toLocaleString([],{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'UTC',hour12:false})+' UTC';
      return`<div class="trade-row" data-key="${key}" style="cursor:pointer;flex-wrap:wrap">
        <span class="badge ${s.won?'badge-w':'badge-l'}">${s.won?'W':'L'}</span>
        <span class="trade-series">${s.series}</span>
        <span class="trade-pnl ${cls(s.pnl)}">${fmt(s.pnl)}</span>
        ${exp?`<div class="trade-detail">
          <span style="font-size:12px;color:#9ca3af">${s.ticker}</span>
          <div class="trade-grid">
            <span>Side: <strong style="color:#e5e7eb">${s.side.toUpperCase()}</strong></span>
            <span>Settled: <strong style="color:#e5e7eb">${timeStr}</strong></span>
            <span>Cost: <strong style="color:#e5e7eb">$${s.cost.toFixed(2)}</strong></span>
            <span>Gross payout: <strong class="g">$${s.rev!=null?s.rev.toFixed(2):'—'}</strong></span>
            <span>Fee: <strong class="r">${s.fee!=null?'-$'+s.fee.toFixed(2):'—'}</strong></span>
            <span>Net P&L: <strong class="${cls(s.pnl)}">${fmt(s.pnl)}</strong></span>
          </div>
        </div>`:''}
      </div>`;
    }).join('');
    trEl.querySelectorAll('.trade-row').forEach(row=>{
      row.addEventListener('click',()=>{
        const k=row.dataset.key;
        expandedTrades.has(k)?expandedTrades.delete(k):expandedTrades.add(k);
        if(last)render(last);
      });
    });
  } else {
    trEl.innerHTML='<div class="empty">No settled trades yet</div>';
  }
}

function set(id,val,colorCls){
  const el=document.getElementById(id);
  el.textContent=val;
  if(colorCls!==undefined) el.className='stat-val '+colorCls;
}

document.querySelectorAll('.ranges button').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('.ranges button').forEach(b=>b.className='');
    btn.className='active';
    range=btn.dataset.r;
    if(last)render(last);
  });
});

async function refresh(){
  try{
    const r=await fetch('/api/data');
    last=await r.json();
    render(last);
  }catch(e){console.error('refresh error',e);}
}

refresh();
setInterval(refresh,30000);
</script>
</body>
</html>"""

@app.route("/")
def index(): return HTML

if __name__ == "__main__":
    os.chdir(BASE)
    _ensure_key()
    port = int(os.environ.get("PORT", 8765))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print(f"Dashboard → http://{host}:{port}/")
    app.run(host=host, port=port, debug=False)
