#!/usr/bin/env python3
"""Kalshi trader dashboard — Render hosted.
Env: KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY (base64 PEM), PORT (set by Render)
"""
import ast, base64, gzip, hmac, math, os, threading, time
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
_cache_lock = threading.Lock()
_refreshing = set()

# How many multiples of its own TTL a value may age while background refreshes keep
# failing. Past this the request blocks and refetches, so a persistently broken source
# surfaces as slowness and an error rather than silently serving old numbers as live.
STALE_CEILING = 10


def _refresh_async(key, fn):
    """Refresh one key behind the request that found it stale."""
    def run():
        try:
            v = fn()
            with _cache_lock:
                _cache[key] = {"t": time.time(), "v": v}
        except Exception as e:                      # kalshi() swallows its own; be safe
            _last_err[key] = str(e)[:120]
        finally:
            with _cache_lock:
                _refreshing.discard(key)
    threading.Thread(target=run, daemon=True).start()


def cached(key, ttl, fn):
    """Stale-while-revalidate.

    The client polls every 30s but settlements carry a 120s TTL, so every fourth poll
    used to pay the full refetch — seconds of pagination — while the browser sat on a
    pending request. A pull-to-refresh that landed on it spun for that whole time for
    no reason, because a perfectly good value was already in hand.

    Now an expired-but-usable value is returned immediately and refreshed behind the
    request. Only one refresh per key runs at a time, so N concurrent viewers cause one
    refetch, not N. Replacement semantics are unchanged: whatever fn() returns wins,
    exactly as before.
    """
    now = time.time()
    with _cache_lock:
        ent = _cache.get(key)
        if ent is not None:
            age = now - ent["t"]
            if age < ttl:
                return ent["v"]
            if age < ttl * STALE_CEILING:
                start = key not in _refreshing
                if start:
                    _refreshing.add(key)
                stale = ent["v"]
                fresh_needed = False
            else:
                start, stale, fresh_needed = False, None, True
        else:
            start, stale, fresh_needed = False, None, True
    if not fresh_needed:
        if start:
            _refresh_async(key, fn)
        return stale
    v = fn()
    with _cache_lock:
        _cache[key] = {"t": time.time(), "v": v}
    return v

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
    live = live_series()
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
                series = s.get("ticker", "").split("-")[0]
                # Strategy stats count ONLY the series the trader actually trades.
                # Non-strategy rows are still emitted (they moved the balance) and the
                # UI keeps them out of WR / trades / P&L via this flag.
                is_strat = (series in live if live is not None
                            else series.endswith("15M"))
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
                    # Needed for break-even. Without a contract count the page can
                    # only compare a win rate to a flat 92%, which is the exact
                    # fee-blind comparison that reads green at a real loss.
                    "con":    round(float(s.get("yes_count_fp", 0) or 0)
                                    + float(s.get("no_count_fp", 0) or 0), 2),
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
        "strike":     _fstrike(m),
    }

def _fstrike(m):
    try:
        v = float(m.get("floor_strike") or 0)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None

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
            # ABS is deliberate. Kalshi's `position` is signed (negative = long NO),
            # and the fills fallback inherits that sign, so a NO position arrived here
            # as -38. Direction is carried by `side`, not by the sign of the count, so
            # a signed count double-encoded it and inverted every derived figure:
            # "Mkt value" rendered $-38.00 and "If win" rendered +$-4.04 on a WINNING
            # position. Magnitude here, direction in `side`.
            contracts = abs(b["contracts"] or int(p.get("position") or 0))
            side = b["side"] or "yes"
            # Quote the side actually held. Showing the YES book for a NO position
            # displays ~9c against a 91c entry, and v5.16 trades NO about half the time.
            ask = mkt.get("no_ask") if side == "no" else mkt.get("yes_ask")
            bid = mkt.get("no_bid") if side == "no" else mkt.get("yes_bid")
            # Live cushion: how far spot sits from the strike, in units of how far it
            # can still travel before close. Same definition as the trader's z_value().
            strike = mkt.get("strike")
            sv = get_spot_vol(ticker.split("-")[0])
            secs = 0
            if mkt.get("close_time"):
                try:
                    secs = max(0.0, (datetime.fromisoformat(
                        mkt["close_time"].replace("Z", "+00:00"))
                        - datetime.now(timezone.utc)).total_seconds())
                except ValueError:
                    secs = 0
            z = None
            if strike and sv and sv["sigma_bp"] > 0 and secs > 0 and sv["spot"] > 0:
                denom = (sv["sigma_bp"] / 1e4) * math.sqrt(secs / 60.0)
                if denom > 0:
                    z = (1.0 if side == "yes" else -1.0) * \
                        ((sv["spot"] - strike) / sv["spot"]) / denom
            out.append({"ticker":     ticker,
                        "contracts":  contracts,
                        "side":       side,
                        "entry":      round(b["entry"] * 100, 1) if b["entry"] else None,
                        "cost":       b["cost"],
                        "fee":        b["fee"],
                        "ask":        ask,
                        "bid":        bid,
                        "close_time": mkt.get("close_time", ""),
                        "title":      mkt.get("title", ""),
                        "strike":     strike,
                        "spot":       round(sv["spot"], 4) if sv else None,
                        "sigma_bp":   round(sv["sigma_bp"], 2) if sv else None,
                        "z":          round(z, 3) if z is not None else None})
        return out
    return cached("pos", 15, _f)

def live_series():
    """SERIES_LIST from the trader, by AST. Never hardcode a strategy constant here.

    The strategy filter used to be `ticker.endswith("15M")`, which counts every 15M
    series the account has ever touched — including KXHYPE15M, KXNEAR15M and
    KXWTI15M, which are shadow/retired and which the bot places no orders in. That is
    307 settlements and -$245.05 of P&L attributed to a strategy that did not make
    those trades. scripts/reconcile.py has filtered on SERIES_LIST for exactly this
    reason since it was written; the dashboard did not.

    Falls back to None on any read failure, and the caller then keeps the old
    endswith("15M") behaviour rather than silently showing an empty book.
    """
    def _f():
        try:
            tree = ast.parse(TRADER.read_text())
        except Exception as e:
            _last_err["series"] = f"parse: {str(e)[:80]}"
            return None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SERIES_LIST":
                    try:
                        return list(ast.literal_eval(node.value))
                    except (ValueError, TypeError) as e:
                        _last_err["series"] = f"literal_eval: {str(e)[:80]}"
                        return None
        _last_err["series"] = "SERIES_LIST not found"
        return None
    return cached("series", 300, _f)


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

def live_const(name, default=None):
    """Any module-level literal from the trader, by AST. Same rule as live_series:
    never hardcode a strategy constant here, or the two drift silently."""
    def _f():
        try:
            tree = ast.parse(TRADER.read_text())
        except Exception as e:
            _last_err[name] = f"parse: {str(e)[:80]}"
            return default
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    try:
                        return ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        return default
        return default
    return cached(f"const:{name}", 300, _f)


def get_spot_vol(series):
    """Live spot and trailing-60min sigma for one series, from Coinbase.

    Deliberately the SAME quantity the trader gates on — `z_value()` uses this sigma
    over this spot against the market's floor_strike. Computing a *different* sigma
    here would render a number that looks authoritative next to an open position and
    quietly means something else than the z the bot acted on.
    """
    pair = (live_const("COINBASE_PAIR") or {}).get(series)
    if not pair:
        return None
    def _f():
        try:
            r = requests.get(
                f"https://api.exchange.coinbase.com/products/{pair}/candles"
                f"?granularity=60", timeout=8)
            if r.status_code != 200:
                return None
            rows = r.json()
        except Exception as e:
            _last_err["spot"] = f"{series}: {str(e)[:70]}"
            return None
        if not isinstance(rows, list) or len(rows) < 12:
            return None
        closes = [float(c[4]) for c in sorted(rows, key=lambda x: x[0])[-61:]]
        rets = [math.log(closes[i] / closes[i - 1])
                for i in range(1, len(closes)) if closes[i - 1] > 0]
        if len(rets) < 10:
            return None
        mu = sum(rets) / len(rets)
        sigma = (sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) ** 0.5
        return {"spot": closes[-1], "sigma_bp": sigma * 1e4}
    return cached(f"spot:{series}", 45, _f)


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
<meta name="mobile-web-app-capable" content="yes">
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


def _pts(ts):
    """Kalshi returns variable sub-second precision; pad the fraction to 6 digits."""
    import re
    ts = (ts or "").replace("Z", "+00:00")
    ts = re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m.group(1).ljust(6, "0"), ts)
    return datetime.fromisoformat(ts)


def _pct_rank(v, xs):
    """Fraction of xs at or below v. Empty history ranks everything mid."""
    return (sum(1 for x in xs if x <= v) / len(xs)) if xs else 0.5


def get_anomalies(sett, pos, balance, health):
    """Things outside THIS account's own historical norms. [] means all clear.

    Thresholds are percentiles of the account's own history, not typed-in numbers.
    A hardcoded "bad day" goes stale the moment sizing changes — the same defect the
    trader's DAILY_LOSS_LIMIT_BETS comment documents — whereas a percentile survived
    the $25 -> $35 change without an edit. The dollar-denominated checks below read
    their limits from the trader by AST for the same reason.

    Silence is the healthy state: this returns nothing at all on a normal day, so
    anything rendered is by definition worth a look.
    """
    out = []
    now = datetime.now(timezone.utc)
    def add(sev, key, msg, detail=""):
        out.append({"sev": sev, "key": key, "msg": msg, "detail": detail})

    # 1. Settlement stall. Normal is close +5s; this is what jammed the book on
    #    2026-08-28 for 4.2h before #222, and it is invisible in every other panel.
    stalled = []
    for p_ in pos or []:
        if not p_.get("close_time"):
            continue
        try:
            lag = (now - _pts(p_["close_time"])).total_seconds() / 60.0
        except ValueError:
            continue
        if lag > 3:
            stalled.append((p_["ticker"], lag))
    if stalled:
        worst = max(l for _, l in stalled)
        add("crit" if worst > 30 else "warn", "settle",
            f"{len(stalled)} position{'s' if len(stalled)>1 else ''} closed but unsettled",
            f"worst {worst:.0f} min past close — normal is 5s. Kalshi-side; slots are "
            f"discounted so this is not blocking entries.")

    # 2/3. Trader liveness.
    h = health or {}
    age = h.get("last_success_age_min")
    if isinstance(age, (int, float)) and age > 20:
        add("crit" if age > 45 else "warn", "stale",
            f"no successful run in {age:.0f} min", "cadence is ~15.3 min")
    if (h.get("consec_failures") or 0) >= 2:
        add("crit", "fails", f"{h['consec_failures']} consecutive run failures")

    # 4. Trading has stopped without a halt. 1.8h is the largest fill gap ever seen.
    if sett:
        try:
            newest = max(_pts(x["ts"]) for x in sett if x.get("ts"))
            quiet = (now - newest).total_seconds() / 60.0
            if quiet > 108 and not pos:
                add("warn", "quiet", f"nothing settled in {quiet/60:.1f}h",
                    "largest gap ever observed is 1.8h")
        except ValueError:
            pass

    # 5. Today's P&L against the account's own daily distribution.
    byday = {}
    for x in sett:
        if not x.get("ts"):
            continue
        try:
            d = _pts(x["ts"]).astimezone(ET).date()
        except ValueError:
            continue
        byday.setdefault(d, []).append(x)
    today = datetime.now(ET).date()
    hist = [sum(v["pnl"] for v in rows) for d, rows in byday.items() if d != today]
    if today in byday and len(hist) >= 10:
        tp = sum(v["pnl"] for v in byday[today])
        r = _pct_rank(tp, hist)
        if r <= 0.10:
            add("warn", "day", f"today ${tp:+.2f} is worse than {(1-r)*100:.0f}% of days",
                f"{sum(1 for x in hist if x < tp)} day(s) in history were worse")

    # 6. Trailing-24h realised loss against the emergency brake.
    bet = live_const("FLAT_BET_DOLLARS")
    bets = live_const("DAILY_LOSS_LIMIT_BETS")
    if isinstance(bet, (int, float)) and isinstance(bets, (int, float)):
        lim = bet * bets
        cut = now.timestamp() - 86400
        p24 = 0.0
        for x in sett:
            try:
                if _pts(x["ts"]).timestamp() >= cut:
                    p24 += x["pnl"]
            except (ValueError, KeyError):
                continue
        if p24 < -0.6 * lim:
            add("crit" if p24 < -0.85 * lim else "warn", "brake",
                f"trailing-24h ${p24:+.2f} vs ${lim:.0f} halt",
                f"{100*abs(p24)/lim:.0f}% of the daily loss limit")

    # 7. Headroom to the hard stop, in losses rather than dollars.
    stop = live_const("STOP_BALANCE")
    if isinstance(stop, (int, float)) and isinstance(bet, (int, float)) and balance and bet > 0:
        left = (balance - stop) / bet
        if left < 15:
            add("crit" if left < 8 else "warn", "room",
                f"{left:.0f} losses of headroom to the ${stop:.0f} stop")

    # 8. Sizing drift. Trade-weighted and dollar-weighted win rates diverging is the
    #    signature of the pre-#151 defect: winning 92% of trades while losing money.
    recent = sorted([x for x in sett if x.get("ts")], key=lambda x: x["ts"])[-200:]
    tc = sum(x["cost"] for x in recent)
    if len(recent) >= 60 and tc > 0:
        tw = 100.0 * sum(1 for x in recent if x["won"]) / len(recent)
        dw = 100.0 * sum(x["cost"] for x in recent if x["won"]) / tc
        if abs(dw - tw) > 2.0:
            add("warn", "drift",
                f"$-weighted WR {dw:.1f}% vs trade WR {tw:.1f}%",
                "losers and winners are being sized differently again")

    return sorted(out, key=lambda a: {"crit": 0, "warn": 1, "info": 2}[a["sev"]])


