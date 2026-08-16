#!/usr/bin/env python3
"""Kalshi trader dashboard — Render hosted.
Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY (base64 PEM), PORT (set by Render)
"""
import base64, os, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
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
        while pages < 20:
            params = {"limit": 200}
            if cursor: params["cursor"] = cursor
            r = kalshi("/portfolio/settlements", params)
            if not r: break
            batch = r.get("settlements", [])
            if not batch: break
            pages += 1
            for s in batch:
                if not s.get("ticker", "").split("-")[0].endswith("15M"):
                    continue
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

def get_deposits():
    def _f():
        out, cursor, pages = [], None, 0
        while pages < 10:
            params = {"limit": 100}
            if cursor: params["cursor"] = cursor
            r = kalshi("/portfolio/deposits", params)
            if not r: break
            batch = r.get("deposits", [])
            if not batch: break
            pages += 1
            for d in batch:
                if d.get("status") != "applied": continue
                net = (int(d.get("amount_cents", 0)) - int(d.get("fee_cents", 0))) / 100.0
                ts_unix = int(d.get("finalized_ts", d.get("created_ts", 0)))
                if not ts_unix: continue
                dt = datetime.fromtimestamp(ts_unix, tz=ET)
                out.append({"ts": dt.isoformat(timespec="seconds"), "amount": round(net, 2)})
            cursor = r.get("cursor")
            if not cursor: break
        out.sort(key=lambda x: x["ts"])
        return out
    return cached("deps", 300, _f)

def get_market(ticker):
    r = kalshi(f"/markets/{ticker}")
    if not r: return {}
    m = r.get("market", r)
    def _cents(key):
        try:
            v = round(float(m.get(key, 0) or 0) * 100)
            return v if v > 0 else None
        except (TypeError, ValueError):
            return None
    return {
        "yes_ask":    _cents("yes_ask_dollars"),
        "yes_bid":    _cents("yes_bid_dollars"),
        "close_time": m.get("close_time", ""),
        "title":      m.get("subtitle", m.get("title", "")),
    }

def get_fills_contracts(ticker):
    """Sum count_fp across all fills for this ticker to get actual contracts held."""
    r = kalshi("/portfolio/fills", {"ticker": ticker, "limit": 50})
    if not r: return 0
    return int(sum(float(f.get("count_fp", 0) or 0) for f in r.get("fills", [])))

