#!/usr/bin/env python3
"""Flask server for arb dashboard.

usage:
    python arb_server.py            # starts on http://localhost:5001
"""
import json, csv, os
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request

BASE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(BASE, "kalshi_snapshot.json")
VEGAS = os.path.join(BASE, "vegas_snapshot.json")
LOG = os.path.join(BASE, "arb_log.csv")


def load_vegas():
    if not os.path.exists(VEGAS): return None
    try:
        with open(VEGAS) as f:
            return json.load(f)
    except:
        return None


def match_vegas_to_kalshi(kalshi_team, vegas_teams):
    """Fuzzy-match Kalshi's short team name to Vegas's full team name."""
    kt = kalshi_team.lower().strip()
    for vt in vegas_teams:
        vtl = vt.lower()
        # Kalshi often abbreviates: "New York Y" for Yankees, "Los Angeles A" for Angels
        # Simple heuristic: last word or common substring
        for word in kt.split():
            if len(word) >= 3 and word in vtl:
                return vt
    return None

app = Flask(__name__)

FEES = {
    "kalshi":    lambda p: 0.07 * p * (1-p),
    "prophet":   lambda p: 0.02 * p,
    "betopenly": lambda p: 0.02 * p,
    "rebet":     lambda p: 0.03 * p,
    "polymarket":lambda p: 0.02 * p * (1-p),
}


def american_to_prob(o):
    return -o/(-o+100) if o < 0 else 100/(o+100)


def compute_arb(pa, pb, cap, fa=0, fb=0):
    if pa<=0 or pb<=0 or pa>=1 or pb>=1: return None
    ratio = (1+pb)/(1+pa)
    n_a = cap/(pa + pb/ratio)
    n_b = n_a/ratio
    ca, cb = n_a*pa, n_b*pb
    faa, fbb = n_a*fa, n_b*fb
    total = ca+cb+faa+fbb
    py = n_a - total
    pn = n_b - total
    g = min(py, pn)
    return {'n_a':round(n_a,1),'n_b':round(n_b,1),'cost_a':round(ca,2),'cost_b':round(cb,2),
            'fee_a':round(faa,3),'fee_b':round(fbb,3),'total':round(total,2),
            'payoff_a':round(py,2),'payoff_b':round(pn,2),
            'guaranteed':round(g,2),'roc':round(g/total*100,2) if total>0 else 0,
            'sum':round(pa+pb,4),'edge_pct':round((1-pa-pb)*100,2)}