# The payload is ~540 KB of mostly-repetitive JSON and the client polls it every 30s,
# so an open phone tab was pulling ~65 MB/hr uncompressed. Flask does not compress
# anything by default and Render does not do it for us. gzip takes 540 KB -> ~50 KB.
# Only /api/data is worth compressing: the HTML is served once per load and the CPU
# cost is paid on every request.
GZIP_MIN_BYTES = 4096


@app.after_request
def _gzip(resp):
    if (resp.status_code != 200
            or "gzip" not in request.headers.get("Accept-Encoding", "").lower()
            or resp.direct_passthrough
            or resp.headers.get("Content-Encoding")
            or not resp.mimetype.startswith("application/json")):
        return resp
    data = resp.get_data()
    if len(data) < GZIP_MIN_BYTES:
        return resp
    resp.set_data(gzip.compress(data, 6))
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Content-Length"] = str(len(resp.get_data()))
    # Same URL can come back gzipped or not depending on the request header.
    resp.headers["Vary"] = "Accept-Encoding"
    return resp


@app.route("/api/data")
def api_data():
    bal  = get_balance()
    sett = get_settlements()
    pos  = get_positions()
    hea  = get_health()
    return jsonify({
        "balance":     bal,
        "settlements": sett,
        "deposits":    get_deposits(),
        "positions":   pos,
        "health":      hea,
        "anomalies":   get_anomalies(sett, pos, bal, hea),
        "bet":         live_const("FLAT_BET_DOLLARS"),
        "stop":        live_const("STOP_BALANCE"),
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
<meta name="mobile-web-app-capable" content="yes">
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
  max-width:620px;margin:0 auto;padding:0 16px calc(48px + var(--safe-b));
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
.chg-dot{color:#2E333A;font-weight:700;margin:0 1px}
.hero-chg{flex-wrap:wrap;row-gap:2px}

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

/* ── tablet ──────────────────────────────────────────────────────── */
/* Width, chart height, stat columns and type scale are all owned by the SIZE
   block at the end of this sheet. Only rules that are still the SOLE definition
   of something remain here. The .cols / .col-left / .col-right rules that used
   to live in this block went out with the two-column body they styled — tabs
   and .sect replaced it, so they had been dead CSS since #226. */
@media (min-width:1080px){
  .stat{padding:16px 15px}
  .stat-val{font-size:22px}
  .duo>.card+.card{margin-top:0}
  #blackout{margin:0 -32px 4px}
}

/* ── view mode / density ─────────────────────────────────────────── */
.modebar{display:flex;align-items:center;justify-content:flex-end;gap:8px;padding-top:14px}
body.simple [data-full]{display:none !important}
body.dense .stat{padding:9px 10px}
body.dense .stat-val{font-size:16px}
body.dense .tr{padding:7px 13px}
body.dense .pos{padding:11px 13px}
body.dense h3{margin:14px 0 7px}

/* ── anomaly strip ───────────────────────────────────────────────── */
#anoms{display:flex;flex-direction:column;gap:7px;margin-top:12px}
.anom{display:flex;gap:10px;align-items:flex-start;padding:11px 13px;border-radius:13px;
  border:1px solid var(--line);background:var(--s1);animation:fade .3s ease}
.anom .ic{width:7px;height:7px;border-radius:50%;flex:0 0 auto;margin-top:5px}
.anom .am{font-size:12.5px;font-weight:700;letter-spacing:-.01em}
.anom .ad{font-size:11px;color:var(--dim);margin-top:3px;line-height:1.45;font-weight:500}
.anom.crit{border-color:rgba(255,69,58,.32);background:rgba(255,69,58,.07)}
.anom.crit .ic{background:var(--down);animation:breathe 1s ease-in-out infinite}
.anom.crit .am{color:var(--down)}
.anom.warn{border-color:rgba(255,163,24,.28);background:rgba(255,163,24,.06)}
.anom.warn .ic{background:var(--warn)}
.anom.warn .am{color:var(--warn)}
.anom-ok{font-size:11px;color:var(--dimmer);font-weight:650;letter-spacing:.4px;
  text-align:center;padding:7px 0}

/* ── position drama: spot vs strike ──────────────────────────────── */
.drama{margin-top:11px;padding-top:11px;border-top:1px solid var(--line)}
.drama-top{display:flex;justify-content:space-between;align-items:baseline;
  font-size:10px;color:var(--dimmer);text-transform:uppercase;letter-spacing:.8px;
  font-weight:750;margin-bottom:9px}
.drama-z{font-size:12.5px;font-weight:800;letter-spacing:-.02em}
.track{position:relative;height:28px;border-radius:8px;background:var(--s2);
  border:1px solid var(--line);overflow:hidden}
.track .mid{position:absolute;left:50%;top:0;bottom:0;width:1px;
  background:rgba(255,255,255,.3)}
.track .midl{position:absolute;left:50%;top:2px;font-size:8.5px;color:var(--dimmer);
  transform:translateX(-50%);font-weight:800;letter-spacing:.5px}
.track .fill{position:absolute;top:0;bottom:0;opacity:.2}
.track .mk{position:absolute;top:3px;bottom:3px;width:3px;border-radius:2px;
  transition:left .5s cubic-bezier(.32,.72,0,1)}
.drama-sub{display:flex;justify-content:space-between;font-size:10.5px;
  color:var(--dim);margin-top:7px;font-weight:600}

/* ── trade filters ───────────────────────────────────────────────── */
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 9px}
.chip{background:var(--s1);border:1px solid var(--line);color:var(--dim);
  font-size:10.5px;font-weight:700;padding:5px 10px;border-radius:14px;cursor:pointer;
  font-family:inherit;letter-spacing:.3px;transition:all .18s}
.chip.on{background:var(--s3);color:var(--tx);border-color:var(--line-2)}

/* ── what-if / streak ────────────────────────────────────────────── */
.wf-row{display:flex;align-items:center;gap:11px;margin-top:9px}
.wf-row input[type=range]{flex:1;accent-color:var(--info);height:3px}
.wf-val{font-size:12.5px;font-weight:800;min-width:42px;text-align:right}
.streak{display:flex;gap:9px;margin-top:9px}
.streak .sk2{flex:1;background:var(--s2);border-radius:11px;padding:10px;text-align:center}
.streak .sk2 .n{font-size:19px;font-weight:750;letter-spacing:-.03em}
.streak .sk2 .t{font-size:9px;color:var(--dimmer);text-transform:uppercase;
  letter-spacing:.8px;font-weight:750;margin-top:4px}

/* ── high-water mark ─────────────────────────────────────────────── */
@keyframes hwm{0%{transform:scale(1)}35%{transform:scale(1.055)}100%{transform:scale(1)}}
.hwm{animation:hwm .85s cubic-bezier(.32,.72,0,1);color:var(--up) !important}
.hwm-b{display:inline-flex;align-items:center;gap:4px;font-size:9.5px;font-weight:800;
  color:var(--up);background:rgba(0,209,129,.11);border:1px solid rgba(0,209,129,.25);
  padding:3px 8px;border-radius:12px;letter-spacing:.6px;margin-left:7px}

/* ══ REDESIGN ═══════════════════════════════════════════════════════
   Three problems this fixes:
   1. Every card had a 1px border, so sixteen outlined boxes read as sixteen
      equally important things. Hierarchy now comes from elevation and spacing,
      and a border is reserved for something that genuinely needs bounding.
   2. Chrome was brighter than data — labels and rules competed with numbers.
      Labels go dimmer, numbers go brighter.
   3. Simple was not simple. It is now four things; everything else is a tab.
   ═══════════════════════════════════════════════════════════════════ */
:root{
  --e1:#0C0E11; --e2:#14171C; --e3:#1C2026;
  --sp:8px;
  --sh:0 1px 2px rgba(0,0,0,.5), 0 6px 18px -8px rgba(0,0,0,.7);
  --r-lg:18px; --r-md:13px; --r-sm:9px;
}
body{max-width:640px;padding-bottom:calc(96px + var(--safe-b))}

/* elevation replaces outlines */
.card,.stat{background:var(--e1);border:none;box-shadow:var(--sh);border-radius:var(--r-lg)}
.stat{border-radius:var(--r-md)}
.seg{background:var(--e1);border:none;box-shadow:var(--sh)}
.seg .pill{background:var(--e3)}
.chip{background:var(--e1);border:1px solid transparent;box-shadow:var(--sh)}
.chip.on{background:var(--e3);border-color:rgba(255,255,255,.10);color:var(--tx)}

/* chrome recedes, data advances */
.stat-lbl,.pos-full,.pnote,.drama-top,.tr-time{color:#454B54}
.stat-val,.pos-tick,.tr-pnl{color:var(--tx)}
h3{font-size:11px;letter-spacing:1.1px;color:#565C66;text-transform:uppercase;font-weight:800}
h4{font-size:10.5px;letter-spacing:1px;color:#565C66;text-transform:uppercase;font-weight:800}

/* chart: lighter stroke, calmer fill */
#pathLine{stroke-width:1.6}
#pathArea{opacity:.62}
body.simple .chart-wrap{margin-top:calc(var(--sp)*2)}

/* ══ top bar + gear ═════════════════════════════════════════════════ */
.topbar{display:flex;align-items:center;justify-content:space-between;
  padding:calc(var(--sp)*1.5) 0 0}
.brand{font-size:12px;font-weight:800;letter-spacing:1.6px;color:#454B54;
  text-transform:uppercase}
.gear{width:34px;height:34px;border-radius:50%;background:var(--e1);border:none;
  box-shadow:var(--sh);color:var(--dim);cursor:pointer;display:flex;align-items:center;
  justify-content:center;font-size:15px;transition:transform .25s,color .2s}
.gear:active{transform:scale(.92)}

.sheet-bg{position:fixed;inset:0;background:rgba(0,0,0,.62);backdrop-filter:blur(7px);
  opacity:0;pointer-events:none;transition:opacity .28s;z-index:80}
.sheet-bg.on{opacity:1;pointer-events:auto}
.sheet{position:fixed;left:0;right:0;bottom:0;z-index:81;background:var(--e2);
  border-radius:22px 22px 0 0;padding:calc(var(--sp)*2.5) calc(var(--sp)*2.5)
  calc(var(--sp)*4 + var(--safe-b));transform:translateY(102%);
  transition:transform .34s cubic-bezier(.32,.72,0,1);max-width:640px;margin:0 auto}
.sheet.on{transform:translateY(0)}
.sheet .grab{width:36px;height:4px;border-radius:3px;background:var(--e3);
  margin:0 auto calc(var(--sp)*2.5)}
.sheet h5{font-size:10px;letter-spacing:1.1px;color:#565C66;text-transform:uppercase;
  font-weight:800;margin:calc(var(--sp)*2) 0 var(--sp)}
.sheet-row{display:flex;align-items:center;justify-content:space-between;gap:12px}

/* ══ bottom tabs (Full only) ════════════════════════════════════════ */
#tabs{position:fixed;left:0;right:0;bottom:0;z-index:60;display:none;
  background:rgba(10,11,13,.94);backdrop-filter:blur(10px);will-change:transform;
  border-top:1px solid rgba(255,255,255,.06);
  padding:7px 10px calc(7px + var(--safe-b));max-width:640px;margin:0 auto}
body.full #tabs{display:flex}
#tabs button{flex:1;background:none;border:none;color:#4C525B;font-family:inherit;
  font-size:9.5px;font-weight:800;letter-spacing:.7px;text-transform:uppercase;
  padding:7px 2px;cursor:pointer;border-radius:11px;min-height:44px;
  display:flex;flex-direction:column;align-items:center;gap:4px;transition:color .2s}
#tabs button .ti{font-size:15px;line-height:1;opacity:.9}
#tabs button.on{color:var(--up)}
body.full .sect{display:none}
body.full .sect.on{display:block;animation:secIn .3s cubic-bezier(.32,.72,0,1)}
@keyframes secIn{from{opacity:0;transform:translateY(7px)}to{opacity:1;transform:none}}

/* ══ sticky mini header ═════════════════════════════════════════════ */
#mini{position:fixed;top:0;left:0;right:0;z-index:55;max-width:640px;margin:0 auto;
  padding:9px 20px;background:rgba(10,11,13,.94);
  backdrop-filter:blur(10px);will-change:transform;border-bottom:1px solid rgba(255,255,255,.06);
  display:flex;align-items:center;justify-content:space-between;
  transform:translateY(-110%);transition:transform .3s cubic-bezier(.32,.72,0,1)}
#mini.on{transform:translateY(0)}
#mini .mb{font-size:15.5px;font-weight:750;letter-spacing:-.02em}
#mini .mc{font-size:12.5px;font-weight:650}

/* ══ Simple: compact positions ══════════════════════════════════════ */
.cpos{display:flex;align-items:center;gap:11px;padding:13px 15px}
.cpos+.cpos{border-top:1px solid rgba(255,255,255,.05)}
.cpos .cs{font-size:13px;font-weight:750;letter-spacing:-.01em;width:52px;flex:0 0 auto}
.cpos .cd{font-size:10.5px;color:var(--dim);font-weight:650;width:58px;flex:0 0 auto}
.cpos .cbar{flex:1;height:5px;border-radius:3px;background:var(--e3);overflow:hidden;position:relative}
.cpos .cbar i{position:absolute;top:0;bottom:0;left:0;border-radius:3px}
.cpos .ct{font-size:11.5px;font-weight:750;width:46px;text-align:right;flex:0 0 auto;
  font-variant-numeric:tabular-nums}

/* ══ "more" reveal (Simple) ═════════════════════════════════════════ */
.more{width:100%;background:none;border:none;color:#565C66;font-family:inherit;
  font-size:11px;font-weight:800;letter-spacing:1px;text-transform:uppercase;
  padding:calc(var(--sp)*2.5) 0;cursor:pointer;display:flex;align-items:center;
  justify-content:center;gap:6px;min-height:44px}
.more .cv{transition:transform .3s}
body.moreopen .more .cv{transform:rotate(180deg)}
body.simple #moreWrap{display:none}
body.simple.moreopen #moreWrap{display:block;animation:secIn .32s cubic-bezier(.32,.72,0,1)}

/* ══ win-rate vs break-even bar ═════════════════════════════════════ */
.wrbar{position:relative;height:32px;border-radius:var(--r-sm);background:var(--e2);
  overflow:hidden;margin-top:10px}
.wrbar .wf{position:absolute;left:0;top:0;bottom:0;border-radius:var(--r-sm);
  transition:width .6s cubic-bezier(.32,.72,0,1)}
.wrbar .be{position:absolute;top:0;bottom:0;width:2px;background:var(--tx);
  box-shadow:0 0 0 1px rgba(0,0,0,.6)}
.wrbar{margin-top:22px}
.wrbar .bel{position:absolute;top:-16px;font-size:8.5px;font-weight:800;color:var(--dim);
  transform:translateX(-50%);letter-spacing:.5px;white-space:nowrap}
.wrbar .wv{position:absolute;left:9px;top:50%;transform:translateY(-50%);
  font-size:13px;font-weight:800;letter-spacing:-.02em}

/* ══ trade strip ════════════════════════════════════════════════════ */
.strip{display:flex;gap:2px;align-items:flex-end;height:26px;margin-top:9px}
.strip i{flex:1;border-radius:1.5px;min-width:2px;transition:opacity .2s}
.strip i:hover{opacity:.6}

/* ══ series bars ════════════════════════════════════════════════════ */
.sbar{position:relative;height:5px;border-radius:3px;background:var(--e2);
  flex:1;overflow:hidden;margin:0 9px}
.sbar i{position:absolute;top:0;bottom:0;border-radius:3px}

/* ══ chart-tab overview ════════════════════════════════════════════ */
.ovgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.ov{text-align:center}
.ovl{font-size:9px;color:#454B54;text-transform:uppercase;letter-spacing:.9px;
  font-weight:800;margin-bottom:6px;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.ovv{font-size:17px;font-weight:730;letter-spacing:-.025em}
.ovsep{height:1px;background:rgba(255,255,255,.06);margin:15px 0 12px}

/* ══ chart axis ═════════════════════════════════════════════════════ */
.yax{position:absolute;right:2px;top:6px;bottom:6px;display:flex;flex-direction:column;
  justify-content:space-between;align-items:flex-end;pointer-events:none;
  font-size:9.5px;font-weight:700;color:#3E444D;font-variant-numeric:tabular-nums}

/* ══ hero compresses off the Chart tab ══════════════════════════════
   The balance is context everywhere but the subject only on Chart. At full size it
   was eating 40% of the viewport on every tab before any of that tab's content. */
body.full:not([data-tab="secChart"]) .hero{padding:calc(var(--sp)*2) 0 0}
body.full:not([data-tab="secChart"]) .hero-bal{font-size:clamp(28px,7vw,34px)}
body.full:not([data-tab="secChart"]) .hero-chg{font-size:12.5px;margin-top:5px}
body.full:not([data-tab="secChart"]) .health{margin-top:9px;transform:scale(.9)}

/* ══ denser tiles and rows ══════════════════════════════════════════ */
.tr{padding:9px 14px}
.pos-grid{gap:7px 10px}

/* ══ mode-specific visibility ═══════════════════════════════════════ */
body.full .more{display:none}
body.simple .hero{padding-top:calc(var(--sp)*2)}
body.simple .foot{margin-top:calc(var(--sp)*2)}

/* ══ motion ═════════════════════════════════════════════════════════ */
.card,.stat{animation:secIn .34s cubic-bezier(.32,.72,0,1) backwards}
.duo .card:nth-child(2){animation-delay:.05s}
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms !important;
    animation-iteration-count:1 !important;transition-duration:.01ms !important}
}

/* ══ RESPONSIVE ═════════════════════════════════════════════════════
   The redesign hardcoded max-width:640px, which overrode the desktop rules that
   already existed further up and left a phone column stranded in a black sea on a
   1500px screen. Layout is now genuinely per-device rather than one column scaled.

   Phone  : one column, bottom tabs, hero large on Chart and compressed elsewhere.
   Desktop: every section laid out at once. Tabs are a phone affordance — with this
            much width, paging between four sections is friction for no gain.
   Simple stays a narrow focused column at every size; widening it would defeat it.
   ═══════════════════════════════════════════════════════════════════ */
@media (min-width:760px){
  body{max-width:740px}
}

@media (min-width:1080px){
  body.full{max-width:1300px;padding:0 32px 40px}
  body.simple{max-width:780px}          /* Simple is meant to stay a single column */
  .hero{padding:34px 0 4px}
  .hero-bal{font-size:clamp(52px,4.4vw,66px)}
  .hero-chg{font-size:16px}

  /* every section visible; the bottom bar is for phones */
  body.full #tabs{display:none}
  body.full .sect{display:block;animation:none}
  body.full .grid{display:grid;grid-template-columns:1fr 1fr;gap:26px;align-items:start}
  body.full #secChart{grid-column:1/-1}
  body.full #secStats{grid-column:1/-1}
  body.full #secPos{grid-column:1;position:sticky;top:20px}
  body.full #secTrades{grid-column:2}
  body.full .sect>h3:first-child{margin-top:0}

  /* the hero is compressed on phones to buy room for the tab below it; on desktop
     nothing is below it, so the compression is pure loss */
  body.full:not([data-tab="secChart"]) .hero{padding:34px 0 4px}
  body.full:not([data-tab="secChart"]) .hero-bal{font-size:clamp(52px,4.4vw,66px)}
  body.full:not([data-tab="secChart"]) .hero-chg{font-size:16px}
  body.full:not([data-tab="secChart"]) .health{margin-top:14px;transform:none}

  .duo{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .duo>.card+.card{margin-top:0}
  .ovgrid{grid-template-columns:repeat(4,1fr);gap:14px}
  .ovv{font-size:20px}
  #blackout{margin:0 -32px 4px}
  #mini{max-width:1300px;padding:10px 32px}
  .more{display:none}                    /* nothing to reveal when all of it is shown */
  body.full #moreWrap{display:block}
}

@media (min-width:1500px){
  body.full{max-width:1460px}
}

/* phone: keep the tab bar clear of the last row */
@media (max-width:1079px){
  body{padding-bottom:calc(104px + var(--safe-b))}
}

/* ══ HOOD ═══════════════════════════════════════════════════════════
   The visual system chosen from the options lab. Robinhood's palette and
   restraint, carrying the instruments Robinhood has no pattern for.
   Options applied: 1 draw-in · 2 number roll · 3 range morph · 4 scrub ·
   5 stagger · 7 underline ranges · 8 flatter easing · 10 stats 4+expand ·
   12 no duplicate WR · 13 no footer · 14 taller chart · 15 line glow ·
   16 pulsing endpoint · 20 streak · 21 all-time high ·
   25 change glow · 26 consistent radii · 27 tighter tracking.
   ═══════════════════════════════════════════════════════════════════ */
:root{
  --up:#00C805; --down:#FF5000;
  --bg:#000; --e1:#0B0B0D; --e2:#131316; --e3:#1B1B1F;
  --line:#1C1C1E; --line-2:#242427;
  --tx:#FFFFFF; --dim:#8C8C8C; --dimmer:#5E5E63;
  --ease:cubic-bezier(.4,0,.2,1);          /* 8 · flatter shared curve */
  --r-lg:14px; --r-md:14px; --r-sm:14px;   /* 26 · one radius everywhere */
}
html,body{background:#000}
.card,.stat{background:var(--e1);box-shadow:none;border:1px solid var(--line)}
.seg{background:var(--e1);border:1px solid var(--line);box-shadow:none}
.seg .pill{background:var(--e3)}

/* 27 · tighter tracking on the figures that carry the page */
.hero-bal{letter-spacing:-.042em;font-weight:500}
.stat-val{letter-spacing:-.03em}

/* 25 · the change figure gets light behind it */
.hero-chg .up,.hero-chg [class*="up"]{text-shadow:0 0 20px rgba(0,200,5,.5)}

/* 14 · taller chart · 15 · the line carries its own glow */
#pathLine{stroke-width:2;filter:drop-shadow(0 0 7px rgba(0,200,5,.45))}
#pathArea{opacity:.85}

/* 16 · the endpoint is the page's one sign of life */
#livePing{animation:ping 2.4s ease-out infinite}

/* 7 · ranges underline instead of pill. movePill already slides the element;
       this only changes what the element looks like. */
#ranges,#modes{background:none;border:none;box-shadow:none;padding:0;gap:22px;
  border-radius:0}
#ranges button,#modes button{padding:8px 0;border-radius:0;font-weight:620;
  font-size:13.5px}
#ranges button.on,#modes button.on{color:var(--up)}
#ranges .pill,#modes .pill{top:auto;bottom:0;height:2px;border-radius:2px;
  background:var(--up);transition:left .34s var(--ease),width .34s var(--ease)}
.controls{gap:28px;padding-bottom:2px;border-bottom:1px solid var(--line)}

/* 10 · four tiles, the rest behind one control · 12 · no duplicate win rate */
.stat.gone{display:none}
.stat.x{display:none}
body.statx .stat.x{display:block}
.statmore{display:block;width:100%;background:none;border:none;color:var(--dim);
  font-family:inherit;font-size:12.5px;font-weight:620;padding:12px 0;cursor:pointer;
  min-height:44px}
.statmore:hover{color:var(--tx)}

/* 13 · no footer */
.foot{display:none}

/* 20 · the streak reads as a moment, not a row */
#streakCard .n{font-size:26px;letter-spacing:-.035em}

/* 21 · all-time high */
.hwm-b{background:rgba(0,200,5,.13);border-color:rgba(0,200,5,.3);color:var(--up);
  letter-spacing:.8px}
@keyframes hwm{0%{transform:scale(1)}30%{transform:scale(1.05)}100%{transform:scale(1)}}
.hwm{animation:hwm .9s var(--ease);color:var(--up) !important}

/* 5 · sections arrive in sequence rather than all at once */
@keyframes riseIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.hero,.chart-wrap,.controls,#overviewCard,.stats,.duo,.sect>h3,.sect>.card{
  animation:riseIn .46s var(--ease) backwards}
