#!/usr/bin/env python3
"""Kalshi trader dashboard — Render hosted.
Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY (base64 PEM), PORT (set by Render)
"""
import ast, base64, hmac, os, time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
from pathlib import Path
import requests
from flask import Flask, jsonify, request, make_response, redirect

BASE = Path(__file__).parent
TRADER = BASE / "late_certainty_trader.py"

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

# The UI floors every range at Aug 1, so history older than this is fetched, parsed
# and then discarded. Keep a week of slack so the pre-range P&L baseline still has
# something to anchor to.
SETTLEMENT_FLOOR = "2026-07-25"

def get_settlements():
    def _f():
        # The date floor below is what should end pagination. This cap only exists so
        # a cursor bug cannot loop forever — at 20 it would have silently truncated
        # the oldest history once volume passed ~150 settlements/day.
        out, cursor, pages = [], None, 0
        while pages < 60:
            params = {"limit": 200}
            if cursor: params["cursor"] = cursor
            r = kalshi("/portfolio/settlements", params)
            if not r: break
            batch = r.get("settlements", [])
            if not batch: break
            pages += 1
            for s in batch:
                # Non-15M rows are NOT skipped: they moved the account balance, so
                # dropping them made the reconstructed balance curve wrong by their
                # total. They are flagged instead, and the UI keeps them out of the
                # strategy stats while still counting them in the balance line.
                is_strat = s.get("ticker", "").split("-")[0].endswith("15M")
                rev  = int(s.get("revenue", 0)) / 100.0
                yc   = float(s.get("yes_total_cost_dollars", 0) or 0)
                nc   = float(s.get("no_total_cost_dollars",  0) or 0)
                fee  = float(s.get("fee_cost", 0) or 0)
                side = "yes" if yc > 0.001 else ("no" if nc > 0.001 else "?")
                out.append({
                    "ticker": s.get("ticker", ""),
                    "series": s.get("ticker", "").split("-")[0],
                    "strat":  is_strat,
                    "side":   side,
                    "pnl":    round(rev - yc - nc - fee, 2),
                    "won":    rev > 0.01,
                    "cost":   round(yc + nc, 2),
                    "rev":    round(rev, 2),
                    "fee":    round(fee, 2),
                    "ts":     s.get("settled_time", ""),
                })
            # Settlements come newest-first; once a page predates the floor every
            # later page does too. Stops ~9 API calls per refresh from being spent
            # on rows the UI throws away, on the same key the trader polls with.
            oldest = min((x.get("settled_time", "") for x in batch if x.get("settled_time")),
                         default="")
            if oldest and oldest[:10] < SETTLEMENT_FLOOR:
                break
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
        "no_ask":     _cents("no_ask_dollars"),
        "no_bid":     _cents("no_bid_dollars"),
        "close_time": m.get("close_time", ""),
        "title":      m.get("subtitle", m.get("title", "")),
    }

def get_fills_basis(ticker):
    """Contracts, side and true cost basis for a ticker, from its fills.

    The positions endpoint does not carry an entry price, so without this the UI can
    only show contract counts — which is why "if win" previously displayed gross
    settlement value (contracts x $1) instead of actual profit, overstating the
    upside by more than 10x on a strategy that risks ~$75 to win ~$6.50.

    Same single API call the contract count already required.
    """
    r = kalshi("/portfolio/fills", {"ticker": ticker, "limit": 50})
    if not r:
        return {"contracts": 0, "side": None, "entry": None, "cost": 0.0, "fee": 0.0}
    n = 0.0; notional = 0.0; fee = 0.0; side = None
    for f in r.get("fills", []):
        c = float(f.get("count_fp", 0) or 0)
        if c <= 0:
            continue
        sd = (f.get("side") or "").lower()
        px = f.get("no_price_dollars") if sd == "no" else f.get("yes_price_dollars")
        try:
            px = float(px)
        except (TypeError, ValueError):
            continue
        signed = -c if (f.get("action") or "").lower() == "sell" else c
        n += signed
        notional += signed * px
        fee += float(f.get("fee_cost", 0) or 0)
        side = side or sd
    entry = (notional / n) if n else None
    return {"contracts": int(n), "side": side, "entry": entry,
            "cost": round(notional, 2), "fee": round(fee, 2)}

def get_positions():
    def _f():
        r = kalshi("/portfolio/positions", {"settlement_status": "unsettled", "limit": 200})
        if not r: return []
        out = []
        for p in r.get("market_positions", []):
            if not p.get("ticker"): continue
            ticker = p.get("ticker", "")
            mkt = get_market(ticker)
            b = get_fills_basis(ticker)
            contracts = b["contracts"] or int(p.get("position") or 0)
            side = b["side"] or "yes"
            # Quote the side actually held. Showing the YES book for a NO position
            # displays ~9c against a 91c entry, and v5.16 trades NO about half the time.
            ask = mkt.get("no_ask") if side == "no" else mkt.get("yes_ask")
            bid = mkt.get("no_bid") if side == "no" else mkt.get("yes_bid")
            out.append({"ticker":     ticker,
                        "contracts":  contracts,
                        "side":       side,
                        "entry":      round(b["entry"] * 100, 1) if b["entry"] else None,
                        "cost":       b["cost"],
                        "fee":        b["fee"],
                        "ask":        ask,
                        "bid":        bid,
                        "close_time": mkt.get("close_time", ""),
                        "title":      mkt.get("title", "")})
        return out
    return cached("pos", 15, _f)

def live_blackout_hours():
    """Read BLACKOUT_HOURS out of the trader by AST — never imports it.

    Same technique as scripts/backtest.py, so the banner cannot drift from what is
    actually running. Returns None if the constant cannot be read; the UI then shows
    no banner rather than asserting a pause it has not verified.
    """
    def _f():
        try:
            tree = ast.parse(TRADER.read_text())
        except Exception as e:
            _last_err["blackout"] = f"parse: {str(e)[:80]}"
            return None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if not (isinstance(t, ast.Name) and t.id == "BLACKOUT_HOURS"):
                    continue
                v = node.value
                # `set()` has no literal form, so literal_eval cannot read it
                if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name)
                        and v.func.id == "set" and not v.args):
                    return []
                try:
                    return sorted(int(h) for h in ast.literal_eval(v))
                except (ValueError, TypeError):
                    _last_err["blackout"] = "BLACKOUT_HOURS is not a literal"
                    return None
        _last_err["blackout"] = "BLACKOUT_HOURS not found in trader"
        return None
    return cached("blackout", 300, _f)

GH_REPO     = os.environ.get("GH_REPO", "chrisgarceau6-dev/kalshi-trader")
GH_WORKFLOW = "late_certainty.yml"
# Cancelled runs are routine: the backup cron collides with the self-dispatch chain
# and the concurrency group drops one. Only these mean the trader actually broke.
_FAILED = {"failure", "timed_out", "startup_failure"}

def get_health():
    """Liveness of the trader itself, from the Actions API.

    A stale-run check alone would not have caught 2026-08-17: cron kept creating runs
    every 5 min while every one of them failed. So this reports the last *successful*
    run and the consecutive-failure count, not just the last run.
    """
    def _f():
        url = (f"https://api.github.com/repos/{GH_REPO}/actions/workflows/"
               f"{GH_WORKFLOW}/runs?per_page=20")
        headers = {"Accept": "application/vnd.github+json"}
        tok = os.environ.get("GH_READ_TOKEN", "").strip()
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                return {"error": f"HTTP {r.status_code}"}
            runs = r.json().get("workflow_runs", [])
        except Exception as e:
            return {"error": str(e)[:120]}
        if not runs:
            return {"error": "no runs"}
        now = datetime.now(timezone.utc)
        def _age(ts):
            d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return round((now - d).total_seconds() / 60.0, 1)
        last = runs[0]
        succ = next((x for x in runs if x.get("conclusion") == "success"), None)
        fails = 0
        for x in runs:
            c = x.get("conclusion")
            if c in _FAILED:
                fails += 1
            elif c == "success":
                break
        return {
            "last_run_ts":          last.get("created_at"),
            "last_run_age_min":     _age(last["created_at"]),
            "last_run_status":      last.get("status"),
            "last_run_conclusion":  last.get("conclusion"),
            "last_success_ts":      succ.get("created_at") if succ else None,
            # Age from run COMPLETION, not creation. Jobs run 900s (#157), so a
            # created_at age can never fall below ~15 min and the health bands
            # below would never see green.
            "last_success_age_min": _age(succ["updated_at"]) if succ else None,
            "consec_failures":      fails,
            "error":                None,
        }
    # 90s TTL keeps this under the 60 req/hr unauthenticated GitHub limit
    return cached("health", 90, _f)


LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<title>Kalshi</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%23000'/%3E%3Cpath d='M7 21l6-7 4 4 8-9' stroke='%2300D181' stroke-width='2.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{background:#000;color:#F4F5F7;display:flex;align-items:center;justify-content:center;
 font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Segoe UI',sans-serif;
 padding:24px;-webkit-font-smoothing:antialiased}
.box{width:100%;max-width:330px;text-align:center}
.mark{width:52px;height:52px;margin:0 auto 20px;display:block}
h1{font-size:21px;font-weight:700;letter-spacing:-.02em;margin-bottom:7px}
p{font-size:13.5px;color:#7C828C;margin-bottom:26px;line-height:1.5;font-weight:500}
form{display:flex;flex-direction:column;gap:10px}
input{background:#0B0C0E;border:1px solid rgba(255,255,255,.09);border-radius:13px;
 padding:15px 16px;color:#F4F5F7;font-size:16px;font-family:inherit;font-weight:550;
 outline:none;transition:border-color .2s,background .2s;width:100%}
input::placeholder{color:#4C525B}
input:focus{border-color:rgba(0,209,129,.5);background:#131519}
button{background:#00D181;color:#00160D;border:none;border-radius:13px;padding:15px;
 font-size:15px;font-weight:750;font-family:inherit;cursor:pointer;letter-spacing:-.01em;
 transition:opacity .18s}
button:active{opacity:.75}
.err{color:#FF453A;font-size:12.5px;font-weight:650;margin-top:15px;min-height:16px}
.hint{color:#4C525B;font-size:11.5px;margin-top:22px;line-height:1.6;font-weight:500}
</style></head><body>
<div class="box">
  <svg class="mark" viewBox="0 0 32 32"><rect width="32" height="32" rx="9" fill="#0B0C0E" stroke="rgba(255,255,255,.09)"/><path d="M7 21l6-7 4 4 8-9" stroke="#00D181" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>
  <h1>Kalshi</h1>
  <p>Enter your access token to view the account.</p>
  <form id="f" autocomplete="on">
    <input id="t" type="password" name="password" placeholder="Access token"
           autocomplete="current-password" autofocus enterkeyhint="go">
    <button type="submit">Unlock</button>
  </form>
  <div class="err" id="e"></div>
  <div class="hint">Saved for 90 days on this device. Phones and home-screen apps
    each keep their own login.</div>
</div>
<script>
document.getElementById('f').addEventListener('submit',function(ev){
  ev.preventDefault();
  var v=document.getElementById('t').value.trim();
  if(!v){document.getElementById('e').textContent='Enter a token';return;}
  // Round-trip through the server so it can set the cookie for THIS context.
  location.href='/?t='+encodeURIComponent(v);
});
if(location.search.indexOf('t=')>-1){document.getElementById('e').textContent='Incorrect token';}
</script>
</body></html>"""


app = Flask(__name__)

DASH_TOKEN = os.environ.get("DASH_TOKEN", "").strip()
HOSTED     = bool(os.environ.get("PORT"))   # Render sets PORT; local runs do not

@app.before_request
def _require_token():
    """This endpoint serves live balance, deposit history and open positions, and the
    URL is published in a public repo. Hosted instances must carry a token."""
    if not DASH_TOKEN:
        if HOSTED:
            return ("DASH_TOKEN is not set on this instance — refusing to serve "
                    "account data.", 503)
        return None                      # local dev binds to 127.0.0.1 only
    supplied = request.args.get("t") or request.cookies.get("dash_token") or ""
    if hmac.compare_digest(supplied, DASH_TOKEN):
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    # A form, not a dead end. Cookies are per-browser, and an iOS home-screen app
    # keeps its own jar separate from Safari, so a token pasted into one context
    # will not carry into the other. Every context needs somewhere to type it.
    return make_response(LOGIN_HTML, 401)

@app.route("/api/data")
def api_data():
    return jsonify({
        "balance":     get_balance(),
        "settlements": get_settlements(),
        "deposits":    get_deposits(),
        "positions":   get_positions(),
        "health":      get_health(),
        "blackout":    live_blackout_hours(),
        "ts":          datetime.now(ET).isoformat(timespec="seconds"),
        "errors":      dict(_last_err),
        "key_set":     bool(os.environ.get("KALSHI_PRIVATE_KEY_PATH") or os.environ.get("KALSHI_PRIVATE_KEY")),
        "key_id_set":  bool(os.environ.get("KALSHI_API_KEY_ID")),
    })

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,maximum-scale=1">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black">
<meta name="apple-mobile-web-app-title" content="Kalshi">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='8' fill='%23000'/%3E%3Cpath d='M7 21l6-7 4 4 8-9' stroke='%2300D181' stroke-width='2.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<title>Kalshi</title>
<style>
:root{
  --bg:#000; --s1:#0B0C0E; --s2:#131519; --s3:#1A1D22;
  --line:rgba(255,255,255,.07); --line-2:rgba(255,255,255,.12);
  --tx:#F4F5F7; --dim:#7C828C; --dimmer:#4C525B;
  --up:#00D181; --down:#FF453A; --warn:#FFA318; --info:#4C8DFF;
  --safe-b:env(safe-area-inset-bottom,0px);
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html{background:var(--bg)}
body{
  background:var(--bg);color:var(--tx);
  font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display','Segoe UI',Inter,sans-serif;
  max-width:620px;margin:0 auto;padding:0 20px calc(48px + var(--safe-b));
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  overscroll-behavior-y:contain;
}
.num{font-variant-numeric:tabular-nums;font-feature-settings:'tnum' 1,'ss01' 1}
.up{color:var(--up)}.down{color:var(--down)}.mut{color:var(--dim)}

/* ── pull to refresh ─────────────────────────────────────────────── */
#ptr{position:fixed;top:0;left:0;right:0;height:0;display:flex;align-items:flex-end;
  justify-content:center;pointer-events:none;z-index:50;overflow:hidden}
#ptr svg{margin-bottom:8px;opacity:0;transition:opacity .15s}
@keyframes spin{to{transform:rotate(360deg)}}
.ptr-spin svg{animation:spin .7s linear infinite}

/* ── banner ──────────────────────────────────────────────────────── */
#blackout{display:none;background:linear-gradient(90deg,#FFA318,#FFB84D);color:#140C00;
  text-align:center;padding:9px 16px;font-size:12px;font-weight:800;margin:0 -20px 4px;
  letter-spacing:.5px}

/* ── hero ────────────────────────────────────────────────────────── */
.hero{padding:30px 0 6px;text-align:center}
.hero-lbl{font-size:11px;color:var(--dimmer);text-transform:uppercase;letter-spacing:1.4px;
  font-weight:700;margin-bottom:10px}
.hero-bal{font-size:clamp(42px,12.5vw,58px);font-weight:700;letter-spacing:-.038em;line-height:1;
  transition:color .25s}
.hero-chg{font-size:15.5px;margin-top:9px;font-weight:600;letter-spacing:-.01em;
  display:flex;align-items:center;justify-content:center;gap:7px;min-height:20px}
.arrow{font-size:11px;line-height:1;transform:translateY(-1px)}
.chg-sub{color:var(--dimmer);font-weight:500}

/* health */
.health{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;font-weight:700;
  letter-spacing:.5px;padding:5px 11px 5px 9px;border-radius:20px;margin-top:14px;
  text-transform:uppercase;border:1px solid transparent;transition:all .3s}
.hdot{width:6px;height:6px;border-radius:50%;flex:0 0 auto}
@keyframes breathe{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.85)}}
.health-ok{background:rgba(0,209,129,.09);color:var(--up);border-color:rgba(0,209,129,.22)}
.health-ok .hdot{background:var(--up);animation:breathe 2.4s ease-in-out infinite;
  box-shadow:0 0 8px rgba(0,209,129,.7)}
.health-warn{background:rgba(255,163,24,.1);color:var(--warn);border-color:rgba(255,163,24,.26)}
.health-warn .hdot{background:var(--warn);animation:breathe 1.2s ease-in-out infinite}
.health-bad{background:rgba(255,69,58,.11);color:var(--down);border-color:rgba(255,69,58,.3)}
.health-bad .hdot{background:var(--down);animation:breathe .7s ease-in-out infinite}
.health-unk{background:var(--s2);color:var(--dim);border-color:var(--line)}
.health-unk .hdot{background:var(--dimmer)}

/* ── chart ───────────────────────────────────────────────────────── */
.chart-wrap{position:relative;height:220px;margin:18px -6px 0;touch-action:pan-y}
#svg{width:100%;height:100%;display:block;overflow:visible}
#pathLine{fill:none;stroke-width:2.25;stroke-linecap:round;stroke-linejoin:round;
  vector-effect:non-scaling-stroke}
#pathArea{stroke:none;opacity:.9}
#baseline{stroke:var(--line-2);stroke-width:1;stroke-dasharray:4 5;vector-effect:non-scaling-stroke}
#cross{stroke:rgba(255,255,255,.28);stroke-width:1;vector-effect:non-scaling-stroke;opacity:0}
#cdot{opacity:0}
#cdot circle{vector-effect:non-scaling-stroke}
@keyframes draw{from{stroke-dashoffset:var(--len)}to{stroke-dashoffset:0}}
@keyframes fade{from{opacity:0}to{opacity:.9}}
.anim #pathLine{animation:draw 1.05s cubic-bezier(.33,1,.68,1)}
.anim #pathArea{animation:fade 1.05s ease}
.pulse-dot{transform-box:fill-box;transform-origin:center}
@keyframes ping{0%{r:4;opacity:.55}100%{r:13;opacity:0}}
#livePing{animation:ping 2.2s ease-out infinite}
.chart-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  color:var(--dimmer);font-size:13px;font-weight:500}
.scrub-time{position:absolute;top:0;font-size:10.5px;color:var(--dim);font-weight:700;
  letter-spacing:.4px;opacity:0;transition:opacity .15s;pointer-events:none;
  transform:translateX(-50%);white-space:nowrap;background:var(--bg);padding:0 6px}

/* ── segmented controls ──────────────────────────────────────────── */
.controls{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:16px}
.seg{position:relative;display:flex;background:var(--s1);border:1px solid var(--line);
  border-radius:22px;padding:3px}
.seg .pill{position:absolute;top:3px;bottom:3px;border-radius:18px;background:var(--s3);
  transition:left .32s cubic-bezier(.32,.72,0,1),width .32s cubic-bezier(.32,.72,0,1);z-index:0}
.seg button{position:relative;z-index:1;background:none;border:none;color:var(--dim);
  font-size:12.5px;font-weight:650;padding:7px 13px;border-radius:18px;cursor:pointer;
  font-family:inherit;transition:color .25s;white-space:nowrap}
.seg button.on{color:var(--tx)}
.seg.mini button{font-size:10.5px;padding:6px 11px;letter-spacing:.4px;font-weight:750}

/* ── stats ───────────────────────────────────────────────────────── */
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin:22px 0 4px}
.stat{background:var(--s1);border:1px solid var(--line);border-radius:15px;padding:14px 13px}
.stat-lbl{font-size:9.5px;color:var(--dimmer);margin-bottom:7px;text-transform:uppercase;
  letter-spacing:.9px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.stat-val{font-size:20px;font-weight:680;line-height:1;letter-spacing:-.025em}
.stat-sub{font-size:10px;color:var(--dimmer);margin-top:5px;font-weight:600}

/* ── sections ────────────────────────────────────────────────────── */
h3{font-size:10.5px;color:var(--dimmer);text-transform:uppercase;letter-spacing:1.1px;
  margin:30px 0 12px;font-weight:750;display:flex;align-items:center;gap:8px}
h3 .count{background:var(--s2);color:var(--dim);border-radius:10px;padding:2px 7px;
  font-size:10px;letter-spacing:.3px}
.card{background:var(--s1);border:1px solid var(--line);border-radius:17px;overflow:hidden}
.empty{color:var(--dimmer);font-size:13px;padding:26px 0;text-align:center;font-weight:500}

/* positions */
.pos{padding:15px 15px 13px;border-bottom:1px solid var(--line)}
.pos:last-child{border-bottom:none}
.pos-top{display:flex;align-items:flex-start;gap:12px}
.pos-tick{font-size:15px;font-weight:700;letter-spacing:-.015em}
.pos-full{font-size:10.5px;color:var(--dimmer);margin-top:3px;font-weight:550;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pos-clock{margin-left:auto;text-align:right;flex:0 0 auto}
.pos-left{font-size:16px;font-weight:750;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.pos-q{font-size:11.5px;color:var(--dim);margin-top:9px;line-height:1.5}
.bar{height:3px;background:var(--s3);border-radius:2px;margin-top:11px;overflow:hidden}
.bar i{display:block;height:100%;border-radius:2px;transition:width .9s linear}
.nextclose{margin-top:7px;font-size:11.5px;color:var(--dimmer);letter-spacing:.03em;
  font-variant-numeric:tabular-nums}
.nextclose b{color:var(--dim);font-weight:600}
.nextclose.imminent b{color:var(--warn)}
.duo{display:block;margin-top:14px}
.duo>.card+.card{margin-top:12px}
.pad{padding:15px 16px 16px}
.pad h4{margin:0 0 12px;font-size:11px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--dimmer);font-weight:600}
.prow{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:9px}
.prow .l{font-size:12.5px;color:var(--dim)}
.prow .v{font-size:15px;font-weight:600}
.pnote{margin-top:10px;font-size:11px;color:var(--dimmer);line-height:1.45}
.srow{display:grid;grid-template-columns:44px 1fr 62px 44px;gap:9px;align-items:center;
  padding:6px 0;border-top:1px solid var(--line)}
.srow:first-of-type{border-top:0}
.srow .sname{font-size:12.5px;color:var(--tx);font-weight:600}
.srow .spk{height:16px}
.srow .sv{font-size:13px;text-align:right;font-variant-numeric:tabular-nums}
.srow .sn{font-size:11px;color:var(--dimmer);text-align:right}
@keyframes pulseGlow{0%,100%{opacity:.35}50%{opacity:1}}
.pulsing{animation:pulseGlow 1.1s ease-in-out infinite}
.pos-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:11px;margin-top:13px;
  padding-top:12px;border-top:1px solid var(--line)}
.pg .l{font-size:9px;color:var(--dimmer);text-transform:uppercase;letter-spacing:.7px;
  margin-bottom:5px;font-weight:750}
.pg .v{font-size:14px;font-weight:680;letter-spacing:-.02em}

/* trades */
.tr{display:flex;align-items:center;gap:11px;padding:13px 15px;border-bottom:1px solid var(--line);
  cursor:pointer;transition:background .18s;flex-wrap:wrap}
.tr:last-child{border-bottom:none}
.tr:active{background:var(--s2)}
.badge{font-size:10px;font-weight:850;width:21px;height:21px;border-radius:7px;flex:0 0 auto;
  display:flex;align-items:center;justify-content:center}
.badge-w{background:rgba(0,209,129,.13);color:var(--up)}
.badge-l{background:rgba(255,69,58,.14);color:var(--down)}
.tr-mid{flex:1;min-width:0}
.tr-ser{font-size:13.5px;font-weight:620;letter-spacing:-.01em}
.tr-time{font-size:10.5px;color:var(--dimmer);margin-top:2px;font-weight:550}
.tr-pnl{font-size:14.5px;font-weight:700;letter-spacing:-.02em;margin-left:auto}
.tr-side{font-size:9px;font-weight:800;letter-spacing:.6px;padding:2px 6px;border-radius:5px;
  background:var(--s3);color:var(--dim)}
.tr-det{width:100%;padding:12px 0 2px;margin-top:11px;border-top:1px solid var(--line);
  display:grid;grid-template-columns:1fr 1fr;gap:9px 18px;font-size:11px;color:var(--dimmer);
  font-weight:550}
.tr-det b{font-weight:700;color:var(--tx);font-variant-numeric:tabular-nums}

/* skeleton */
@keyframes shim{to{background-position:200% 0}}
.sk{background:linear-gradient(90deg,var(--s1) 25%,var(--s2) 37%,var(--s1) 63%);
  background-size:200% 100%;animation:shim 1.4s ease infinite;border-radius:7px;color:transparent!important}

/* stale */
#recon{display:none;background:rgba(255,163,24,.1);border:1px solid rgba(255,163,24,.35);
  color:#FFA318;border-radius:12px;padding:10px 13px;font-size:11.5px;font-weight:650;
  margin-top:16px;text-align:center;line-height:1.5}
#stale{display:none;background:rgba(255,69,58,.1);border:1px solid rgba(255,69,58,.3);
  color:var(--down);border-radius:12px;padding:10px 13px;font-size:11.5px;font-weight:650;
  margin-top:16px;text-align:center}
.foot{text-align:center;color:var(--dimmer);font-size:10.5px;margin-top:26px;font-weight:550}
.cols{display:block}

/* ── tablet ──────────────────────────────────────────────────────── */
@media (min-width:760px){
  body{max-width:720px}
  .chart-wrap{height:260px}
  .hero-bal{font-size:60px}
}

/* ── desktop: use the width instead of a phone column in a black sea ── */
@media (min-width:1080px){
  body{max-width:1280px;padding:0 34px calc(60px + var(--safe-b))}
  .hero{padding:40px 0 4px}
  .hero-bal{font-size:68px}
  .hero-chg{font-size:17px}
  .chart-wrap{height:340px;margin:22px -8px 0}
  .stats{grid-template-columns:repeat(6,1fr);gap:11px;margin:26px 0 4px}
  .stat{padding:16px 15px}
  .stat-val{font-size:22px}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:30px;align-items:start}
  .duo{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px}
  .duo>.card+.card{margin-top:0}
  /* positions stay in view while the trade log scrolls past them */
  .col-left{position:sticky;top:22px}
  .col-left h3,.col-right h3{margin-top:26px}
  #blackout{margin:0 -34px 4px}
}
@media (min-width:1500px){
  body{max-width:1440px}
  .chart-wrap{height:380px}
}
</style>
</head>
<body>
<div id="ptr"><svg width="19" height="19" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="#4C525B" stroke-width="2.5" stroke-dasharray="42" stroke-dashoffset="14" stroke-linecap="round"/></svg></div>
<div id="blackout">BLACKOUT HOUR — STRATEGY PAUSED</div>

<div class="hero">
  <div class="hero-lbl" id="heroLbl">Portfolio</div>
  <div class="hero-bal num sk" id="bal">$0000.00</div>
  <div class="hero-chg num" id="chg"><span class="sk">+$00.00 today</span></div>
  <div class="health health-unk" id="health"><span class="hdot"></span><span id="healthTx">checking</span></div>
  <div class="nextclose" id="nextClose"></div>
</div>

<div class="chart-wrap">
  <svg id="svg" preserveAspectRatio="none" viewBox="0 0 1000 220">
    <defs>
      <linearGradient id="grad" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%"   id="g0" stop-color="#00D181" stop-opacity=".26"/>
        <stop offset="100%" id="g1" stop-color="#00D181" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <path id="pathArea" fill="url(#grad)"></path>
    <line id="baseline" x1="0" x2="1000"></line>
    <path id="pathLine"></path>
    <line id="cross" y1="0" y2="220"></line>
    <g id="cdot"><circle id="cdotO" r="5.5" fill="#000" stroke-width="2.5"></circle></g>
    <g id="liveDot" opacity="0"><circle id="livePing" fill="none"></circle><circle id="liveCore" r="3.5"></circle></g>
  </svg>
  <div class="scrub-time" id="scrubTime"></div>
  <div class="chart-empty" id="chartEmpty" style="display:none">No activity in this range</div>
</div>

<div class="controls">
  <div class="seg" id="ranges"><div class="pill"></div>
    <button data-r="1H">1H</button><button data-r="1D">1D</button><button data-r="1W">1W</button><button data-r="1M">1M</button><button data-r="ALL">All</button>
  </div>
  <div class="seg mini" id="modes"><div class="pill"></div>
    <button data-m="pnl">P&amp;L</button><button data-m="bal">Balance</button>
  </div>
</div>

<div id="stale">Data may be stale — last refresh failed</div>
<div id="recon"></div>

<div class="stats" id="stats">
  <div class="stat"><div class="stat-lbl" id="l0">P&L</div><div class="stat-val num sk" id="v0">$0</div><div class="stat-sub" id="s0"></div></div>
  <div class="stat"><div class="stat-lbl" id="l1">Win rate</div><div class="stat-val num sk" id="v1">0%</div><div class="stat-sub" id="s1"></div></div>
  <div class="stat"><div class="stat-lbl" id="l2">Trades</div><div class="stat-val num sk" id="v2">0</div><div class="stat-sub" id="s2"></div></div>
  <div class="stat"><div class="stat-lbl" id="l3">Hour P&L</div><div class="stat-val num sk" id="v3">$0</div><div class="stat-sub" id="s3"></div></div>
  <div class="stat"><div class="stat-lbl" id="l4">Hour WR</div><div class="stat-val num sk" id="v4">0%</div><div class="stat-sub" id="s4"></div></div>
  <div class="stat"><div class="stat-lbl" id="l5">Open</div><div class="stat-val num sk" id="v5">0</div><div class="stat-sub" id="s5"></div></div>
</div>

<div class="duo">
  <div class="card pad" id="paceCard"></div>
  <div class="card pad" id="seriesCard"></div>
</div>

<div class="cols">
  <div class="col-left">
    <h3>Open positions <span class="count" id="posN">0</span></h3>
    <div class="card" id="positions"><div class="empty">No open positions</div></div>
  </div>
  <div class="col-right">
    <h3>Recent trades</h3>
    <div class="card" id="trades"><div class="empty">Loading…</div></div>
  </div>
</div>

<div class="foot" id="foot">—</div>

<script>
'use strict';
const $=id=>document.getElementById(id);
const UP='#00D181', DOWN='#FF453A', BLUE='#4C8DFF';
const AUG1=new Date('2026-08-01T04:00:00Z').getTime();
// The ALL range floors at Aug 1 (Kalshi keeps settled markets ~67 days), so it is
// not all time and must not be labelled as if it were.
const RLBL={'1H':'Hour','1D':'Today','1W':'Week','1M':'Month','ALL':'Since Aug 1'};
let range='1D', mode='pnl', last=null, expanded=new Set(), pts=[], firstDraw=true, scrubbing=false;

/* Modelled baseline, from the canonical harness at $50 flat with the measured
   0.105c execution gap. Reproduce with:
     python3 scripts/backtest.py            (then apply --slip 0.105)
   These are the ONLY hardcoded strategy numbers on the page. Everything else is
   derived from live data. Re-measure them if the config changes; a stale baseline
   here silently mis-scores every day. */
const MODEL_PER_TRADE = 0.54;    // $/trade at $50 flat, 0.105c slip
const MODEL_TRADES_DAY = 126;    // distinct entries per day the harness takes
const MODEL_BET = 50;            // the bet size those two figures were measured at

/* ── formatting ─────────────────────────────────────────────────── */
const money=n=>(n<0?'-':'')+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const signed=n=>(n>=0?'+':'-')+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const cls=n=>n>0?'up':n<0?'down':'mut';
const pct=(w,n)=>n?(w/n*100).toFixed(1)+'%':'—';
const et=(d,o)=>d.toLocaleString('en-US',Object.assign({timeZone:'America/New_York'},o));

function cutoff(r){
  const now=new Date(); let t;
  if(r==='1H') t=now.getTime()-3600000;
  else if(r==='1D'){
    const p=et(now,{hour12:false,hour:'2-digit',minute:'2-digit',second:'2-digit'}).split(':').map(Number);
    t=now.getTime()-((p[0]%24*3600+p[1]*60+p[2])*1000);
  }
  else if(r==='1W') t=now.getTime()-7*86400000;
  else if(r==='1M') t=now.getTime()-30*86400000;
  else t=AUG1;
  return Math.max(t,AUG1);
}

/* ── number tween ───────────────────────────────────────────────── */
const tweens={};
function tween(el,to,fmt){
  if(!el) return;
  const key=el.id, from=(tweens[key]!==undefined)?tweens[key]:to;
  if(from===to){el.textContent=fmt(to);tweens[key]=to;return;}
  const t0=performance.now(), dur=520;
  cancelAnimationFrame(el._raf||0);
  const step=t=>{
    const k=Math.min(1,(t-t0)/dur), e=1-Math.pow(1-k,3);
    const v=from+(to-from)*e;
    el.textContent=fmt(v);
    if(k<1) el._raf=requestAnimationFrame(step); else tweens[key]=to;
  };
  el._raf=requestAnimationFrame(step);
}
function setNum(el,txt){ el.classList.remove('sk'); el.textContent=txt; }

/* ── health ─────────────────────────────────────────────────────── */
function renderHealth(h){
  const el=$('health'), tx=$('healthTx');
  let c='health-unk', t='status unknown';
  if(!h||h.error){ t='status unavailable'; }
  else{
    const a=h.last_success_age_min, f=h.consec_failures||0;
    const ago=a==null?'never':(a<60?Math.round(a)+'m ago':(a/60).toFixed(1)+'h ago');
    // Jobs run 900s and land ~15 min apart, so age-since-completion cycles
    // 0-15 min in health. Two missed cycles (>32) is a real outage.
    if(f>=2){ c='health-bad'; t=f+' consecutive failed runs'; }
    else if(a==null||a>32){ c='health-bad'; t='trader down · '+ago; }
    else if(a>18){ c='health-warn'; t='trader lagging · '+ago; }
    else { c='health-ok'; t='trader live · '+ago; }
  }
  el.className='health '+c; tx.textContent=t;
}

/* ── chart ──────────────────────────────────────────────────────── */
const W=1000,H=220,PADY=18;
function drawChart(series,color,baseVal){
  const line=$('pathLine'), area=$('pathArea'), sv=$('svg');
  pts=[];
  if(series.length<2){
    line.setAttribute('d',''); area.setAttribute('d','');
    $('baseline').style.opacity=0; $('liveDot').setAttribute('opacity','0');
    $('chartEmpty').style.display='flex'; return;
  }
  $('chartEmpty').style.display='none';
  const t0=series[0].t, t1=series[series.length-1].t, span=(t1-t0)||1;
  let lo=Infinity,hi=-Infinity;
  for(const p of series){ if(p.v<lo)lo=p.v; if(p.v>hi)hi=p.v; }
  if(baseVal!=null){ lo=Math.min(lo,baseVal); hi=Math.max(hi,baseVal); }
  const pad=(hi-lo)*0.12||1; lo-=pad; hi+=pad;
  const X=t=>(t-t0)/span*W;
  const Y=v=>PADY+(1-(v-lo)/((hi-lo)||1))*(H-PADY*2);
  let d='';
  series.forEach((p,i)=>{ const x=X(p.t),y=Y(p.v); pts.push({x,y,t:p.t,v:p.v}); d+=(i?'L':'M')+x.toFixed(2)+' '+y.toFixed(2); });
  line.setAttribute('d',d); line.setAttribute('stroke',color);
  area.setAttribute('d',d+'L'+W+' '+H+'L0 '+H+'Z');
  $('g0').setAttribute('stop-color',color); $('g1').setAttribute('stop-color',color);
  if(baseVal!=null){ const by=Y(baseVal); const bl=$('baseline'); bl.setAttribute('y1',by); bl.setAttribute('y2',by); bl.style.opacity=1; }
  else $('baseline').style.opacity=0;
  const lp=pts[pts.length-1];
  const ld=$('liveDot'); ld.setAttribute('opacity','1');
  ld.setAttribute('transform','translate('+lp.x+','+lp.y+')');
  $('liveCore').setAttribute('fill',color); $('livePing').setAttribute('stroke',color);
  $('cdotO').setAttribute('stroke',color);
  if(firstDraw){
    const len=line.getTotalLength();
    line.style.setProperty('--len',len);
    line.style.strokeDasharray=len; sv.classList.remove('anim');
    void sv.offsetWidth; sv.classList.add('anim');
    setTimeout(()=>{ line.style.strokeDasharray='none'; },1100);
    firstDraw=false;
  }
}

/* ── scrub ──────────────────────────────────────────────────────── */
const wrap=document.querySelector('.chart-wrap');
let lastIdx=-1;
function scrubAt(clientX){
  if(!pts.length) return;
  const r=wrap.getBoundingClientRect();
  const fx=Math.max(0,Math.min(1,(clientX-r.left)/r.width))*W;
  let best=0,bd=Infinity;
  for(let i=0;i<pts.length;i++){ const dd=Math.abs(pts[i].x-fx); if(dd<bd){bd=dd;best=i;} }
  const p=pts[best];
  if(best!==lastIdx){ lastIdx=best; if(navigator.vibrate) navigator.vibrate(1); }
  const cr=$('cross'); cr.setAttribute('x1',p.x); cr.setAttribute('x2',p.x); cr.style.opacity=1;
  const cd=$('cdot'); cd.setAttribute('transform','translate('+p.x+','+p.y+')'); cd.style.opacity=1;
  $('liveDot').setAttribute('opacity','0');
  const st=$('scrubTime');
  st.textContent=et(new Date(p.t),{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true});
  st.style.left=(p.x/W*100)+'%'; st.style.opacity=1;
  const bal=$('bal'), chg=$('chg');
  if(mode==='bal'){ bal.textContent=money(p.v); chg.innerHTML='<span class="chg-sub">'+st.textContent+' ET</span>'; }
  else{
    bal.textContent=signed(p.v); bal.className='hero-bal num '+cls(p.v);
    chg.innerHTML='<span class="chg-sub">'+st.textContent+' ET</span>';
  }
}
function endScrub(){
  scrubbing=false; lastIdx=-1;
  $('cross').style.opacity=0; $('cdot').style.opacity=0; $('scrubTime').style.opacity=0;
  $('liveDot').setAttribute('opacity','1');
  if(last) render(last);
}
wrap.addEventListener('pointerdown',e=>{ scrubbing=true; wrap.setPointerCapture(e.pointerId); scrubAt(e.clientX); });
wrap.addEventListener('pointermove',e=>{ if(scrubbing) scrubAt(e.clientX); });
wrap.addEventListener('pointerup',endScrub);
wrap.addEventListener('pointercancel',endScrub);
wrap.addEventListener('pointerleave',()=>{ if(scrubbing) endScrub(); });

/* ── segmented pill ─────────────────────────────────────────────── */
function movePill(seg){
  const on=seg.querySelector('button.on'), pill=seg.querySelector('.pill');
  if(!on) return;
  pill.style.left=on.offsetLeft+'px'; pill.style.width=on.offsetWidth+'px';
}
function initSeg(seg,attr,def,cb){
  seg.querySelectorAll('button').forEach(b=>{
    if(b.dataset[attr]===def) b.classList.add('on');
    b.addEventListener('click',()=>{
      seg.querySelectorAll('button').forEach(x=>x.classList.remove('on'));
      b.classList.add('on'); movePill(seg);
      if(navigator.vibrate) navigator.vibrate(2);
      cb(b.dataset[attr]);
    });
  });
  requestAnimationFrame(()=>movePill(seg));
}
initSeg($('ranges'),'r','1D',v=>{ range=v; firstDraw=true; if(last) render(last); });
initSeg($('modes'),'m','pnl',v=>{ mode=v; firstDraw=true; if(last) render(last); });
window.addEventListener('resize',()=>{ movePill($('ranges')); movePill($('modes')); });

/* ── reconciliation ─────────────────────────────────────────────────
   Every percent on this page rests on one assumption: that deposits + settlements
   explain every dollar the account moved. Nothing in the API proves that — a
   withdrawal is invisible (Kalshi exposes no withdrawals feed here), and history
   older than SETTLEMENT_FLOOR is simply absent. Both silently vanish into `drift`,
   which is the term the whole curve is anchored on.

   So compare two independent things across visits: how much equity actually changed,
   and how much the event feed says should have changed. A persistent gap means the
   feed is incomplete and the percentages cannot be trusted.

   Fees are why the tolerance is not zero: Kalshi charges them at fill, but they are
   only booked into P&L at settlement, so an open position sits fee-low until it
   resolves. That is tracked explicitly rather than absorbed. */
const RECON_KEY='dash-recon-v1';

/* ── pace vs the modelled baseline ──────────────────────────────────
   Compared per TRADE, not per hour. Settlements do not arrive uniformly through the
   day, so prorating a daily figure by clock time would score a quiet morning as a
   shortfall. Trade count is shown against the full-day model with the elapsed
   fraction beside it, so a partial day reads as partial rather than as a miss. */
function renderPace(sett, d){
  const el=$('paceCard'); if(!el) return;
  const dayStart=cutoff('1D');
  const today=sett.filter(s=>new Date(s.ts).getTime()>=dayStart);
  const n=today.length, pnl=today.reduce((a,s)=>a+s.pnl,0);
  const per=n?pnl/n:null;
  // scale the baseline to whatever the account is actually betting
  const openCost=(d.positions||[]).reduce((a,p)=>a+(p.cost||0),0);
  const bet=(d.positions||[]).length?openCost/(d.positions||[]).length:MODEL_BET;
  const scale=Math.max(0.2,Math.min(6,bet/MODEL_BET));
  const expPer=MODEL_PER_TRADE*scale, expDay=MODEL_PER_TRADE*MODEL_TRADES_DAY*scale;
  const et0=new Date(dayStart), frac=Math.min(1,(Date.now()-dayStart)/86400000);
  const ratio=per!=null&&expPer>0?per/expPer:null;
  const capture=Math.min(100,n/MODEL_TRADES_DAY*100);
  const barCol=ratio==null?'var(--dimmer)':ratio>=1?UP:ratio>=0?'var(--warn)':DOWN;
  el.innerHTML='<h4>Pace vs model</h4>'+
    '<div class="prow"><span class="l">Today</span><span class="v num '+cls(pnl)+'">'+
      signed(pnl)+'</span></div>'+
    '<div class="prow"><span class="l">Model pace</span><span class="v num mut">'+
      signed(expDay*frac)+'</span></div>'+
    '<div class="prow"><span class="l">Per trade</span><span class="v num '+
      (per==null?'mut':cls(per-expPer))+'">'+
      (per==null?'—':signed(per)+'  vs  '+signed(expPer))+'</span></div>'+
    '<div class="prow"><span class="l">Trades</span><span class="v num mut">'+n+
      ' / '+MODEL_TRADES_DAY+'</span></div>'+
    '<div class="bar"><i style="width:'+capture+'%;background:'+barCol+'"></i></div>'+
    '<div class="pnote">'+(frac*100).toFixed(0)+'% of the day elapsed · baseline '+
      signed(MODEL_PER_TRADE)+'/trade at $'+MODEL_BET+
      (scale!==1?' scaled x'+scale.toFixed(2):'')+'</div>';
}

/* ── per-series breakdown with sparklines ───────────────────────── */
function spark(vals,col){
  if(vals.length<2) return '';
  let run=0; const cum=vals.map(v=>run+=v);
  const lo=Math.min(0,...cum), hi=Math.max(0,...cum), sp=(hi-lo)||1;
  const w=60,h=16;
  const pts=cum.map((v,i)=>(i/(cum.length-1)*w).toFixed(1)+','+
    (h-((v-lo)/sp)*h).toFixed(1)).join(' ');
  const zero=(h-((0-lo)/sp)*h).toFixed(1);
  return '<svg class="spk" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'">'+
    '<line x1="0" y1="'+zero+'" x2="'+w+'" y2="'+zero+'" stroke="var(--line-2)" stroke-width="1"/>'+
    '<polyline points="'+pts+'" fill="none" stroke="'+col+'" stroke-width="1.6" '+
    'stroke-linejoin="round" stroke-linecap="round"/></svg>';
}
function renderSeries(sett){
  const el=$('seriesCard'); if(!el) return;
  const cut=cutoff(range);
  const inR=sett.filter(s=>new Date(s.ts).getTime()>=cut);
  const by={};
  for(const s of inR){
    const k=(s.series||'').replace('KX','').replace('15M','')||'?';
    (by[k]=by[k]||[]).push(s);
  }
  const keys=Object.keys(by).sort((a,b)=>
    by[b].reduce((x,s)=>x+s.pnl,0)-by[a].reduce((x,s)=>x+s.pnl,0));
  if(!keys.length){ el.innerHTML='<h4>By series</h4><div class="empty">No trades in range</div>'; return; }
  el.innerHTML='<h4>By series · '+RLBL[range]+'</h4>'+keys.map(k=>{
    const g=by[k], p=g.reduce((a,s)=>a+s.pnl,0), w=g.filter(s=>s.won).length;
    const col=p>0?UP:p<0?DOWN:'#7C828C';
    return '<div class="srow"><div class="sname">'+k+'</div>'+
      spark(g.map(s=>s.pnl),col)+
      '<div class="sv '+cls(p)+'">'+signed(p)+'</div>'+
      '<div class="sn">'+pct(w,g.length)+'</div></div>';
  }).join('');
}

/* ── next settlement countdown ──────────────────────────────────── */
function tickClose(){
  const el=$('nextClose'); if(!el) return;
  const now=Date.now(), q=15*60*1000;
  const next=Math.ceil(now/q)*q;              // 15M markets close on :00/:15/:30/:45
  const s=Math.max(0,Math.round((next-now)/1000));
  const mm=Math.floor(s/60), ss=String(s%60).padStart(2,'0');
  el.innerHTML='next close in <b>'+mm+':'+ss+'</b>';
  el.classList.toggle('imminent', s<=60);
  const live=$('liveDot');
  if(live) live.classList.toggle('pulsing', s<=60);
}

function reconcile(equity, settAll, deps, openFee){
  const now=Date.now();
  let prev=null;
  try{ prev=JSON.parse(localStorage.getItem(RECON_KEY)||'null'); }catch(e){}
  const save=()=>{ try{
    localStorage.setItem(RECON_KEY,JSON.stringify({t:now,equity:equity,openFee:openFee}));
  }catch(e){} };
  if(!prev||!(prev.t>0)||typeof prev.equity!=='number'){ save(); return null; }
  // Only events strictly newer than the last sample: older ones were already
  // counted, and the oldest can age out of the fetch window entirely.
  const moved=settAll.filter(s=>new Date(s.ts).getTime()>prev.t).reduce((a,s)=>a+s.pnl,0)
             +deps.filter(x=>x.t>prev.t).reduce((a,x)=>a+x.dep,0);
  const feeDelta=openFee-(prev.openFee||0);
  const gap=+(equity-(prev.equity+moved-feeDelta)).toFixed(2);
  save();
  return {gap:gap, since:prev.t, moved:+moved.toFixed(2)};
}

function showRecon(rec, equity, retHidden){
  const el=$('recon'); const msgs=[];
  if(retHidden){
    msgs.push('Percent hidden — deposits and settlements do not account for the '+
              'current balance, so a return cannot be computed honestly.');
  }
  if(rec){
    // Settlement timestamps can arrive slightly out of order, so require a gap
    // bigger than a rounding wobble before crying wolf.
    const tol=Math.max(2, equity*0.0025);
    if(Math.abs(rec.gap)>tol){
      msgs.push(money(Math.abs(rec.gap))+(rec.gap<0?' left':' entered')+
        ' the account since '+et(new Date(rec.since),{month:'short',day:'numeric',
        hour:'numeric',minute:'2-digit',hour12:true})+
        ' with no matching deposit or settlement. Percentages are understated'+
        (rec.gap<0?' — a withdrawal is the usual cause.':' until it is explained.'));
    }
  }
  el.innerHTML=msgs.join('<br>');
  el.style.display=msgs.length?'block':'none';
}

/* ── render ─────────────────────────────────────────────────────── */
function render(d){
  renderHealth(d.health);
  const settAll=d.settlements||[];
  const sett=settAll.filter(s=>s.strat!==false);   // flag absent on older payloads
  const cut=cutoff(range);
  const inR=sett.filter(s=>new Date(s.ts).getTime()>=cut);

  // blackout — server reads BLACKOUT_HOURS from the trader; null means unverified
  const hr=parseInt(et(new Date(),{hour:'numeric',hour12:false}))||0;
  $('blackout').style.display=(d.blackout||[]).includes(hr)?'block':'none';

  // series — strategy stats use 15M settlements only; the balance line uses every
  // settlement, because anything that moved cash has to be in the reconstruction.
  let run=0; const pnlAll=sett.map(s=>{run+=s.pnl;return{t:new Date(s.ts).getTime(),v:run};});
  const deps=(d.deposits||[]).map(x=>({t:new Date(x.ts).getTime(),dep:x.amount,pnl:0}));
  const trs=settAll.map(x=>({t:new Date(x.ts).getTime(),dep:0,pnl:x.pnl}));
  const comb=deps.concat(trs).sort((a,b)=>a.t-b.t);
  // Anchor on EQUITY, not cash: /portfolio/balance excludes money tied up in open
  // positions, and the curve is built from the live figure, so anchoring on cash
  // back-dated that hole across the whole range.
  // Cost basis comes from the fills API and can come back empty; entry x contracts
  // is the same number from the position record. Falling through to 0 would
  // understate equity by the whole open position and re-inflate every percentage.
  const openCost=(d.positions||[]).reduce((a,p)=>{
    const c=+p.cost||0;
    if(c>0) return a+c;
    const e=+p.entry||0, n=+p.contracts||0;
    return a+(e>0&&n>0?e/100*n:0);
  },0);
  const liveBal=(d.balance||0)+openCost;
  // Then walk BACKWARDS from today: the balance at any past instant is today's
  // equity minus everything that has happened since. The old forward sum needed a
  // `drift` term to reconcile with the live balance, which silently assumed the
  // event feed was complete back to the account's first day. It is not — settlements
  // stop at SETTLEMENT_FLOOR while deposits go back months, so drift absorbed every
  // missing trade and smeared it across the whole curve. Walking backwards needs the
  // feed complete only from the range start forward, which is exactly what it is.
  const balAll=new Array(comb.length);
  let after=0;
  for(let i=comb.length-1;i>=0;i--){
    const e=comb[i];
    balAll[i]={t:e.t,v:+(liveBal-after).toFixed(2),dep:e.dep,pnl:e.pnl};
    after+=e.dep+e.pnl;
  }

  let series,color,baseVal;
  if(mode==='pnl'){
    const pre=pnlAll.filter(x=>x.t<cut), base=pre.length?pre[pre.length-1].v:0;
    series=pnlAll.filter(x=>x.t>=cut).map(x=>({t:x.t,v:+(x.v-base).toFixed(2)}));
    if(series.length) series.unshift({t:cut,v:0});
    baseVal=0;
    const endV=series.length?series[series.length-1].v:0;
    color=endV>=0?UP:DOWN;
  }else{
    series=balAll.filter(x=>x.t>=cut);
    baseVal=series.length?series[0].v:null;
    const endV=series.length?series[series.length-1].v:0;
    color=baseVal==null?BLUE:(endV>=baseVal?UP:DOWN);
  }
  drawChart(series,color,baseVal);

  // hero
  const rangePnl=inR.reduce((a,s)=>a+s.pnl,0);
  const bal=$('bal'), chg=$('chg');
  bal.classList.remove('sk');

  // Return % belongs on P&L, never on Balance: the balance line moves on deposits too,
  // so a $100 deposit into a $400 account would read as a +25% "gain". This is a
  // time-weighted return — each settlement is chained against the balance it was
  // actually earned on, so a deposit resets the base for later trades but is never
  // itself counted as a gain. Dividing range P&L by a single balance would break the
  // other way: an account funded mid-range shows a huge percent off a tiny base.
  // Return on the capital actually at work (modified Dietz): range P&L over the
  // opening balance plus each in-range deposit weighted by how long it was invested.
  //
  // NOT a chained per-trade TWR. Chaining ~1,400 trades makes the answer depend
  // almost entirely on what balance the reconstruction believes existed at the
  // start, and on 2026-08-18 that produced "+169.42%" next to $221.98 of P&L: the
  // real opening balance was $367.06 (confirmed against the trader's own logged
  // balance at 2026-07-31T23:59Z), and the correct figure is +31.05%.
  const firstIn=balAll.find(b=>b.t>=cut);
  const startBal=firstIn?+(firstIn.v-firstIn.dep-firstIn.pnl).toFixed(2):liveBal;
  const nowT=Date.now(), rspan=Math.max(1,nowT-cut);
  let capital=startBal;
  for(const x of deps){
    if(x.t>=cut) capital+=x.dep*Math.max(0,Math.min(1,(nowT-x.t)/rspan));
  }
  // A non-positive opening balance means the feed is missing something (a
  // withdrawal, most likely). Report nothing rather than a number off a bad base.
  const ret=(startBal>0&&capital>0)?rangePnl/capital*100:null;
  const openFee=(d.positions||[]).reduce((a,p)=>a+(+p.fee||0),0);
  showRecon(reconcile(liveBal,settAll,deps,openFee), liveBal, ret===null);
  const retTx=ret==null?'':(ret>=0?'+':'-')+Math.abs(ret).toFixed(2)+'%';

  if(mode==='bal'){
    $('heroLbl').textContent='Portfolio balance';
    bal.className='hero-bal num';
    tween(bal,liveBal,v=>money(v));
    const delta=series.length?series[series.length-1].v-series[0].v:0;
    chg.className='hero-chg num '+cls(delta);
    const notes=[RLBL[range]];
    if(deps.some(x=>x.t>=cut)) notes.push('incl. deposits');
    if(openCost>0) notes.push(money(openCost)+' in open positions');
    chg.innerHTML='<span class="arrow">'+(delta>=0?'▲':'▼')+'</span>'+signed(delta)+
      ' <span class="chg-sub">'+notes.join(' · ')+'</span>';
  }else{
    $('heroLbl').textContent=RLBL[range]+' P&L';
    bal.className='hero-bal num '+cls(rangePnl);
    tween(bal,rangePnl,v=>signed(v));
    chg.className='hero-chg num '+cls(rangePnl);
    chg.innerHTML=(ret==null?'':'<span class="arrow">'+(ret>=0?'▲':'▼')+'</span>'+retTx+' ')+
      '<span class="chg-sub">'+(ret==null?'':'on '+money(capital)+' avg capital · ')+
      money(liveBal)+' portfolio · '+inR.length+' trades</span>';
  }

  // stats
  const wins=inR.filter(s=>s.won).length;
  const hs=sett.filter(s=>new Date(s.ts).getTime()>=cutoff('1H'));
  const hw=hs.filter(s=>s.won).length, hp=hs.reduce((a,s)=>a+s.pnl,0);
  const alt=range==='1H';
  const dS=alt?sett.filter(s=>new Date(s.ts).getTime()>=cutoff('1D')):hs;
  const dW=dS.filter(s=>s.won).length, dP=dS.reduce((a,s)=>a+s.pnl,0);
  const avg=inR.length?rangePnl/inR.length:0;

  // A TRADE-weighted win rate against a flat 92% is the wrong comparison and has
  // already misled once (CLAUDE.md: "Never compare a raw win rate to a flat 92%").
  // P&L experiences a DOLLAR-weighted rate, and break-even moves with the price
  // paid. Both are computed from the trades actually in range:
  //   dollar-weighted WR = cost(winners) / cost(all)
  //   break-even         = cost(all) / contracts(all) = cost-weighted avg price
  // Contracts are exact for winners (settlement pays $1/contract, so rev IS the
  // count). Losers carry no count, so they are imputed at the winner-implied
  // average price; every trade sits in the same 90-93c band, which holds that
  // error near 0.03pp — two orders below the ~1.2pp effect this is here to show.
  function margin(rows){
    if(!rows.length) return null;
    const cost=rows.reduce((a,s)=>a+s.cost,0);
    if(cost<=0) return null;
    const W=rows.filter(s=>s.won);
    const wCost=W.reduce((a,s)=>a+s.cost,0);
    const wCon =W.reduce((a,s)=>a+s.rev ,0);          // $1/contract => rev = count
    if(wCon<=0) return null;
    const px=wCost/wCon;                               // implied avg entry price
    if(!(px>0&&px<1)) return null;
    const con=rows.reduce((a,s)=>a+(s.won?s.rev:s.cost/px),0);
    if(con<=0) return null;
    const dwWR=100*wCost/cost, bev=100*cost/con;
    return {dwWR:dwWR, be:bev, margin:dwWR-bev};
  }
  const M=margin(inR);
  const be=M?M.margin:null;
  const S=[
    ['l0',RLBL[range]+' P&L','v0',signed(rangePnl),cls(rangePnl),'s0',inR.length?signed(avg)+' / trade':''],
    ['l1',RLBL[range]+' WR ($-wtd)','v1',M?M.dwWR.toFixed(2)+'%':pct(wins,inR.length),
     M?cls(M.margin):'','s1',
     M?(M.margin>=0?'+':'')+M.margin.toFixed(2)+'pp vs '+M.be.toFixed(2)+'% b/e':''],
    ['l2',RLBL[range]+' trades','v2',String(inR.length),'mut','s2',wins+'W · '+(inR.length-wins)+'L'],
    ['l3',(alt?'Today':'Hour')+' P&L','v3',signed(dP),cls(dP),'s3',''],
    ['l4',(alt?'Today':'Hour')+' WR','v4',pct(dW,dS.length),'','s4',dS.length+' trades'],
    ['l5','Open','v5',String((d.positions||[]).length),(d.positions||[]).length?'up':'mut','s5','']
  ];
  for(const [li,lt,vi,vt,vc,si,st] of S){
    $(li).textContent=lt; const el=$(vi);
    el.className='stat-val num '+vc; setNum(el,vt); $(si).textContent=st;
  }

  renderPace(sett, d);
  renderSeries(sett);

  // positions
  const pos=d.positions||[]; $('posN').textContent=pos.length;
  const pe=$('positions');
  if(pos.length){
    pe.innerHTML=pos.map(p=>{
      const ms=p.close_time?new Date(p.close_time).getTime()-Date.now():0;
      const s=Math.max(0,Math.floor(ms/1000));
      const lt=ms<=0?'settling':(s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s');
      const locked=ms<120000;
      const c=p.contracts||0;
      const frac=Math.max(0,Math.min(1,s/600))*100;   // markets open ~600s before close
      const col=ms<=0?'#7C828C':s<120?DOWN:s<300?'#FFA318':UP;
      const spread=(p.ask!=null&&p.bid!=null)?(p.ask-p.bid)+'¢':'—';
      // No bid means no mark. The old fallback showed contracts x $1 in the last two
      // minutes, which asserts the position wins — it is exactly when that is not
      // guaranteed. Unknown is shown as unknown.
      const mv=p.bid!=null?'$'+(c*p.bid/100).toFixed(2):'—';
      // Profit if this settles in your favour, NOT gross settlement value: you paid
      // ~92c for a $1 contract, so the upside is the ~8c spread, not the whole dollar.
      const win=p.entry!=null?(c*(100-p.entry)/100-(p.fee||0)):null;
      const risk=p.cost!=null?p.cost:null;
      return '<div class="pos"><div class="pos-top"><div style="flex:1;min-width:0">'+
        '<div class="pos-tick">'+p.ticker.split('-')[0]+
          (p.entry!=null?' <span class="tr-side">'+(p.side||'yes').toUpperCase()+' @ '+p.entry+'¢</span>':'')+'</div>'+
        '<div class="pos-full">'+p.ticker+'</div></div>'+
        '<div class="pos-clock"><div class="pos-left" style="color:'+col+'">'+lt+'</div></div></div>'+
        (p.title?'<div class="pos-q">'+p.title+'</div>':'')+
        '<div class="bar"><i style="width:'+frac+'%;background:'+col+'"></i></div>'+
        '<div class="pos-grid">'+
        '<div class="pg"><div class="l">Contracts</div><div class="v num">'+(c||'—')+'</div></div>'+
        '<div class="pg"><div class="l">'+(p.side||'yes').toUpperCase()+' bid / ask</div><div class="v num">'+
          (p.bid!=null?p.bid+'¢':'—')+' / '+(p.ask!=null?p.ask+'¢':'—')+'</div></div>'+
        '<div class="pg"><div class="l">Spread</div><div class="v num">'+spread+'</div></div>'+
        '<div class="pg"><div class="l">If win</div><div class="v num up">'+(win!=null?'+$'+win.toFixed(2):'—')+'</div></div>'+
        '<div class="pg"><div class="l">Mkt value</div><div class="v num">'+mv+'</div></div>'+
        '<div class="pg"><div class="l">At risk</div><div class="v num down">'+(risk!=null?'-$'+risk.toFixed(2):'—')+'</div></div>'+
        '</div></div>';
    }).join('');
  } else pe.innerHTML='<div class="empty">No open positions</div>';

  // trades
  const rec=sett.slice(-60).reverse(), te=$('trades');
  if(rec.length){
    te.innerHTML=rec.map(s=>{
      const k=s.ticker+'|'+s.ts, ex=expanded.has(k), dt=new Date(s.ts);
      const ts=et(dt,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true});
      return '<div class="tr" data-k="'+k+'">'+
        '<span class="badge '+(s.won?'badge-w':'badge-l')+'">'+(s.won?'W':'L')+'</span>'+
        '<div class="tr-mid"><div class="tr-ser">'+s.series+'</div>'+
        '<div class="tr-time">'+ts+' ET</div></div>'+
        '<span class="tr-side">'+s.side.toUpperCase()+'</span>'+
        '<span class="tr-pnl num '+cls(s.pnl)+'">'+signed(s.pnl)+'</span>'+
        (ex?'<div class="tr-det"><span>Ticker <b>'+s.ticker+'</b></span>'+
          '<span>Cost <b>$'+s.cost.toFixed(2)+'</b></span>'+
          '<span>Payout <b class="up">$'+(s.rev!=null?s.rev.toFixed(2):'—')+'</b></span>'+
          '<span>Fee <b class="down">-$'+(s.fee!=null?s.fee.toFixed(2):'—')+'</b></span>'+
          '<span>Net <b class="'+cls(s.pnl)+'">'+signed(s.pnl)+'</b></span>'+
          '<span>Side <b>'+s.side.toUpperCase()+'</b></span></div>':'')+
        '</div>';
    }).join('');
    te.querySelectorAll('.tr').forEach(r=>r.addEventListener('click',()=>{
      const k=r.dataset.k; expanded.has(k)?expanded.delete(k):expanded.add(k);
      if(navigator.vibrate) navigator.vibrate(2);
      if(last) render(last);
    }));
  } else te.innerHTML='<div class="empty">No settled trades yet</div>';

  $('foot').textContent='Updated '+et(new Date(d.ts),{hour:'numeric',minute:'2-digit',hour12:true})+' ET · refreshes every 30s';
}

/* ── refresh + staleness ────────────────────────────────────────── */
let lastOk=Date.now();
async function refresh(){
  try{
    const r=await fetch('/api/data',{cache:'no-store'});
    if(!r.ok) throw new Error('HTTP '+r.status);
    last=await r.json(); lastOk=Date.now();
    $('stale').style.display='none';
    if(!scrubbing) render(last);
  }catch(e){
    // Never leave stale numbers on screen looking live — that was the old bug.
    const age=Math.round((Date.now()-lastOk)/60000);
    $('stale').style.display='block';
    $('stale').textContent='Data may be stale — last refresh failed'+(age>0?' ('+age+'m old)':'');
  }
}

/* pull to refresh */
let py=0,pulling=false;
addEventListener('touchstart',e=>{ if(scrollY<=0){py=e.touches[0].clientY;pulling=true;} },{passive:true});
addEventListener('touchmove',e=>{
  if(!pulling) return;
  const dy=e.touches[0].clientY-py;
  if(dy>0&&scrollY<=0){ const h=Math.min(70,dy*.5); $('ptr').style.height=h+'px';
    $('ptr').querySelector('svg').style.opacity=Math.min(1,h/45); }
},{passive:true});
addEventListener('touchend',()=>{
  if(!pulling) return; pulling=false;
  const h=parseFloat($('ptr').style.height)||0;
  if(h>42){ $('ptr').classList.add('ptr-spin'); if(navigator.vibrate)navigator.vibrate(6);
    refresh().finally(()=>{ $('ptr').classList.remove('ptr-spin'); $('ptr').style.height='0px';
      $('ptr').querySelector('svg').style.opacity=0; }); }
  else { $('ptr').style.height='0px'; $('ptr').querySelector('svg').style.opacity=0; }
});

/* live countdown ticks without refetching */
setInterval(()=>{ if(last&&!scrubbing&&(last.positions||[]).length) render(last); },1000);
refresh();
tickClose(); setInterval(tickClose,1000);
setInterval(refresh,30000);
document.addEventListener('visibilitychange',()=>{ if(!document.hidden) refresh(); });
</script>
</body>
</html>"""

@app.route("/")
def index():
    if DASH_TOKEN and request.args.get("t"):
        # Token already validated upstream. Set the cookie, then bounce to a clean
        # URL so it stops living in history, screenshots and the address bar.
        resp = redirect("/")
        resp.set_cookie("dash_token", DASH_TOKEN, max_age=90 * 86400,
                        httponly=True, samesite="Lax", secure=HOSTED)
        return resp
    return make_response(HTML)

if __name__ == "__main__":
    os.chdir(BASE)
    _ensure_key()
    port = int(os.environ.get("PORT", 8765))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    print(f"Dashboard → http://{host}:{port}/")
    app.run(host=host, port=port, debug=False)