HTML = r"""
<!doctype html>
<html><head><title>Arb Dashboard</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,sans-serif;margin:0;padding:20px;}
h1{color:#58a6ff;margin:0 0 4px 0;font-size:22px;}
.stale{color:#f85149;} .fresh{color:#7ee787;}
.meta{color:#8b949e;font-size:13px;margin-bottom:20px;}
table{border-collapse:collapse;width:100%;background:#161b22;border-radius:8px;overflow:hidden;}
th,td{padding:10px 14px;text-align:left;border-bottom:1px solid #30363d;font-size:14px;}
th{background:#21262d;color:#58a6ff;position:sticky;top:0;}
tr:hover{background:#21262d;cursor:pointer;}
.spread-wide{color:#7ee787;font-weight:bold;}
.spread-med{color:#e3b341;}
.spread-tight{color:#8b949e;}
.sport{color:#79c0ff;font-weight:bold;font-size:12px;}
.close{color:#8b949e;font-size:12px;}
#calc{background:#161b22;border-radius:8px;padding:20px;margin-top:20px;display:none;}
#calc.show{display:block;}
label{display:block;color:#8b949e;font-size:12px;margin-top:12px;margin-bottom:4px;}
input,select{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:8px 12px;border-radius:6px;font-size:14px;width:200px;}
button{background:#238636;color:#fff;border:none;padding:10px 20px;border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;margin-top:16px;}
button:hover{background:#2ea043;}
button.secondary{background:#30363d;}
button.secondary:hover{background:#484f58;}
#result{margin-top:20px;padding:16px;border-radius:8px;font-family:'SF Mono',monospace;font-size:13px;line-height:1.5;}
.arb-yes{background:#0d3f1c;border:2px solid #238636;}
.arb-no{background:#3f0d0d;border:2px solid #f85149;}
.action{background:#21262d;padding:12px;border-radius:6px;margin-top:10px;}
.big{font-size:20px;font-weight:bold;}
.big-green{color:#7ee787;}
.big-red{color:#f85149;}
.log-summary{margin-top:20px;padding:16px;background:#161b22;border-radius:8px;font-size:13px;}
</style></head>
<body>
<h1>⚡ Multi-Venue Sports Arb</h1>
<div class="meta">Kalshi live · <span id="fetched">loading...</span> · <button class="secondary" onclick="load()">Refresh</button></div>
<div style="margin-bottom:16px;padding:12px;background:#161b22;border-radius:8px;display:flex;gap:12px;flex-wrap:wrap;align-items:center;">
  <div><span style="color:#8b949e;font-size:13px;margin-right:6px;">Sort:</span>
  <button id="sort-velocity" onclick="setSort('velocity')">⚡ Velocity</button>
  <button id="sort-soon" onclick="setSort('soon')" class="secondary">⏱ Ending Soonest</button>
  <button id="sort-spread" onclick="setSort('spread')" class="secondary">📏 Widest</button>
  <button id="sort-divergence" onclick="setSort('divergence')" class="secondary">🎯 Vegas Divergence</button></div>
  <div><label style="display:inline;font-size:13px;color:#8b949e;">
    <input type="checkbox" id="vegas-only" onchange="load()"> Only Vegas-flagged</label></div>
  <div><span style="color:#8b949e;font-size:13px;margin-right:6px;">Max close:</span>
  <select id="max-hrs" onchange="load()" style="width:auto;">
    <option value="">All</option>
    <option value="6">6h</option>
    <option value="12">12h</option>
    <option value="24" selected>24h</option>
    <option value="72">3d</option>
    <option value="168">1w</option>
  </select></div>
  <div><span style="color:#8b949e;font-size:13px;margin-right:6px;">Venue:</span>
  <select id="venue-sel" onchange="load()" style="width:auto;">
    <option value="prophet" selected>Prophet</option>
    <option value="betopenly">BetOpenly</option>
    <option value="rebet">Rebet</option>
    <option value="polymarket">Polymarket</option>
  </select></div>
  <div><label style="display:inline;font-size:13px;color:#8b949e;">
    <input type="checkbox" id="notify-toggle" onchange="toggleNotify()"> Desktop notifications</label></div>
</div>
<div id="log-summary"></div>
<div style="padding:10px 14px;background:#0d3f1c;border-radius:6px;margin-bottom:12px;font-size:13px;color:#7ee787;">
  <b>How to read the trigger prices:</b> For each team, "Arb if Prophet ≤ X or ≥ Y" tells you the range that produces a locked arb.
  Just glance at Prophet — if their moneyline is <b>outside</b> the trigger range, you have an arb. Click the row to compute exact sizing.
</div>
<table id="games-table">
  <thead><tr><th>#</th><th>Sport</th><th>Matchup</th><th>Kalshi (bid/ask)</th><th>Vegas</th><th>Arb Triggers</th><th>Closes</th><th>Vel</th></tr></thead>
  <tbody id="games-body"><tr><td colspan="8">Loading...</td></tr></tbody>
</table>

<div id="calc">
  <h2 style="color:#58a6ff;margin-top:0;">Check Arb</h2>
  <div id="calc-game" style="color:#8b949e;margin-bottom:12px;"></div>
  <label>Team (which side does your P2P bet win on?)</label>
  <select id="team-select"></select>
  <label>Other venue</label>
  <select id="venue-select">
    <option>prophet</option><option>betopenly</option><option>rebet</option><option>polymarket</option>
  </select>
  <label>Price format</label>
  <select id="price-type" onchange="togglePriceLabel()">
    <option value="american">American odds (e.g. -125 or +150)</option>
    <option value="probability">Implied probability (0-1)</option>
    <option value="decimal">Decimal odds (e.g. 1.80)</option>
  </select>
  <label id="price-label">Price</label>
  <input id="price-input" type="number" step="0.01" placeholder="-125">
  <label>Capital to deploy ($)</label>
  <input id="capital-input" type="number" step="10" value="200">
  <br><button onclick="calc()">Compute Arb</button>
  <button class="secondary" onclick="hideCalc()">Close</button>
  <div id="result"></div>
</div>

<script>
let currentGame = null;
let currentSort = 'velocity';

function setSort(s) {
  currentSort = s;
  ['velocity','soon','spread'].forEach(x => {
    document.getElementById('sort-'+x).className = x===s ? '' : 'secondary';
  });
  load();
}

function fmtHours(h) {
  if (h < 1) return Math.round(h*60) + 'm';
  if (h < 48) return h.toFixed(1) + 'h';
  return Math.round(h/24) + 'd';
}

function fmtAmerican(a) {
  if (a === null || a === undefined) return '?';
  return (a > 0 ? '+' : '') + a;
}

function load() {
  const maxHrs = document.getElementById('max-hrs').value;
  const venue = document.getElementById('venue-sel').value;
  const vegasOnly = document.getElementById('vegas-only').checked;
  const url = '/api/games?sort=' + currentSort + '&venue=' + venue +
              (maxHrs ? '&max_hours='+maxHrs : '') +
              (vegasOnly ? '&vegas_only=1' : '');
  fetch(url).then(r=>r.json()).then(d=>{
    const now = new Date();
    const fetched = new Date(d.fetched_at);
    const age = (now - fetched) / 1000 / 60;
    const vegasStatus = d.vegas_available
      ? `<span style="color:#79c0ff">· Vegas ${d.vegas_fetched_at ? '✓' : '?'}</span>`
      : `<span style="color:#8b949e">· No Vegas data (setup vegas_cron.py for auto-detection)</span>`;
    document.getElementById('fetched').innerHTML =
      `fetched ${age.toFixed(0)}m ago <span class="${age<10?'fresh':'stale'}">(${age<10?'fresh':'STALE'})</span> · ${d.games.length} games · vs <b>${venue}</b> ${vegasStatus}`;
    const body = document.getElementById('games-body');
    body.innerHTML = '';
    d.games.slice(0, 40).forEach((g, i) => {
      const tr = document.createElement('tr');
      tr.onclick = () => openCalc(g);
      if (g.vegas_flag) tr.style.borderLeft = '4px solid #7ee787';
      const velClass = g.velocity > 1 ? 'spread-wide' : g.velocity > 0.3 ? 'spread-med' : 'spread-tight';
      const hrsClass = g.hours_to_close < 6 ? 'spread-wide' : g.hours_to_close < 24 ? 'spread-med' : 'spread-tight';
      const matchup = g.sides.slice(0,2).map(s=>s.team).join(' vs ');
      const prices = g.sides.slice(0,2).map(s=>{
        const flag = s.live_book === false ? ' <span style="color:#e3b341" title="No live orderbook - not tradeable at these prices">⚠</span>' : '';
        return `<b>${s.team.substring(0,10)}</b>: ${s.yes_bid.toFixed(3)}/${s.yes_ask.toFixed(3)}${flag}`;
      }).join('<br>');
      const vegasCol = g.sides.slice(0,2).map(s=>{
        if (s.vegas_prob === undefined) return '<span style="color:#8b949e">-</span>';
        const divClass = Math.abs(s.vegas_div) > 0.03 ? 'spread-wide' : 'spread-tight';
        const sign = s.vegas_div > 0 ? '+' : '';
        return `<b>${s.team.substring(0,10)}</b>: ${s.vegas_prob.toFixed(3)} <span class="${divClass}">(${sign}${(s.vegas_div*100).toFixed(1)}c)</span>`;
      }).join('<br>');
      const triggers = g.sides.slice(0,2).map(s => {
        if (!s.triggers) return '';
        const t = s.triggers;
        // Highlight if Vegas suggests arb likely
        let flag = '';
        if (s.vegas_prob !== undefined) {
          if (s.vegas_prob < t.lo_prob) flag = ' <span style="color:#7ee787;font-weight:bold">◄ ARB LIKELY</span>';
          else if (s.vegas_prob > t.hi_prob) flag = ' <span style="color:#7ee787;font-weight:bold">► ARB LIKELY</span>';
        }
        return `<b>${s.team.substring(0,10)}</b>: <span style="color:#7ee787">${fmtAmerican(t.lo_american)}</span> or <span style="color:#7ee787">${fmtAmerican(t.hi_american)}</span>${flag}`;
      }).join('<br>');
      tr.innerHTML = `<td>${i+1}</td><td class="sport">${g.sport}</td><td>${matchup}</td>` +
                     `<td style="font-family:SF Mono,monospace;font-size:12px;">${prices}</td>` +
                     `<td style="font-family:SF Mono,monospace;font-size:12px;">${vegasCol}</td>` +
                     `<td style="font-family:SF Mono,monospace;font-size:12px;">${triggers}</td>` +
                     `<td class="${hrsClass}">${fmtHours(g.hours_to_close)}</td>` +
                     `<td class="${velClass}">${g.velocity.toFixed(2)}</td>`;
      body.appendChild(tr);
    });
    // desktop notification for high-velocity new opportunities
    if (window.notifyEnabled && d.games.length > 0) {
      const topVel = d.games[0].velocity;
      if (topVel > 1 && (!window.lastNotifiedVel || topVel > window.lastNotifiedVel * 1.2)) {
        window.lastNotifiedVel = topVel;
        new Notification('New high-velocity game!', {
          body: `${d.games[0].sport}: ${d.games[0].sides.map(s=>s.team).join(' vs ')} — velocity ${topVel.toFixed(2)}, closes in ${fmtHours(d.games[0].hours_to_close)}`
        });
      }
    }
  });
  fetch('/api/log-summary').then(r=>r.json()).then(d=>{
    if (!d.count) { document.getElementById('log-summary').innerHTML = ''; return; }
    document.getElementById('log-summary').innerHTML =
      `<div class="log-summary">📊 <b>Your log:</b> ${d.count} arbs · $${d.total_deployed.toFixed(2)} deployed · ` +
      `$${d.total_projected.toFixed(2)} projected profit · avg ROC ${d.avg_roc.toFixed(2)}%</div>`;
  });
}

function openCalc(game) {
  currentGame = game;
  document.getElementById('calc').classList.add('show');
  document.getElementById('calc-game').textContent = `${game.sport}: ${game.sides.map(s=>s.team).join(' vs ')}`;
  const sel = document.getElementById('team-select');
  sel.innerHTML = '';
  game.sides.forEach((s,i) => {
    const opt = document.createElement('option');
    opt.value = i;
    opt.textContent = `${s.team} (Kalshi yes: ${s.yes_bid.toFixed(3)}/${s.yes_ask.toFixed(3)})`;
    sel.appendChild(opt);
  });
  document.getElementById('result').innerHTML = '';
  window.scrollTo({top: document.getElementById('calc').offsetTop, behavior:'smooth'});
}

function hideCalc() { document.getElementById('calc').classList.remove('show'); }

function togglePriceLabel() {
  const t = document.getElementById('price-type').value;
  const labels = {american:'American odds (negative for favorite)', probability:'YES probability (0-1)', decimal:'Decimal odds'};
  document.getElementById('price-label').textContent = labels[t];
}

function calc() {
  const sideIdx = parseInt(document.getElementById('team-select').value);
  const side = currentGame.sides[sideIdx];
  const venue = document.getElementById('venue-select').value;
  const ptype = document.getElementById('price-type').value;
  const priceRaw = parseFloat(document.getElementById('price-input').value);
  const capital = parseFloat(document.getElementById('capital-input').value);

  fetch('/api/arb', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({kalshi_bid:side.yes_bid, kalshi_ask:side.yes_ask, team:side.team,
                          venue, price_type:ptype, price:priceRaw, capital,
                          event:currentGame.event, kalshi_ticker:side.ticker})})
    .then(r=>r.json()).then(showResult);
}

function showResult(r) {
  const div = document.getElementById('result');
  if (r.arb) {
    const b = r.best;
    const dir = r.direction;
    // Kalshi side details for auto-execute
    const kalshi_side = dir === 1 ? 'yes' : 'no';
    const kalshi_price_cents = dir === 1
      ? Math.round(r.kalshi_ask * 100)
      : Math.round((1 - r.kalshi_bid) * 100);
    // Need ticker — pass through from server
    let action = '';
    if (dir === 1) {
      action = `<div class="action"><b style="color:#7ee787;">EXECUTE (do both within 60 seconds):</b><br><br>` +
               `<b>1) KALSHI:</b> Buy <span class="big-green">${b.n_a.toFixed(0)} YES</span> on ${r.team} @ ~${r.kalshi_ask.toFixed(3)} → cost ~$${b.cost_a}<br>` +
               `<b>2) ${r.venue.toUpperCase()}:</b> Bet OPPOSITE team to win → stake ~$${b.cost_b}<br><br>` +
               `<span style="color:#8b949e;">Payoff if ${r.team} wins: +$${b.payoff_a} · If opposite wins: +$${b.payoff_b}</span></div>`;
    } else {
      action = `<div class="action"><b style="color:#7ee787;">EXECUTE (do both within 60 seconds):</b><br><br>` +
               `<b>1) KALSHI:</b> Buy <span class="big-green">${b.n_a.toFixed(0)} NO</span> on ${r.team} (= buy opposite team YES) → cost ~$${b.cost_a}<br>` +
               `<b>2) ${r.venue.toUpperCase()}:</b> Bet ${r.team} to win → stake ~$${b.cost_b}<br><br>` +
               `<span style="color:#8b949e;">Payoff if opposite wins: +$${b.payoff_a} · If ${r.team} wins: +$${b.payoff_b}</span></div>`;
    }
    div.className = 'arb-yes';
    const execBtn = r.kalshi_ticker
      ? `<button onclick='executeKalshi("${r.kalshi_ticker}", "${kalshi_side}", ${Math.round(b.n_a)}, ${kalshi_price_cents})' style="background:#238636;font-size:15px;">⚡ Auto-Execute Kalshi Side</button> `
      : '';
    div.innerHTML = `<div class="big big-green">✓ LOCKED ARB: $${b.guaranteed} on $${b.total} (${b.roc.toFixed(2)}% ROC)</div>` +
                    action + `<br>${execBtn}` +
                    `<button onclick='logArb(${JSON.stringify(r).replace(/'/g,"&#39;")})'>Log This Arb</button>` +
                    `<div id="exec-status" style="margin-top:10px;font-family:SF Mono,monospace;font-size:12px;"></div>`;
  } else {
    div.className = 'arb-no';
    const b1 = r.d1 || {}; const b2 = r.d2 || {};
    div.innerHTML = `<div class="big big-red">✗ NO ARB</div>` +
                    `<div class="action">Direction 1 (YES Kalshi + NO ${r.venue}): guaranteed <b>$${b1.guaranteed || 0}</b><br>` +
                    `Direction 2 (NO Kalshi + YES ${r.venue}): guaranteed <b>$${b2.guaranteed || 0}</b><br><br>` +
                    `<span style="color:#8b949e;">Fees + spread eat any edge. Try another game.</span></div>`;
  }
}

function logArb(r) {
  fetch('/api/log', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(r)})
    .then(r=>r.json()).then(_=>{ alert('Logged!'); load(); });
}

function executeKalshi(ticker, side, count, priceCents) {
  const status = document.getElementById('exec-status');
  status.innerHTML = '<span style="color:#e3b341;">Placing Kalshi order...</span>';
  fetch('/api/execute-kalshi', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ticker, side, count, price_cents: priceCents})})
    .then(r=>r.json()).then(r=>{
      if (r.ok) {
        status.innerHTML = `<span style="color:#7ee787;">✓ Kalshi order placed!</span> ${JSON.stringify(r.resp).substring(0,200)}<br><b style="color:#e3b341;">NOW place the Prophet/venue side manually — you have seconds!</b>`;
      } else {
        status.innerHTML = `<span style="color:#f85149;">✗ Failed: ${r.error || JSON.stringify(r.resp)}</span>`;
      }
    });
}

function toggleNotify() {
  if (document.getElementById('notify-toggle').checked) {
    Notification.requestPermission().then(p => {
      window.notifyEnabled = (p === 'granted');
      if (window.notifyEnabled) new Notification('Arb dashboard notifications enabled');
    });
  } else {
    window.notifyEnabled = false;
  }
}

load();
setInterval(load, 30000);
</script>
</body></html>
"""