.chart-wrap{animation-delay:.05s}
.controls{animation-delay:.10s}
#overviewCard{animation-delay:.15s}
.stats{animation-delay:.20s}
.duo{animation-delay:.25s}
.duo+.duo{animation-delay:.30s}

/* everything shares one curve */
.seg .pill,.bar i,.track .f,.wrbar .wf,.cbar i,.mk,.tr,.pos,.stat,.card{
  transition-timing-function:var(--ease)}

@media (prefers-reduced-motion:reduce){
  .hero,.chart-wrap,.controls,#overviewCard,.stats,.duo,.sect>h3,.sect>.card{
    animation:none}
  #livePing{animation:none}
}

/* ══ SIZE — one authority, phone first ══════════════════════════════
   Chart height had been set in THIRTEEN places across three overlapping
   media-query systems, and the stat grid asked for six columns while only four
   tiles were visible — leaving two dead cells on every desktop. Every one of
   those declarations was removed; this block is now the only thing that sizes
   the chart, the stat grid and the range control. Add sizes HERE or they will
   fight something.
   ═══════════════════════════════════════════════════════════════════ */

/* ── phone (iPhone portrait is the base case, not an afterthought) ── */
.chart-wrap{height:190px;margin:14px -6px 0}
body.simple .chart-wrap{height:128px}
body.dense .chart-wrap{height:136px}