def get_positions():
    def _f():
        r = kalshi("/portfolio/positions", {"settlement_status": "unsettled", "limit": 200})
        if not r: return []
        out = []
        for p in r.get("market_positions", []):
            if not p.get("ticker"): continue
            ticker = p.get("ticker", "")
            mkt = get_market(ticker)
            contracts = get_fills_contracts(ticker) or int(p.get("position") or 0)
            out.append({"ticker":     ticker,
                        "contracts":  contracts,
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
        "deposits":    get_deposits(),
        "positions":   get_positions(),
        "blackout":    [13],
        "ts":          datetime.now(ET).isoformat(timespec="seconds"),
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
.chart-toggle{display:flex;justify-content:flex-end;gap:2px;margin:6px 0 0}
.chart-toggle button{background:none;border:1px solid #2a2a2a;color:#6b7280;font-size:11px;font-weight:600;padding:3px 10px;border-radius:10px;cursor:pointer;font-family:inherit;letter-spacing:.3px;transition:all .15s}
.chart-toggle button.active{background:#1c1c1c;color:#fff;border-color:#444}
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
<div class="chart-toggle">
  <button id="ct-pnl" class="active" data-m="pnl">P&amp;L</button>
  <button id="ct-bal" data-m="bal">Balance</button>
</div>
<div class="chart-wrap"><canvas id="chart"></canvas></div>
<div class="stats">
  <div class="stat"><div class="stat-lbl" id="lbl-rpnl">P&L</div><div class="stat-val" id="s-rpnl">—</div></div>
  <div class="stat"><div class="stat-lbl" id="lbl-rwr">WR</div><div class="stat-val" id="s-rwr">—</div></div>
  <div class="stat"><div class="stat-lbl" id="lbl-rn">Trades</div><div class="stat-val m" id="s-rn">—</div></div>
  <div class="stat"><div class="stat-lbl" id="lbl-hpnl">Hour P&L</div><div class="stat-val" id="s-hpnl">—</div></div>
  <div class="stat"><div class="stat-lbl" id="lbl-hwr">Hour WR</div><div class="stat-val" id="s-hwr">—</div></div>
  <div class="stat"><div class="stat-lbl">Open</div><div class="stat-val" id="s-open">—</div></div>
</div>
<h3>Open Positions</h3>
<div id="positions"><div class="empty">No open positions</div></div>
<h3>Recent Trades</h3>
<div id="trades"><div class="empty">Loading...</div></div>
<script>
let chart=null, range='1D', chartMode='pnl', last=null, expandedTrades=new Set();

const LABELS={'1H':'Last 1h','1D':'Today','1W':'Last 7d','1M':'Last 30d','ALL':'Since Aug 1'};

const AUG1=new Date('2026-08-01T04:00:00Z').getTime();
function cutoff(r){
  const now=new Date();
  let t;
  if(r==='1H')t=now.getTime()-3600000;
  else if(r==='1D'){
    const s=now.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'});
    const [h,m,sc]=s.split(':').map(Number);
    t=now.getTime()-(((h%24)*3600+m*60+sc)*1000);
  }
  else if(r==='1W')t=now.getTime()-7*86400000;
  else if(r==='1M')t=now.getTime()-30*86400000;
  else t=AUG1;
  return Math.max(t,AUG1);
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

function buildChart(labels,vals,mode){
  const last=vals.length?vals[vals.length-1]:0;
  const color=mode==='bal'?'#3b82f6':(last>=0?'#22c55e':'#ef4444');
  if(chart){
    chart.data.labels=labels;
    chart.data.datasets[0].data=vals;
    chart.data.datasets[0].borderColor=color;
    chart.data.datasets[0].backgroundColor=color+'1a';
    chart.options.scales.y.ticks.callback=v=>'$'+v.toFixed(0);
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

  const etHr=parseInt(new Date().toLocaleString('en-US',{timeZone:'America/New_York',hour:'numeric',hour12:false}))||0;
  document.getElementById('blackout').style.display=
    (d.blackout||[]).includes(etHr)?'block':'none';

  const now=new Date(d.ts);
  document.getElementById('ts').textContent=
    now.toLocaleString([],{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',
    timeZone:'America/New_York',hour12:true})+' ET · 30s refresh';

  const sett=d.settlements||[];
  const cut=cutoff(range);
  const inRange=sett.filter(s=>new Date(s.ts).getTime()>=cut);

  // Build cumulative P&L series (relative, starts at 0 for range)
  let runPnl=0;
  const pnlSeries=sett.map(s=>{runPnl+=s.pnl;return{ts:s.ts,pnl:runPnl};});

  // Build absolute balance series using real deposit events + trade P&L
  // Merge deposits and settlements into one timeline, sorted by timestamp
  const deps=(d.deposits||[]).map(x=>({ts:x.ts,dep:x.amount,pnl:0}));
  const trades=sett.map(x=>({ts:x.ts,dep:0,pnl:x.pnl}));
  const combined=[...deps,...trades].sort((a,b)=>a.ts<b.ts?-1:1);
  let runBal=0;
  const balSeries=combined.map(ev=>{runBal+=ev.dep+ev.pnl;return{ts:ev.ts,bal:+runBal.toFixed(2)};});
  // Scale so the final point matches current live balance (accounts for open positions, rounding)
  const liveBal=d.balance||0;
  const computedFinal=balSeries.length?balSeries[balSeries.length-1].bal:liveBal;
  const drift=+(liveBal-computedFinal).toFixed(2);
  balSeries.forEach(x=>x.bal=+(x.bal+drift).toFixed(2));

  const labels=[],vals=[];
  if(chartMode==='pnl'){
    const preRange=pnlSeries.filter(x=>new Date(x.ts).getTime()<cut);
    const baseline=preRange.length?preRange[preRange.length-1].pnl:0;
    const inRange=pnlSeries.filter(x=>new Date(x.ts).getTime()>=cut);
    for(const x of inRange){
      const dt=new Date(x.ts);
      labels.push(range==='1H'||range==='1D'
        ?dt.toLocaleTimeString([],{hour:'numeric',minute:'2-digit',timeZone:'America/New_York',hour12:true})
        :dt.toLocaleDateString([],{month:'short',day:'numeric',timeZone:'America/New_York'}));
      vals.push(+(x.pnl-baseline).toFixed(2));
    }
  } else {
    const inRange=balSeries.filter(x=>new Date(x.ts).getTime()>=cut);
    for(const x of inRange){
      const dt=new Date(x.ts);
      labels.push(range==='1H'||range==='1D'
        ?dt.toLocaleTimeString([],{hour:'numeric',minute:'2-digit',timeZone:'America/New_York',hour12:true})
        :dt.toLocaleDateString([],{month:'short',day:'numeric',timeZone:'America/New_York'}));
      vals.push(x.bal);
    }
  }
  buildChart(labels,vals,chartMode);

  // Hero change
  const rangePnl=inRange.reduce((a,s)=>a+s.pnl,0);
  const chgEl=document.getElementById('chg');
  chgEl.textContent=fmt(rangePnl)+' ('+LABELS[range]+')';
  chgEl.className='hero-chg '+(rangePnl>=0?'g':'r');

  // Range stats (top row — tracks selected range button)
  const rlbl={'1H':'Hour','1D':'Today','1W':'Week','1M':'Month','ALL':'Since Aug 1'};
  const rpnl=inRange.reduce((a,s)=>a+s.pnl,0);
  const rwin=inRange.filter(s=>s.won).length;
  document.getElementById('lbl-rpnl').textContent=rlbl[range]+' P&L';
  document.getElementById('lbl-rwr').textContent=rlbl[range]+' WR';
  document.getElementById('lbl-rn').textContent=rlbl[range]+' Trades';
  set('s-rpnl',fmt(rpnl),cls(rpnl));
  set('s-rwr',wr(rwin,inRange.length),'');
  set('s-rn',inRange.length,'m');

  // Bottom stats row — shows Hour stats normally; swaps to Today when range is already 1H
  const hrCut=cutoff('1H');
  const hs=sett.filter(s=>new Date(s.ts).getTime()>=hrCut);
  const hpnl=hs.reduce((a,s)=>a+s.pnl,0);
  const hwin=hs.filter(s=>s.won).length;
  if(range==='1H'){
    const dayCut=cutoff('1D');
    const ds=sett.filter(s=>new Date(s.ts).getTime()>=dayCut);
    const dpnl=ds.reduce((a,s)=>a+s.pnl,0);
    const dwin=ds.filter(s=>s.won).length;
    document.getElementById('lbl-hpnl').textContent='Today P&L';
    document.getElementById('lbl-hwr').textContent='Today WR';
    set('s-hpnl',fmt(dpnl),cls(dpnl));
    set('s-hwr',wr(dwin,ds.length),'');
  } else {
    document.getElementById('lbl-hpnl').textContent='Hour P&L';
    document.getElementById('lbl-hwr').textContent='Hour WR';
    set('s-hpnl',fmt(hpnl),cls(hpnl));
    set('s-hwr',wr(hwin,hs.length),'');
  }

  // Open positions
  const pos=d.positions||[];
  set('s-open',pos.length,pos.length>0?'g':'m');
  const posEl=document.getElementById('positions');
  if(pos.length){
    posEl.innerHTML=pos.map(p=>{
      const tl=timeLeft(p.close_time);
      const msLeft=p.close_time?new Date(p.close_time).getTime()-Date.now():Infinity;
      const nearExpiry=msLeft<120000&&msLeft>0;
      const settling=msLeft<=0;
      const spread=p.yes_ask!=null&&p.yes_bid!=null?p.yes_ask-p.yes_bid:null;
      const cts=p.contracts||0;
      const payout=cts.toFixed(2);
      const askDisp=p.yes_ask!=null?p.yes_ask+'¢':(nearExpiry||settling?'<span style="color:#f59e0b">Locked</span>':'—');
      const bidDisp=p.yes_bid!=null?p.yes_bid+'¢':(nearExpiry||settling?'<span style="color:#f59e0b">Locked</span>':'—');
      const spreadDisp=spread!=null?spread+'¢':'—';
      const mktValDisp=p.yes_bid!=null?'~$'+(cts*p.yes_bid/100).toFixed(2):(nearExpiry||settling?`<span style="color:#f59e0b">~$${payout}</span>`:'—');
      return`<div class="pos-row" style="flex-wrap:wrap;align-items:flex-start">
        <div style="flex:1;min-width:0">
          <div class="pos-ticker">${p.ticker.split('-')[0]}</div>
          <div class="pos-sub" style="font-size:11px">${p.ticker}</div>
        </div>
        <div style="flex-shrink:0;text-align:right">
          <div class="pos-ask">${settling?'Settling…':tl||'LIVE'}</div>
          <div class="pos-time">● LIVE</div>
        </div>
        ${p.title?`<div class="pos-question">${p.title}</div>`:''}
        <div class="pos-grid">
          <div class="pos-grid-cell"><div class="lbl">Contracts</div><div class="val">${cts||'—'}</div></div>
          <div class="pos-grid-cell"><div class="lbl">Ask</div><div class="val">${askDisp}</div></div>
          <div class="pos-grid-cell"><div class="lbl">Bid</div><div class="val">${bidDisp}</div></div>
          <div class="pos-grid-cell"><div class="lbl">Spread</div><div class="val">${spreadDisp}</div></div>
          <div class="pos-grid-cell"><div class="lbl">Win Payout</div><div class="val g">+$${payout}</div></div>
          <div class="pos-grid-cell"><div class="lbl">Mkt Value</div><div class="val">${mktValDisp}</div></div>
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
      const timeStr=dt.toLocaleString([],{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',timeZone:'America/New_York',hour12:true})+' ET';
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

document.querySelectorAll('.chart-toggle button').forEach(btn=>{
  btn.addEventListener('click',()=>{
    document.querySelectorAll('.chart-toggle button').forEach(b=>b.className='');
    btn.className='active';
    chartMode=btn.dataset.m;
    if(chart){chart.destroy();chart=null;}
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