@app.route('/')
def index():
    return render_template_string(HTML)


def american_from_prob(p):
    if p <= 0 or p >= 1: return None
    return int(round(-p * 100 / (1-p))) if p >= 0.5 else int(round((1-p) * 100 / p))


def arb_triggers(kalshi_bid, kalshi_ask, venue='prophet'):
    """For each team, compute the venue-YES price ranges that trigger a locked arb.
    Returns two thresholds:
      lo_trigger — if venue_yes < lo, buy NO on Kalshi + YES on venue for arb
      hi_trigger — if venue_yes > hi, buy YES on Kalshi + NO on venue for arb
    """
    kalshi_fee = lambda p: 0.07 * p * (1-p)
    venue_fees = {'prophet': 0.02, 'betopenly': 0.02, 'rebet': 0.03, 'polymarket': 0.02}
    vf = venue_fees.get(venue, 0.02)

    # Direction 1: Buy YES Kalshi at ask, Buy NO venue at (1 - venue_yes)
    # Need: ask + (1 - venue_yes) + kalshi_fee(ask) + vf*(1-venue_yes) < 1
    # venue_yes > ask + kalshi_fee(ask) + vf - vf*venue_yes
    # venue_yes * (1 + vf) > ask + kalshi_fee(ask) + vf
    hi_trigger = (kalshi_ask + kalshi_fee(kalshi_ask) + vf) / (1 + vf)

    # Direction 2: Buy NO Kalshi at (1-bid), Buy YES venue at venue_yes
    # Need: (1 - bid) + venue_yes + kalshi_fee(1-bid) + vf*venue_yes < 1
    # venue_yes * (1 + vf) < bid - kalshi_fee(1-bid)
    lo_trigger = (kalshi_bid - kalshi_fee(1-kalshi_bid)) / (1 + vf)

    return {
        'lo_prob': round(lo_trigger, 3),
        'hi_prob': round(hi_trigger, 3),
        'lo_american': american_from_prob(lo_trigger),
        'hi_american': american_from_prob(hi_trigger),
    }