.stats{grid-template-columns:repeat(2,1fr);gap:8px;margin:18px 0 4px}
.ovgrid{grid-template-columns:repeat(2,1fr);gap:12px}
.pos-grid{gap:9px 8px}
.chips{gap:5px}

/* Eight range buttons do not fit across 375px. Scroll them instead of letting
   the page scroll sideways, and keep the sliding underline working by leaving
   the segment itself as the scroll container. */
.controls{display:flex;align-items:flex-end;justify-content:flex-start;
  gap:12px;margin-top:14px}
#ranges{flex:1 1 auto;min-width:0;overflow-x:auto;overflow-y:hidden;
  scrollbar-width:none;-webkit-overflow-scrolling:touch;gap:15px}
#ranges::-webkit-scrollbar{display:none}
#ranges button{flex:0 0 auto}
#modes{flex:0 0 auto}

.hero{padding:22px 0 4px}
.hero-bal{font-size:clamp(38px,11.5vw,50px)}
.hero-chg{font-size:14px}
h3{margin:18px 0 3px}
.col{padding:4px 0 18px}
.col+.col{border-left:none;border-top:1px solid var(--line);padding-top:14px}
.duo{display:grid;grid-template-columns:1fr;gap:10px}
.pos-grid{grid-template-columns:repeat(3,1fr)}
/* tap targets */
#ranges button,#modes button,#tabs button,.chip,.more,.statmore,.gear{min-height:44px}
#ranges button,#modes button{display:flex;align-items:center}

@media (min-width:600px){
  .chart-wrap{height:238px}
  body.simple .chart-wrap{height:150px}
  .stats{grid-template-columns:repeat(4,1fr);gap:9px}
  .ovgrid{grid-template-columns:repeat(4,1fr)}
  #ranges{gap:19px}
  .hero-bal{font-size:clamp(44px,8vw,56px)}
  .hero-chg{font-size:15px}
}

@media (min-width:900px){ .chart-wrap{height:290px} }

@media (min-width:1080px){
  .chart-wrap{height:350px;margin:20px -8px 0}
  body.simple .chart-wrap{height:180px}
  body.dense .chart-wrap{height:230px}
  .stats{grid-template-columns:repeat(4,1fr);gap:11px;margin:22px 0 4px}
  .controls{gap:26px}
  #ranges{overflow:visible;gap:24px;flex:0 0 auto}
  .hero{padding:32px 0 4px}
  .hero-bal{font-size:clamp(52px,4.6vw,66px)}
  .hero-chg{font-size:16px}
  .col{padding:4px 0 22px}
  .col+.col{border-left:1px solid var(--line);border-top:none;padding-top:4px;
    padding-left:26px}
  .duo{grid-template-columns:1fr 1fr;gap:16px}
  h3{margin:22px 0 3px}
}

@media (min-width:1500px){ .chart-wrap{height:390px} }

/* nothing may push the document sideways */
/* `overflow-x:hidden` on BOTH html and body makes BODY its own scroll container. On
   macOS Chrome that can swallow trackpad wheel events while the scrollbar still
   works — you can drag the bar but two fingers do nothing. Measured with the rule
   disabled, scrollWidth === clientWidth at every width from 320 to 2200 in both
   modes, so nothing actually overflows and this was purely defensive. `clip` on the
   root clips without creating a scroll container, and body gets no overflow-x at
   all, so it can never become one. */
html{overflow-x:clip}
.wrbar,.track,.strip,.sbar,.cbar,.zbar{max-width:100%}

/* iPhone: 16px gutters are enough at 375px; give it back on anything wider. */
@media (min-width:600px){ body{padding-left:20px;padding-right:20px} }
@media (min-width:1080px){ body.full{padding-left:32px;padding-right:32px} }
</style>
</head>
<body>
<div id="ptr"><svg width="19" height="19" viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="9" stroke="#4C525B" stroke-width="2.5" stroke-dasharray="42" stroke-dashoffset="14" stroke-linecap="round"/></svg></div>
<div id="blackout">BLACKOUT HOUR — STRATEGY PAUSED</div>

<div id="mini">
  <span class="mb num" id="miniBal">—</span>
  <span class="mc num" id="miniChg">—</span>
</div>

<div class="topbar">
  <span class="brand">Kalshi</span>
  <button class="gear" id="gearBtn" aria-label="Settings">&#9881;</button>
</div>

<div id="anoms"></div>

<div class="hero">
  <div class="hero-lbl" id="heroLbl">Portfolio</div>
  <div class="hero-bal num sk" id="bal">$0000.00</div>
  <div class="hero-chg num" id="chg"><span class="sk">+$00.00 today</span></div>
  <div class="health health-unk" id="health"><span class="hdot"></span><span id="healthTx">checking</span></div>
  <div class="nextclose" id="nextClose"></div>
</div>

<div class="grid">
<div class="sect on" id="secChart">
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
  <div class="yax"><span id="yHi">—</span><span id="yLo">—</span></div>
  <div class="scrub-time" id="scrubTime"></div>
  <div class="chart-empty" id="chartEmpty" style="display:none">No activity in this range</div>
</div>

<div class="controls" data-full>
  <div class="seg" id="ranges"><div class="pill"></div>
    <button data-r="1H" data-full>1H</button><button data-r="1D">1D</button><button data-r="24H" data-full>24H</button><button data-r="48H" data-full>48H</button><button data-r="72H" data-full>72H</button><button data-r="1W">1W</button><button data-r="1M" data-full>1M</button><button data-r="ALL">All</button>
  </div>
  <div class="seg mini" id="modes"><div class="pill"></div>
    <button data-m="pnl">P&amp;L</button><button data-m="bal">Balance</button>
  </div>
</div>

<div class="card pad" id="overviewCard"></div>

<div id="stale">Data may be stale — last refresh failed</div>
<div id="recon" data-full></div>
</div><!-- /secChart -->

<div class="sect" id="secStats" data-full>
<div class="card pad" id="marginCard"></div>
<div class="stats" id="stats">
  <div class="stat"><div class="stat-lbl" id="l0">P&L</div><div class="stat-val num sk" id="v0">$0</div><div class="stat-sub" id="s0"></div></div>
  <div class="stat gone"><div class="stat-lbl" id="l1">Win rate</div><div class="stat-val num sk" id="v1">0%</div><div class="stat-sub" id="s1"></div></div>
  <div class="stat"><div class="stat-lbl" id="l2">Trades</div><div class="stat-val num sk" id="v2">0</div><div class="stat-sub" id="s2"></div></div>
  <div class="stat"><div class="stat-lbl" id="l3">Hour P&L</div><div class="stat-val num sk" id="v3">$0</div><div class="stat-sub" id="s3"></div></div>
  <div class="stat x"><div class="stat-lbl" id="l4">Hour WR</div><div class="stat-val num sk" id="v4">0%</div><div class="stat-sub" id="s4"></div></div>
  <div class="stat"><div class="stat-lbl" id="l5">Open</div><div class="stat-val num sk" id="v5">0</div><div class="stat-sub" id="s5"></div></div>
  <div class="stat x"><div class="stat-lbl" id="l6">Per trade</div><div class="stat-val num sk" id="v6">$0</div><div class="stat-sub" id="s6"></div></div>
  <div class="stat x"><div class="stat-lbl" id="l7">Bet size</div><div class="stat-val num sk" id="v7">$0</div><div class="stat-sub" id="s7"></div></div>
  <div class="stat x"><div class="stat-lbl" id="l8">Headroom</div><div class="stat-val num sk" id="v8">0</div><div class="stat-sub" id="s8"></div></div>
</div>
<button class="statmore" id="statMore" data-full>Show 4 more &#9662;</button>

<div class="duo">
  <div class="card pad" id="paceCard" data-full></div>
  <div class="card pad" id="seriesCard"></div>
</div>

<div class="duo">
  <div class="card pad" id="streakCard"></div>
  <div class="card pad" id="whatifCard"></div>
</div>
</div><!-- /secStats -->

<div class="sect" id="secPos">
  <h3 data-full>Open positions <span class="count" id="posN">0</span></h3>
  <div class="card" id="positions"><div class="empty">No open positions</div></div>
</div>

<div class="sect" id="secTrades">
  <button class="more" id="moreBtn"><span id="moreTx">Recent trades</span>
    <span class="cv">&#9662;</span></button>
  <div id="moreWrap">
    <div class="card pad" id="stripCard" data-full></div>
    <h3 data-full>Recent trades</h3>
    <div class="chips" id="tfilt" data-full></div>
    <div class="card" id="trades"><div class="empty">Loading…</div></div>
  </div>
</div>

</div><!-- /grid -->

<div class="foot" id="foot">—</div>

<nav id="tabs">
  <button data-t="secChart" class="on"><span class="ti">&#9650;</span>Chart</button>
  <button data-t="secPos"><span class="ti">&#9673;</span>Open</button>
  <button data-t="secTrades"><span class="ti">&#9776;</span>Trades</button>
  <button data-t="secStats"><span class="ti">&#9632;</span>Stats</button>
</nav>

<div class="sheet-bg" id="sheetBg"></div>
<div class="sheet" id="sheet">
  <div class="grab"></div>
  <h5>View</h5>
  <div class="sheet-row">
    <span class="mut" style="font-size:12.5px;font-weight:600">Detail level</span>
    <div class="seg mini" id="viewSeg"><div class="pill"></div>
      <button data-v="simple">Simple</button><button data-v="full">Full</button>
    </div>
  </div>
  <h5>Density</h5>
  <div class="sheet-row">
    <span class="mut" style="font-size:12.5px;font-weight:600">Row height</span>
    <div class="seg mini" id="densSeg"><div class="pill"></div>
      <button data-d="comfy">Comfy</button><button data-d="dense">Dense</button>
    </div>
  </div>
  <h5>Alerts</h5>
  <div class="sheet-row">
    <span class="mut" style="font-size:12.5px;font-weight:600">Sound &amp; haptics on fills</span>
    <button class="chip" id="sndBtn">OFF</button>
  </div>
</div>

<script>
'use strict';
const $=id=>document.getElementById(id);
const UP='#00C805', DOWN='#FF5000', BLUE='#4C8DFF';
const AUG1=new Date('2026-08-01T04:00:00Z').getTime();
// The ALL range floors at Aug 1 (Kalshi keeps settled markets ~67 days), so it is
// not all time and must not be labelled as if it were.
const RLBL={'1H':'Hour','1D':'Today','24H':'24 hours','48H':'48 hours',
  '72H':'72 hours','1W':'Week','1M':'Month','ALL':'Since Aug 1'};
let range='1D', mode='pnl', last=null, expanded=new Set(), pts=[], firstDraw=true, scrubbing=false;
let chartT0=0, chartSpan=1;
let viewMode=localStorage.getItem('kv')||'simple';
let density=localStorage.getItem('kd')||'comfy';
let sndOn=localStorage.getItem('ks')==='1';
let tf={series:null,side:null,res:null}, wfBet=null, lastTopTs=null;
const esc=t=>String(t==null?'':t).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtPx=n=>{const a=Math.abs(n);return '$'+n.toLocaleString('en-US',
  {minimumFractionDigits:a<1?4:2,maximumFractionDigits:a<1?4:2});};

/* Modelled baseline, re-measured 2026-08-29 against the CURRENT live config —
   $35 flat, z-gate on at 0.761, both sides. Reproduce exactly:
     python3 scripts/backtest.py --slip 0.227
     -> 9,717 tr  93.59% WR  +$4,978... at 0.105c / +$4,549 at 0.227c
        over 2026-06-11..08-28 (79 days) = +$0.47/tr, +$58/day, 123 tr/day
   These are the ONLY hardcoded strategy numbers on the page; everything else is
   derived from live data. A stale baseline here silently mis-scores every day, and
   this one was stale in TWO ways:
     - it was measured at $50 with the z-gate OFF, and the page rescales linearly by
       bet size, so it was asserting $0.378/tr at $35 against a true $0.47;
     - it used slip 0.105c, which CLAUDE.md struck through on 2026-08-24 as YES-only
       and measured against a 1-min candle. The live figure is 0.227c, n=500, both
       sides. Using the retired number flattered every "pace vs model" reading.
   Re-measure whenever the strategy config changes. */
const MODEL_PER_TRADE = 0.47;    // $/trade at $35 flat, 0.227c slip, z-gate on
const MODEL_TRADES_DAY = 123;    // distinct entries per day the harness takes
const MODEL_BET = 35;            // the bet size those two figures were measured at

/* ── formatting ─────────────────────────────────────────────────── */
const money=n=>(n<0?'-':'')+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const signed=n=>(n>=0?'+':'-')+'$'+Math.abs(n).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const cls=n=>n>0?'up':n<0?'down':'mut';
const pct=(w,n)=>n?(w/n*100).toFixed(1)+'%':'—';
const et=(d,o)=>d.toLocaleString('en-US',Object.assign({timeZone:'America/New_York'},o));
/* ET's UTC offset, resolved once. Used for cheap day bucketing in hot loops. */
const ET_OFF=(()=>{ const d=new Date();
  return new Date(d.toLocaleString('en-US',{timeZone:'America/New_York'})).getTime()
       - new Date(d.toLocaleString('en-US',{timeZone:'UTC'})).getTime(); })();

/* cutoff() does a toLocaleString to find the ET day boundary, which costs ~50us.
   Several callers pass it straight into a filter predicate, so it was running once
   PER SETTLEMENT — 2,500 timezone conversions per render, three times over. Memoised
   per (range, wall-clock second): correctness is unchanged because the value only
   moves once a second, and every caller inside a loop becomes free. */