@app.route('/api/games')
def games():
    if not os.path.exists(SNAPSHOT):
        return jsonify({'fetched_at': '1970-01-01T00:00:00', 'games': []})
    with open(SNAPSHOT) as f:
        d = json.load(f)
    venue = request.args.get('venue', 'prophet')
    vegas = load_vegas()
    now = datetime.now()
    for g in d.get('games', []):
        try:
            close_dt = datetime.fromisoformat(g['close'].replace('Z', '+00:00').replace('+00:00',''))
            hrs = max(0.1, (close_dt - now).total_seconds() / 3600)
        except:
            hrs = 999
        g['hours_to_close'] = round(hrs, 1)
        g['velocity'] = round(g['avg_spread'] / hrs * 100, 3)
        # compute triggers for each side
        for s in g['sides']:
            if s.get('yes_bid') is not None and s.get('yes_ask') is not None:
                s['triggers'] = arb_triggers(s['yes_bid'], s['yes_ask'], venue)
        # attach Vegas consensus if available
        g['vegas_divergence'] = None
        g['vegas_flag'] = False
        if vegas and vegas.get('sports', {}).get(g['sport']):
            for vg in vegas['sports'][g['sport']]:
                vegas_teams = list(vg.get('consensus', {}).keys())
                matches = 0
                divs = []
                for s in g['sides']:
                    vt = match_vegas_to_kalshi(s['team'], vegas_teams)
                    if vt and vg['consensus'].get(vt) is not None:
                        kalshi_mid = (s['yes_bid'] + s['yes_ask']) / 2
                        vegas_prob = vg['consensus'][vt]
                        div = vegas_prob - kalshi_mid
                        s['vegas_prob'] = round(vegas_prob, 3)
                        s['vegas_div'] = round(div, 3)
                        divs.append(abs(div))
                        matches += 1
                if matches == len(g['sides']):
                    max_div = max(divs) if divs else 0
                    g['vegas_divergence'] = round(max_div, 3)
                    # flag if Vegas disagrees with Kalshi by more than typical fees (~3c)
                    g['vegas_flag'] = max_div > 0.03
                    break
    sort = request.args.get('sort', 'velocity')
    max_hrs = request.args.get('max_hours', type=float, default=None)
    vegas_only = request.args.get('vegas_only') == '1'
    if max_hrs:
        d['games'] = [g for g in d['games'] if g['hours_to_close'] <= max_hrs]
    if vegas_only:
        d['games'] = [g for g in d['games'] if g.get('vegas_flag')]
    if sort == 'velocity':
        d['games'].sort(key=lambda g: -g['velocity'])
    elif sort == 'soon':
        d['games'].sort(key=lambda g: g['hours_to_close'])
    elif sort == 'spread':
        d['games'].sort(key=lambda g: -g['avg_spread'])
    elif sort == 'divergence':
        d['games'].sort(key=lambda g: -(g.get('vegas_divergence') or 0))
    d['vegas_available'] = vegas is not None
    d['vegas_fetched_at'] = vegas.get('fetched_at') if vegas else None
    return jsonify(d)


@app.route('/api/arb', methods=['POST'])
def arb():
    d = request.json
    ptype = d.get('price_type','american')
    price = d.get('price', 0)
    if ptype == 'american':
        venue_prob = american_to_prob(int(price))
    elif ptype == 'decimal':
        venue_prob = 1/price if price > 0 else 0
    else:
        venue_prob = price

    venue = d.get('venue','prophet')
    kb, ka = d.get('kalshi_bid'), d.get('kalshi_ask')
    cap = d.get('capital', 200)
    fee_v = FEES.get(venue, FEES['prophet'])
    fee_k = FEES['kalshi']

    d1 = compute_arb(ka, 1-venue_prob, cap, fa=fee_k(ka), fb=fee_v(1-venue_prob))
    d2 = compute_arb(1-kb, venue_prob, cap, fa=fee_k(1-kb), fb=fee_v(venue_prob))

    best, direction = None, None
    if d1 and d1['guaranteed'] > 0: best, direction = d1, 1
    if d2 and d2['guaranteed'] > 0 and (not best or d2['guaranteed'] > best['guaranteed']):
        best, direction = d2, 2

    return jsonify({'arb': best is not None, 'best': best, 'direction': direction,
                    'd1': d1, 'd2': d2, 'kalshi_bid': kb, 'kalshi_ask': ka,
                    'team': d.get('team',''), 'venue': venue, 'event': d.get('event',''),
                    'kalshi_ticker': d.get('kalshi_ticker','')})