const _cutC=new Map();
function cutoff(r){
  const k=r+'|'+((Date.now()/1000)|0);
  const hit=_cutC.get(k); if(hit!==undefined) return hit;
  if(_cutC.size>32) _cutC.clear();
  const v=_cutRaw(r); _cutC.set(k,v); return v;
}
function _cutRaw(r){
  const now=new Date(); let t;
  if(r==='1H') t=now.getTime()-3600000;
  else if(r==='24H') t=now.getTime()-24*3600000;
  else if(r==='48H') t=now.getTime()-48*3600000;
  else if(r==='72H') t=now.getTime()-72*3600000;
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

/* Ranges have different point counts AND different time spans, so a morph has to
   interpolate BOTH axes. Resample each side to a fixed count in normalised index
   space, then lerp t and v together — that is what makes the curve appear to
   travel rather than cut. Draw-in owns the first paint; morph owns every range
   change after it. Reduced-motion falls back to a plain redraw. */
function resample(s,n){
  const out=[], m=s.length;
  for(let k=0;k<n;k++){
    const f=k/(n-1)*(m-1), i=Math.floor(f), fr=f-i;
    const a=s[i], b=s[Math.min(m-1,i+1)];
    out.push({t:a.t+(b.t-a.t)*fr, v:a.v+(b.v-a.v)*fr});
  }
  return out;
}
let morphRAF=null, lastSeries=null;
function drawSeries(series,color,baseVal){
  const reduce=window.matchMedia('(prefers-reduced-motion:reduce)').matches;
  if(reduce||firstDraw||!lastSeries||lastSeries.length<2||series.length<2){
    lastSeries=series; drawChart(series,color,baseVal); return;
  }
  const N=170, from=resample(lastSeries,N), to=resample(series,N);
  lastSeries=series;
  if(morphRAF) cancelAnimationFrame(morphRAF);
  const t0=performance.now(), D=560;
  (function step(now){
    const k=Math.min(1,(now-t0)/D), e=1-Math.pow(1-k,3);
    drawChart(from.map((p,i)=>({t:p.t+(to[i].t-p.t)*e, v:p.v+(to[i].v-p.v)*e})),
              color,baseVal);
    // Land on the REAL series, not a 99.9% interpolation of it — otherwise the
    // resampled approximation is what the scrub and the endpoint read from.
    if(k<1) morphRAF=requestAnimationFrame(step);
    else drawChart(series,color,baseVal);
  })(t0);
}
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
  chartT0=t0; chartSpan=span;
  const X=t=>(t-t0)/span*W;
  const Y=v=>PADY+(1-(v-lo)/((hi-lo)||1))*(H-PADY*2);
  let d='';
  series.forEach((p,i)=>{ const x=X(p.t),y=Y(p.v); pts.push({x,y,t:p.t,v:p.v}); d+=(i?'L':'M')+x.toFixed(2)+' '+y.toFixed(2); });
  line.setAttribute('d',d); line.setAttribute('stroke',color);
  area.setAttribute('d',d+'L'+W+' '+H+'L0 '+H+'Z');
  $('g0').setAttribute('stop-color',color); $('g1').setAttribute('stop-color',color);
  if(baseVal!=null){ const by=Y(baseVal); const bl=$('baseline'); bl.setAttribute('y1',by); bl.setAttribute('y2',by); bl.style.opacity=1; }
  else $('baseline').style.opacity=0;
  // Min/max on the axis so a value can be read without scrubbing for it.
  const yl=$('yLo'), yh=$('yHi');
  if(yl&&yh){
    const f=mode==='bal'?money:signed;
    yh.textContent=f(hi-pad); yl.textContent=f(lo+pad);
  }
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
initSeg($('ranges'),'r','1D',v=>{ range=v; if(last) render(last); });
initSeg($('modes'),'m','pnl',v=>{ mode=v; if(last) render(last); });

/* ── view mode & density ────────────────────────────────────────────
   Simple is the default because the page is most often opened to answer one
   question — is it working and am I up. Full is opt-in and remembered. */
function applyView(){
  document.body.classList.toggle('simple',viewMode==='simple');
  document.body.classList.toggle('full',viewMode==='full');
  document.body.classList.toggle('dense',density==='dense');
  // Full hides every section but the active one, so one must always be active or
  // switching into Full lands on a blank page.
  if(viewMode==='full'&&!document.querySelector('.sect.on')) setTab('secChart');
  // A Full-only range must not stay selected after switching to Simple, or the
  // page silently shows a window whose control is no longer on screen.
  const on=$('ranges').querySelector('button.on');
  if(viewMode==='simple'&&on&&on.hasAttribute('data-full')){
    $('ranges').querySelectorAll('button').forEach(x=>x.classList.remove('on'));
    $('ranges').querySelector('button[data-r="1D"]').classList.add('on');
    range='1D'; firstDraw=true;
  }
  requestAnimationFrame(()=>{ movePill($('ranges')); movePill($('modes'));
    movePill($('viewSeg')); movePill($('densSeg')); });
}
initSeg($('viewSeg'),'v',viewMode,v=>{ viewMode=v; localStorage.setItem('kv',v);
  applyView(); if(last) render(last); });
initSeg($('densSeg'),'d',density,v=>{ density=v; localStorage.setItem('kd',v);
  applyView(); if(last) render(last); });
document.body.dataset.tab='secChart';   // stamped before any setTab() call
function paintSnd(){ const b=$('sndBtn'); b.classList.toggle('on',sndOn);
  b.textContent=sndOn?'ON':'OFF'; }
$('sndBtn').addEventListener('click',()=>{ sndOn=!sndOn;
  localStorage.setItem('ks',sndOn?'1':'0'); paintSnd(); if(sndOn) chime(true); });
paintSnd(); applyView();

let _wide=WIDE();
window.addEventListener('resize',()=>{
  movePill($('ranges')); movePill($('modes'));
  movePill($('viewSeg')); movePill($('densSeg'));
  // Crossing the breakpoint changes which sections are on screen, and the ones that
  // were hidden were skipped by vis() — they need a render before they are shown.
  if(WIDE()!==_wide){ _wide=WIDE(); if(last) render(last); }
});

/* ── tabs, sheet, reveal, sticky header ─────────────────────────────
   Full mode is four screens rather than one long scroll. Simple has no tabs at
   all — the whole point is that it is one screen you do not navigate. */
/* A section is worth rendering only if it is on screen. Simple hides everything
   marked data-full; Full shows exactly one tab. */
// Declared as a function, not a const arrow: the resize handler above references it
// during setup, and a const would still be in its temporal dead zone there.
function WIDE(){ return window.matchMedia('(min-width:1080px)').matches; }
function vis(sec){
  const el=$(sec); if(!el) return false;
  if(viewMode==='simple') return !el.hasAttribute('data-full');
  if(WIDE()) return true;   // desktop lays every section out at once, so all render
  return el.classList.contains('on');
}
function setTab(id){
  document.body.dataset.tab=id;
  document.querySelectorAll('.sect').forEach(x=>x.classList.toggle('on',x.id===id));
  const t=$('tabs'); if(t) t.querySelectorAll('button').forEach(b=>
    b.classList.toggle('on',b.dataset.t===id));
  if(navigator.vibrate) navigator.vibrate(3);
  window.scrollTo({top:0,behavior:'smooth'});
  if(last) render(last);          // the tab just revealed was skipped while hidden
}
$('tabs').querySelectorAll('button').forEach(b=>
  b.addEventListener('click',()=>setTab(b.dataset.t)));

function sheet(on){
  $('sheet').classList.toggle('on',on);
  $('sheetBg').classList.toggle('on',on);
  if(on&&navigator.vibrate) navigator.vibrate(3);
  requestAnimationFrame(()=>{ movePill($('viewSeg')); movePill($('densSeg')); });
}
$('gearBtn').addEventListener('click',()=>sheet(true));
$('sheetBg').addEventListener('click',()=>sheet(false));
document.addEventListener('keydown',e=>{ if(e.key==='Escape') sheet(false); });

$('statMore').addEventListener('click',()=>{
  const o=document.body.classList.toggle('statx');
  $('statMore').innerHTML=o?'Show less &#9652;':'Show 4 more &#9662;';
  if(navigator.vibrate) navigator.vibrate(3);
});
$('moreBtn').addEventListener('click',()=>{
  const open=document.body.classList.toggle('moreopen');
  $('moreTx').textContent=open?'Hide trades':'Recent trades';
  if(navigator.vibrate) navigator.vibrate(3);
});

let miniOn=false;
window.addEventListener('scroll',()=>{
  const on=window.scrollY>150;
  if(on!==miniOn){ miniOn=on; $('mini').classList.toggle('on',on); }
},{passive:true});
$('mini').addEventListener('click',()=>window.scrollTo({top:0,behavior:'smooth'}));

/* ── chart-tab overview ─────────────────────────────────────────────
   The Chart tab was a chart and then half a screen of black. This fills it with the
   four numbers worth knowing at a glance plus where the money came from, so the
   default tab answers "how am I doing" without a trip to Stats. */
function renderOverview(sett,d){
  const el=$('overviewCard'); if(!el) return;
  const cut=cutoff(range), inR=sett.filter(s=>s._t>=cut);
  if(!inR.length){ el.innerHTML='<div class="empty">No trades in range</div>'; return; }
  const pnl=inR.reduce((a,s)=>a+s.pnl,0);
  const cost=inR.reduce((a,s)=>a+s.cost,0);
  const fees=inR.reduce((a,s)=>a+(s.fee||0),0);
  const con=inR.reduce((a,s)=>a+(s.con||0),0);
  const wc=inR.filter(s=>s.won).reduce((a,s)=>a+s.cost,0);
  const dw=cost?100*wc/cost:0, be=con?100*(cost+fees)/con:0, m=dw-be;
  const nOpen=(d.positions||[]).length;
  const tile=(l,v,c)=>'<div class="ov"><div class="ovl">'+l+'</div><div class="ovv num '+
    (c||'')+'">'+v+'</div></div>';
  const by={};
  for(const x of inR){ const k=(x.series||'').replace('KX','').replace('15M','')||'?';
    (by[k]=by[k]||[]).push(x); }
  const keys=Object.keys(by).sort((a,b)=>
    by[b].reduce((x,y)=>x+y.pnl,0)-by[a].reduce((x,y)=>x+y.pnl,0));
  const mx=Math.max(...keys.map(k=>Math.abs(by[k].reduce((x,y)=>x+y.pnl,0))))||1;
  el.innerHTML='<div class="ovgrid">'+
      tile(RLBL[range]+' P&L',signed(pnl),cls(pnl))+
      tile('Margin',(m>=0?'+':'')+m.toFixed(2)+'pp',m>=0?'up':'down')+
      tile('Trades',inR.length,'')+
      tile('Open',nOpen,'')+
    '</div>'+
    '<div class="ovsep"></div>'+
    keys.map(k=>{
      const g=by[k], v=g.reduce((a,x)=>a+x.pnl,0), w=g.filter(x=>x.won).length;
      const col=v>0?UP:v<0?DOWN:'#7C828C';
      const wd=Math.max(3,Math.abs(v)/mx*100);
      return '<div class="srow"><div class="sname">'+esc(k)+'</div>'+
        '<div class="sbar"><i style="width:'+wd.toFixed(0)+'%;background:'+col+
          ';opacity:.75;'+(v<0?'right:50%':'left:50%')+'"></i>'+
          '<i style="left:50%;width:1px;background:rgba(255,255,255,.18);opacity:1"></i></div>'+
        '<div class="sv '+cls(v)+'">'+signed(v)+'</div>'+
        '<div class="sn">'+pct(w,g.length)+'</div></div>';
    }).join('');
}

/* ── margin: win rate against its OWN break-even ────────────────────
   The only number that says whether the account makes money. A win rate on its
   own cannot: at ~91.8c entries you need ~91.8% before fees just to break even,
   so 92% is a LOSS. Break-even here is (cost + fees) / contracts, never
   cost/contracts — the fee-blind version reads green at a real loss. */
function renderMargin(sett){
  const el=$('marginCard'); if(!el) return;
  const inR=sett.filter(s=>s._t>=cutoff(range));
  const cost=inR.reduce((a,s)=>a+s.cost,0);
  const fees=inR.reduce((a,s)=>a+(s.fee||0),0);
  const con=inR.reduce((a,s)=>a+(s.con||0),0);
  if(!inR.length||!cost||!con){
    el.innerHTML='<h4>Margin</h4><div class="empty">No trades in range</div>'; return;
  }
  const wcost=inR.filter(s=>s.won).reduce((a,s)=>a+s.cost,0);
  const dw=100*wcost/cost, be=100*(cost+fees)/con, m=dw-be;
  const lo=Math.min(dw,be)-1.2, hi=Math.max(dw,be)+0.6, span=(hi-lo)||1;
  const wpc=Math.max(2,Math.min(100,(dw-lo)/span*100));
  const bpc=Math.max(0,Math.min(100,(be-lo)/span*100));
  const col=m>=0?UP:DOWN;
  el.innerHTML='<h4>Margin · '+RLBL[range]+'</h4>'+
    '<div class="prow"><span class="l">$-weighted win rate</span>'+
      '<span class="v num">'+dw.toFixed(2)+'%</span></div>'+
    '<div class="prow"><span class="l">Break-even (incl. fees)</span>'+
      '<span class="v num mut">'+be.toFixed(2)+'%</span></div>'+
    '<div class="wrbar"><div class="wf" style="width:'+wpc.toFixed(1)+'%;background:'+
      col+';opacity:.22"></div>'+
      '<div class="be" style="left:'+bpc.toFixed(1)+'%"></div>'+
      '<div class="bel" style="left:'+bpc.toFixed(1)+'%">B/E</div>'+
      '<div class="wv" style="color:'+col+'">'+(m>=0?'+':'')+m.toFixed(2)+'pp</div></div>'+
    '<div class="pnote">'+(m>=0
      ? 'Winning '+m.toFixed(2)+' points more often than you need to.'
      : 'Below break-even — the win rate does not cover entry price plus fees.')+'</div>';
}

/* ── trade strip: 60 outcomes at a glance ───────────────────────── */
function renderStrip(sett){
  const el=$('stripCard'); if(!el) return;
  const rows=applyFilters(sett).slice(-60);
  if(!rows.length){ el.innerHTML='<h4>Last 60</h4><div class="empty">None</div>'; return; }
  const w=rows.filter(s=>s.won).length;
  // Height was |P&L|, which made this LIE. A loss is ~$34 and a win is ~$2.50, so a
  // 54W/6L run rendered as a wall of red — the strip said "disaster" about the best
  // week on record. Outcome is categorical, so it gets the categorical encoding:
  // equal height, colour carries the result. Magnitude lives in opacity and the
  // tooltip, where it cannot overpower a 90% win rate.
  const mx=Math.max(...rows.map(s=>Math.abs(s.pnl)))||1;
  el.innerHTML='<h4>Last '+rows.length+' · '+w+'W '+(rows.length-w)+'L · '+
      (100*w/rows.length).toFixed(0)+'%</h4>'+
    '<div class="strip">'+rows.map(s=>{
      const mag=0.45+0.55*Math.min(1,Math.abs(s.pnl)/mx);
      return '<i style="height:100%;background:'+(s.won?UP:DOWN)+
        ';opacity:'+mag.toFixed(2)+'" title="'+signed(s.pnl)+'"></i>';
    }).join('')+'</div>';
}

/* ── sound + haptics ────────────────────────────────────────────────
   Ambient awareness: a distinct tone for a win and a loss means the state is
   audible without the page being on screen. Off by default and remembered —
   audio that starts itself uninvited is hostile. */
function chime(win){
  if(!sndOn) return;
  try{
    const C=window.AudioContext||window.webkitAudioContext; if(!C) return;
    const ctx=chime._c||(chime._c=new C());
    if(ctx.state==='suspended') ctx.resume();
    const o=ctx.createOscillator(), g=ctx.createGain();
    o.type='sine'; o.frequency.value=win?880:320;
    g.gain.setValueAtTime(0.0001,ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.075,ctx.currentTime+0.02);
    g.gain.exponentialRampToValueAtTime(0.0001,ctx.currentTime+0.3);
    o.connect(g); g.connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime+0.32);
  }catch(e){}
}
function announce(sett){
  const top=sett.length?sett[sett.length-1]:null;
  if(!top){ return; }
  if(lastTopTs!==null && top.ts!==lastTopTs){
    chime(top.won);
    if(navigator.vibrate) navigator.vibrate(top.won?6:[7,45,7]);
  }
  lastTopTs=top.ts;
}

/* ── anomalies ──────────────────────────────────────────────────────
   Silence is the healthy state. Anything rendered here is, by construction,
   outside this account's own historical norms. */
function renderAnoms(d){
  const el=$('anoms'), a=d.anomalies||[];
  // Nothing is the right rendering for nothing. The health pill already says the
  // trader is live, so a permanent "ALL CLEAR" only pushed the balance down the page.
  if(!a.length){ el.innerHTML=''; return; }
  el.innerHTML=a.map(x=>'<div class="anom '+esc(x.sev)+'"><div class="ic"></div><div>'+
    '<div class="am">'+esc(x.msg)+'</div>'+
    (x.detail?'<div class="ad">'+esc(x.detail)+'</div>':'')+'</div></div>').join('');
}