@app.route('/api/log', methods=['POST'])
def log():
    d = request.json
    b = d.get('best',{})
    new = not os.path.exists(LOG)
    with open(LOG, 'a', newline='') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['timestamp','event','team','venue','direction','n_kalshi','n_venue',
                        'cost_kalshi','cost_venue','total','projected_profit','roc_pct'])
        w.writerow([datetime.now().isoformat(timespec='seconds'), d.get('event',''), d.get('team',''),
                    d.get('venue',''), d.get('direction',0), b.get('n_a'), b.get('n_b'),
                    b.get('cost_a'), b.get('cost_b'), b.get('total'), b.get('guaranteed'),
                    b.get('roc')])
    return jsonify({'ok': True})


@app.route('/api/execute-kalshi', methods=['POST'])
def execute_kalshi():
    """One-click Kalshi execution. Places a limit order on the specified side."""
    try:
        from kalshi_auth import place_order, get_balance
    except ImportError as e:
        return jsonify({'ok': False, 'error': f'kalshi_auth import failed: {e}'})

    d = request.json
    ticker = d.get('ticker')
    side = d.get('side')  # 'yes' or 'no'
    count = int(d.get('count', 0))
    price_cents = int(d.get('price_cents', 0))

    if not ticker or side not in ('yes','no') or count <= 0 or not (1 <= price_cents <= 99):
        return jsonify({'ok': False, 'error': 'invalid params'})

    try:
        if side == 'yes':
            code, resp = place_order(ticker, 'yes', count, yes_price_cents=price_cents, action='buy')
        else:
            code, resp = place_order(ticker, 'no', count, no_price_cents=price_cents, action='buy')
        return jsonify({'ok': code == 201 or code == 200, 'http': code, 'resp': resp})
    except FileNotFoundError as e:
        return jsonify({'ok': False, 'error': 'Kalshi API not set up. Run: python kalshi_auth.py setup'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/kalshi-balance')
def kalshi_balance():
    try:
        from kalshi_auth import get_balance
        code, j = get_balance()
        return jsonify({'ok': code == 200, 'balance': j})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/log-summary')
def log_summary():
    if not os.path.exists(LOG):
        return jsonify({'count':0,'total_deployed':0,'total_projected':0,'avg_roc':0})
    with open(LOG) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return jsonify({'count':0,'total_deployed':0,'total_projected':0,'avg_roc':0})
    td = sum(float(r['total']) for r in rows)
    tp = sum(float(r['projected_profit']) for r in rows)
    return jsonify({'count': len(rows), 'total_deployed': td, 'total_projected': tp,
                    'avg_roc': tp/td*100 if td > 0 else 0})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=5001, debug=False)