/* ── streaks ────────────────────────────────────────────────────── */
function renderStreak(sett){
  const el=$('streakCard'); if(!el) return;
  let cur=0,best=0,curL=0,worstL=0;
  for(const s of sett){
    if(s.won){ cur++; best=Math.max(best,cur); curL=0; }
    else { curL++; worstL=Math.max(worstL,curL); cur=0; }
  }
  // Bucket by ET calendar day with arithmetic, not toLocaleString per row — the
  // per-row version cost ~124ms on 2,500 settlements all by itself. The ET offset is
  // resolved once; a trade within an hour of midnight on one of the two DST-change
  // days can land in the neighbouring bucket, which is acceptable for "best day".
  const days={};
  for(const s of sett){ const k=Math.floor((s._t+ET_OFF)/86400000);
    days[k]=(days[k]||0)+s.pnl; }
  const vals=Object.values(days);
  const bestDay=vals.length?Math.max(...vals):0, worstDay=vals.length?Math.min(...vals):0;
  el.innerHTML='<h4>Streaks &amp; bests</h4><div class="streak">'+
    '<div class="sk2"><div class="n up num">'+cur+'</div><div class="t">current W</div></div>'+
    '<div class="sk2"><div class="n num">'+best+'</div><div class="t">best W</div></div>'+
    '<div class="sk2"><div class="n down num">'+worstL+'</div><div class="t">worst L</div></div>'+
    '</div>'+
    '<div class="prow" style="margin-top:10px"><span class="l">Best day</span>'+
      '<span class="v num up">'+signed(bestDay)+'</span></div>'+
    '<div class="prow"><span class="l">Worst day</span>'+
      '<span class="v num down">'+signed(worstDay)+'</span></div>';
}

/* ── what-if sizing ─────────────────────────────────────────────── */
let wfState=null;
function paintWhatif(){
  if(!wfState) return;
  const {cur,base}=wfState, scaled=base*(wfBet/cur);
  $('wfVal').textContent='$'+wfBet;
  $('wfBaseL').textContent=RLBL[wfState.range]+' at $'+cur;
  const bv=$('wfBaseV'); bv.textContent=signed(base); bv.className='v num '+cls(base);
  $('wfScaleL').textContent='at $'+wfBet;
  const sv=$('wfScaleV'); sv.textContent=signed(scaled); sv.className='v num '+cls(scaled);
}
function renderWhatif(sett,d){
  const el=$('whatifCard'); if(!el) return;
  const cur=d.bet||35; if(wfBet==null) wfBet=cur;
  const inR=sett.filter(s=>s._t>=cutoff(range));
  wfState={cur:cur, base:inR.reduce((a,s)=>a+s.pnl,0), range:range};
  // Build the shell ONCE. This used to rebuild el.innerHTML from the slider's own
  // 'input' handler, which destroyed the <input> being dragged — the pointer was
  // left holding a node no longer in the document, so the slider jumped one step
  // and stopped dead. On touch it barely moved at all. Only the text changes now.
  if(!el.dataset.built){
    el.innerHTML='<h4>What if the bet were…</h4>'+
      '<div class="wf-row"><input type="range" id="wfR" min="10" max="100" step="5">'+
        '<span class="wf-val num" id="wfVal"></span></div>'+
      '<div class="prow"><span class="l" id="wfBaseL"></span>'+
        '<span class="v num" id="wfBaseV"></span></div>'+
      '<div class="prow"><span class="l" id="wfScaleL"></span>'+
        '<span class="v num" id="wfScaleV"></span></div>'+
      '<div class="pnote">Linear rescale of the same trades at the same prices. It does '+
        'NOT model fill quality or book depth at larger size, both of which get worse — '+
        'so read it as a bound, not a forecast.</div>';
    el.dataset.built='1';
    $('wfR').addEventListener('input',e=>{ wfBet=+e.target.value; paintWhatif(); });
  }
  // Do not fight a finger that is mid-drag: a 30s refresh must not yank the thumb.
  const r=$('wfR');
  if(document.activeElement!==r && +r.value!==wfBet) r.value=wfBet;
  paintWhatif();
}

/* ── trade filters ──────────────────────────────────────────────── */
function renderFilters(sett){
  const el=$('tfilt'); if(!el) return;
  const ser=[...new Set(sett.map(s=>(s.series||'').replace('KX','').replace('15M','')))]
    .filter(Boolean).sort();
  const mk=(k,v,l)=>'<button class="chip'+(tf[k]===v?' on':'')+'" data-k="'+k+'" data-v="'+
    esc(v)+'">'+esc(l)+'</button>';
  el.innerHTML=mk('res','win','Wins')+mk('res','loss','Losses')+
    mk('side','yes','YES')+mk('side','no','NO')+ser.map(x=>mk('series',x,x)).join('');
  el.querySelectorAll('.chip').forEach(b=>b.addEventListener('click',()=>{
    const k=b.dataset.k, v=b.dataset.v; tf[k]=tf[k]===v?null:v;
    if(navigator.vibrate) navigator.vibrate(2);
    if(last) render(last);
  }));
}
function applyFilters(rows){
  if(viewMode!=='full') return rows;      // chips are hidden in Simple; so is their effect
  return rows.filter(s=>{
    if(tf.res==='win'&&!s.won) return false;
    if(tf.res==='loss'&&s.won) return false;
    if(tf.side&&(s.side||'').toLowerCase()!==tf.side) return false;
    if(tf.series&&(s.series||'').replace('KX','').replace('15M','')!==tf.series) return false;
    return true;
  });
}

/* ── all-time high ──────────────────────────────────────────────── */
function checkHWM(bal){
  if(!(bal>0)) return false;
  const prev=parseFloat(localStorage.getItem('khwm')||'0');
  if(bal>prev){
    localStorage.setItem('khwm',String(bal));
    if(prev>0){
      const el=$('bal'); el.classList.remove('hwm'); void el.offsetWidth; el.classList.add('hwm');
      if(navigator.vibrate) navigator.vibrate([6,40,6]);
    }
    return true;
  }
  return bal>=prev-0.005;
}

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
/* ── position drama: live spot vs strike ────────────────────────────
   The actual bet, drawn. z is the cushion between spot and the strike measured in
   units of how far price can still travel before close — the SAME quantity the
   trader gates entries on, so the number here is the one the bot would compute.
   The marker walks as spot moves and the scale tightens as the clock runs down,
   which is the whole drama: a comfortable position gets thinner by standing still. */
function drama(p){
  // Market already closed: z is undefined because there is no remaining volatility to
  // normalise against, and CURRENT spot is NOT the settlement price — that is the RTI
  // print taken at close, which has already happened. Drawing spot-vs-strike here
  // would imply an outcome from a price that no longer decides it. The book does know,
  // so say what the book says and nothing more.
  if(p.z==null&&p.bid!=null&&p.close_time&&new Date(p.close_time).getTime()<=Date.now()){
    const decided=p.bid>=99, lost=p.bid<=1;
    const col=decided?UP:lost?DOWN:'#7C828C';
    return '<div class="drama"><div class="drama-top">'+
      '<span>awaiting settlement</span>'+
      '<span class="drama-z" style="color:'+col+'">'+
        (decided?'WON':lost?'LOST':'UNDECIDED')+'</span></div>'+
      '<div class="drama-sub"><span>book '+p.bid+'¢ on '+esc((p.side||'').toUpperCase())+
        '</span><span class="mut">settlement price is fixed at close</span></div></div>';
  }
  if(p.z==null||p.strike==null||p.spot==null) return '';
  const z=p.z;
  const col=z>=1.5?UP:z>=0.761?'#8FD14F':z>=0?'#FFA318':DOWN;
  const L=Math.max(2,Math.min(98,50+(z/6)*100));
  const diff=p.spot-p.strike, dp=p.strike?diff/p.strike*100:0;
  const note=z<0?'behind — needs a reversal':z<0.761?'thin — under the gate cut':
    z<1.5?'holding':'comfortable';
  return '<div class="drama">'+
    '<div class="drama-top"><span>'+esc(note)+'</span>'+
      '<span class="drama-z" style="color:'+col+'">z '+(z>=0?'+':'')+z.toFixed(2)+'</span></div>'+
    '<div class="track">'+
      '<div class="fill" style="'+(z>=0?'left:50%;width:'+(L-50).toFixed(1)+'%':
        'left:'+L.toFixed(1)+'%;width:'+(50-L).toFixed(1)+'%')+';background:'+col+'"></div>'+
      '<div class="mid"></div><div class="midl">STRIKE</div>'+
      '<div class="mk" style="left:'+L.toFixed(1)+'%;background:'+col+'"></div>'+
    '</div>'+
    '<div class="drama-sub"><span>spot '+fmtPx(p.spot)+'</span>'+
      '<span style="color:'+col+'">'+(diff>=0?'+':'')+fmtPx(diff)+' ('+(dp>=0?'+':'')+
        dp.toFixed(2)+'%)</span>'+
      '<span>&sigma; '+(p.sigma_bp!=null?p.sigma_bp.toFixed(1)+'bp':'—')+'</span></div>'+
    '</div>';
}

function renderPace(sett, d){
  const el=$('paceCard'); if(!el) return;
  const dayStart=cutoff('1D');
  const today=sett.filter(s=>s._t>=dayStart);
  const n=today.length, pnl=today.reduce((a,s)=>a+s.pnl,0);
  const per=n?pnl/n:null;
  // scale the baseline to whatever the account is actually betting
  const openCost=(d.positions||[]).reduce((a,p)=>a+(p.cost||0),0);
  const bet=(d.positions||[]).length?openCost/(d.positions||[]).length:MODEL_BET;
  const scale=Math.max(0.2,Math.min(6,bet/MODEL_BET));
  const expPer=MODEL_PER_TRADE*scale, expDay=MODEL_PER_TRADE*MODEL_TRADES_DAY*scale;
  const et0=new Date(dayStart), frac=Math.min(1,(Date.now()-dayStart)/86400000);
  const ratio=per!=null&&expPer>0?per/expPer:null;
  // Straight-line projection off elapsed day fraction. Suppressed before 8% of the
  // day has run, where one trade swings it by tens of dollars and it reads as noise.
  const proj=frac>0.08?pnl/frac:null;
  const capture=Math.min(100,n/MODEL_TRADES_DAY*100);
  const barCol=ratio==null?'var(--dimmer)':ratio>=1?UP:ratio>=0?'var(--warn)':DOWN;
  el.innerHTML='<h4>Pace vs model</h4>'+
    '<div class="prow"><span class="l">Today</span><span class="v num '+cls(pnl)+'">'+
      signed(pnl)+'</span></div>'+
    '<div class="prow"><span class="l">Model pace</span><span class="v num mut">'+
      signed(expDay*frac)+'</span></div>'+
    '<div class="prow"><span class="l">Projected close</span><span class="v num '+
      (proj==null?'mut':cls(proj))+'">'+(proj==null?'—':signed(proj))+'</span></div>'+
    '<div class="prow"><span class="l">Per trade</span><span class="v num '+
      (per==null?'mut':cls(per-expPer))+'">'+
      (per==null?'—':signed(per)+'  vs  '+signed(expPer))+'</span></div>'+
    '<div class="prow"><span class="l">Trades</span><span class="v num mut">'+n+
      ' / '+MODEL_TRADES_DAY+'</span></div>'+
    '<div class="bar"><i style="width:'+capture+'%;background:'+barCol+'"></i></div>'+
    '<div class="pnote">'+(frac*100).toFixed(0)+'% of the day elapsed · baseline '+
      signed(MODEL_PER_TRADE)+'/trade at $'+MODEL_BET+' (0.227c slip)'+
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
  const inR=sett.filter(s=>s._t>=cut);
  const by={};
  for(const s of inR){
    const k=(s.series||'').replace('KX','').replace('15M','')||'?';
    (by[k]=by[k]||[]).push(s);
  }
  const keys=Object.keys(by).sort((a,b)=>
    by[b].reduce((x,s)=>x+s.pnl,0)-by[a].reduce((x,s)=>x+s.pnl,0));
  if(!keys.length){ el.innerHTML='<h4>By series</h4><div class="empty">No trades in range</div>'; return; }
  const mx=Math.max(...keys.map(k=>Math.abs(by[k].reduce((x,s)=>x+s.pnl,0))))||1;
  el.innerHTML='<h4>By series · '+RLBL[range]+'</h4>'+keys.map(k=>{
    const g=by[k], p=g.reduce((a,s)=>a+s.pnl,0), w=g.filter(s=>s.won).length;
    const col=p>0?UP:p<0?DOWN:'#7C828C';
    // Bar length is |P&L| against the biggest mover, so the ranking is readable as
    // a shape before any number is read. Sparkline stays for the shape over time.
    const wd=mx>0?Math.max(3,Math.abs(p)/mx*100):0;
    return '<div class="srow"><div class="sname">'+esc(k)+'</div>'+
      '<div class="sbar"><i style="width:'+wd.toFixed(0)+'%;background:'+col+
        ';opacity:.75;'+(p<0?'right:50%':'left:50%')+'"></i>'+
        '<i style="left:50%;width:1px;background:rgba(255,255,255,.18);opacity:1"></i></div>'+
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
  const moved=settAll.filter(s=>s._t>prev.t).reduce((a,s)=>a+s.pnl,0)
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
  // Parse each settled_time ONCE per payload. Every range filter below then becomes
  // a numeric compare instead of a Date construction, and there are ~10 such passes.
  const settAll=d.settlements||[];
  if(settAll.length&&settAll[0]._t===undefined)
    for(let i=0;i<settAll.length;i++) settAll[i]._t=new Date(settAll[i].ts).getTime();
  const sett=settAll.filter(s=>s.strat!==false);   // flag absent on older payloads
  const cut=cutoff(range);
  const inR=sett.filter(s=>s._t>=cut);

  // blackout — server reads BLACKOUT_HOURS from the trader; null means unverified
  const hr=parseInt(et(new Date(),{hour:'numeric',hour12:false}))||0;
  $('blackout').style.display=(d.blackout||[]).includes(hr)?'block':'none';

  // series — strategy stats use 15M settlements only; the balance line uses every
  // settlement, because anything that moved cash has to be in the reconstruction.
  let run=0; const pnlAll=sett.map(s=>{run+=s.pnl;return{t:s._t,v:run};});
  const deps=(d.deposits||[]).map(x=>({t:new Date(x.ts).getTime(),dep:x.amount,pnl:0}));
  const trs=settAll.map(x=>({t:x._t,dep:0,pnl:x.pnl}));
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
  drawSeries(series,color,baseVal);

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
      '<span class="chg-sub">'+(ret==null?'':'on '+money(capital)+' capital')+
      '</span><span class="chg-dot">·</span><span class="chg-sub">'+
      money(liveBal)+'</span><span class="chg-dot">·</span><span class="chg-sub">'+
      inR.length+' trades</span>';
  }

  // stats
  const wins=inR.filter(s=>s.won).length;
  const hs=sett.filter(s=>s._t>=cutoff('1H'));
  const hw=hs.filter(s=>s.won).length, hp=hs.reduce((a,s)=>a+s.pnl,0);
  const alt=range==='1H';
  const dS=alt?sett.filter(s=>s._t>=cutoff('1D')):hs;
  const dW=dS.filter(s=>s.won).length, dP=dS.reduce((a,s)=>a+s.pnl,0);
  const avg=inR.length?rangePnl/inR.length:0;

  // A TRADE-weighted win rate against a flat 92% is the wrong comparison and has
  // already misled once (CLAUDE.md: "Never compare a raw win rate to a flat 92%").
  // P&L experiences a DOLLAR-weighted rate, and break-even moves with the price
  // paid. Both are computed from the trades actually in range:
  //   dollar-weighted WR = cost(winners) / cost(all)
  //   break-even         = (cost(all) + fees) / contracts(all)
  // Fees belong INSIDE break-even. P&L is rev-cost-fee, so a break-even priced on
  // cost alone is one a winner clears while the account still loses: at ~0.54c per
  // contract that is 0.54pp, enough to show green at a real -0.2pp. Shipped wrong
  // once; the fee term is the whole reason a 92.5% dollar-weighted rate lost money.
  // Contracts are exact for winners (settlement pays $1/contract, so rev IS the
  // count). Losers carry no count, so they are imputed at the winner-implied
  // average price; every trade sits in the same 90-93c band, which holds that
  // error near 0.03pp — two orders below the ~1.2pp effect this is here to show.
  function margin(rows){
    if(!rows.length) return null;
    const cost=rows.reduce((a,s)=>a+s.cost,0);
    const fees=rows.reduce((a,s)=>a+(s.fee||0),0);
    if(cost<=0) return null;
    const W=rows.filter(s=>s.won);
    const wCost=W.reduce((a,s)=>a+s.cost,0);
    const wCon =W.reduce((a,s)=>a+s.rev ,0);          // $1/contract => rev = count
    if(wCon<=0) return null;
    const px=wCost/wCon;                               // implied avg entry price
    if(!(px>0&&px<1)) return null;
    const con=rows.reduce((a,s)=>a+(s.won?s.rev:s.cost/px),0);
    if(con<=0) return null;
    const dwWR=100*wCost/cost, bev=100*(cost+fees)/con;
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
    ['l5','Open','v5',String((d.positions||[]).length),(d.positions||[]).length?'up':'mut','s5',''],
    ['l6','Per trade','v6',inR.length?signed(avg):'—',inR.length?cls(avg):'mut','s6',
     RLBL[range].toLowerCase()],
    ['l7','Bet size','v7',d.bet?'$'+d.bet:'—','mut','s7','flat'],
    ['l8','Headroom','v8',
      (d.stop&&d.bet&&d.balance)?String(Math.floor((d.balance-d.stop)/d.bet)):'—','mut','s8',
      d.stop?'losses to $'+d.stop:'']
  ];
  for(const [li,lt,vi,vt,vc,si,st] of S){
    $(li).textContent=lt; const el=$(vi);
    el.className='stat-val num '+vc; setNum(el,vt); $(si).textContent=st;
  }

  renderAnoms(d);
  announce(sett);
  $('heroLbl').innerHTML='Portfolio'+(checkHWM(d.balance)?
    '<span class="hwm-b">&#9650; ALL-TIME HIGH</span>':'');
  // Full mode shows one section at a time, so building the other three every refresh
  // was pure waste — the Stats tab alone is five cards over the whole settlement list.
  if(vis('secStats')){
    renderPace(sett, d); renderSeries(sett); renderStreak(sett);
    renderWhatif(sett, d); renderMargin(sett);
  }
  if(vis('secTrades')) renderStrip(sett);
  if(vis('secChart')) renderOverview(sett, d);
  $('miniBal').textContent=money(d.balance||0);
  const dayP=sett.filter(s=>s._t>=cutoff('1D'))
    .reduce((a,s)=>a+s.pnl,0);
  const mc=$('miniChg'); mc.textContent=signed(dayP); mc.className='mc num '+cls(dayP);
  renderFilters(sett);

  // positions
  const pos=d.positions||[]; $('posN').textContent=pos.length;
  const pe=$('positions');
  if(pos.length&&viewMode==='simple'){
    // Simple: one line each. Coin, side, time bar, clock. Nothing else.
    pe.innerHTML=pos.map(p=>{
      const ms=p.close_time?new Date(p.close_time).getTime()-Date.now():0;
      const sc=Math.max(0,Math.floor(ms/1000));
      const lt=ms<=0?'settling':(sc<60?sc+'s':Math.floor(sc/60)+':'+String(sc%60).padStart(2,'0'));
      const frac=Math.max(0,Math.min(1,sc/600))*100;
      const col=ms<=0?'#7C828C':p.z==null?'#7C828C':p.z>=1.5?UP:p.z>=0.761?'#8FD14F':
        p.z>=0?'#FFA318':DOWN;
      return '<div class="cpos" data-ct="'+(p.close_time?new Date(p.close_time).getTime():0)+'">'+
        '<div class="cs">'+esc(p.ticker.split('-')[0].replace('KX','').replace('15M',''))+'</div>'+
        '<div class="cd">'+esc((p.side||'').toUpperCase())+' '+(p.entry!=null?p.entry+'\u00A2':'')+'</div>'+
        '<div class="cbar"><i style="width:'+frac.toFixed(0)+'%;background:'+col+'"></i></div>'+
        '<div class="ct" style="color:'+col+'">'+lt+'</div></div>';
    }).join('');
  } else if(pos.length){
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
      return '<div class="pos" data-ct="'+(p.close_time?new Date(p.close_time).getTime():0)+'"><div class="pos-top"><div style="flex:1;min-width:0">'+
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
        '</div>'+drama(p)+'</div>';
    }).join('');
  } else pe.innerHTML='<div class="empty">No open positions</div>';

  // trades
  const rec=applyFilters(sett).slice(-60).reverse(), te=$('trades');
  if(rec.length){
    te.innerHTML=rec.map(s=>{
      const k=s.ticker+'|'+s.ts, ex=expanded.has(k), dt=new Date(s.ts);
      const ts=et(dt,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit',hour12:true});
      return '<div class="tr" data-k="'+k+'">'+
        '<span class="badge '+(s.won?'badge-w':'badge-l')+'">'+(s.won?'W':'L')+'</span>'+
        '<div class="tr-mid"><div class="tr-ser">'+s.series+'</div>'+
        '<div class="tr-time">settled '+ts+' ET</div></div>'+
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
let inFlight=null;
// Pull-to-refresh, the 30s timer and the visibility handler can all fire at once.
// Without this they raced and whichever response landed last won, so the screen
// could jump backwards to older data. Callers all await the SAME request.
async function refresh(){
  if(inFlight) return inFlight;
  inFlight=(async()=>{
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
  })().finally(()=>{ inFlight=null; });
  return inFlight;
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
/* Position clocks tick every second. Calling render() to move a countdown was
   costing ~10 filtered passes over every settlement — thousands of Date parses per
   second — plus rebuilding the trade list, the strip and every card. That was the
   lag. Touch only the two nodes per position that actually change. */
function tickPositions(){
  const now=Date.now(), els=document.querySelectorAll('[data-ct]');
  for(let i=0;i<els.length;i++){
    const el=els[i], ms=(+el.dataset.ct)-now, sec=Math.max(0,Math.floor(ms/1000));
    const compact=el.classList.contains('cpos');
    const lt=ms<=0?'settling':(compact
      ? (sec<60?sec+'s':Math.floor(sec/60)+':'+String(sec%60).padStart(2,'0'))
      : (sec<60?sec+'s':Math.floor(sec/60)+'m '+(sec%60)+'s'));
    const t=el.querySelector(compact?'.ct':'.pos-left');
    if(t&&t.textContent!==lt) t.textContent=lt;
    const bar=el.querySelector(compact?'.cbar i':'.bar i');
    if(bar) bar.style.width=(Math.max(0,Math.min(1,sec/600))*100).toFixed(1)+'%';
  }
}
setInterval(()=>{ if(!scrubbing&&!document.hidden) tickPositions(); },1000);
refresh();
tickClose(); setInterval(tickClose,1000);
setInterval(()=>{ if(!document.hidden) refresh(); },30000);
// A backgrounded tab can sit for hours; catch up the moment it is looked at again.
// Registered ONCE. This was duplicated, so every return to the tab fired two full
// refreshes — two ~540 KB fetches racing each other, on phones, on cellular.
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
