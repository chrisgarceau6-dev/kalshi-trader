#!/usr/bin/env python3
"""
kalshi_weather_edge.py — Kalshi daily high-temp markets vs NWP ensembles (hypothesis H1).

Subcommands
  selftest          offline checks (fees vs published table, book walker, bucket probs)
  discover          find live weather series/tickers (don't trust the hardcoded CITIES)
  snapshot          live EV table + paper log for today's strikes (run from cron)
  backtest          historical: candlestick quotes at a decision hour vs Previous-Runs model
  calibration-pull  corrected settled-market calibration dataset (parlays filtered out)

Examples
  python kalshi_weather_edge.py selftest
  python kalshi_weather_edge.py discover
  python kalshi_weather_edge.py snapshot --cities NYC,CHI
  python kalshi_weather_edge.py backtest --cities NYC,CHI,MIA --start 2026-05-15 --end 2026-07-17
  python kalshi_weather_edge.py calibration-pull --days 60 --min-volume 250

Endpoints verified against docs.kalshi.com and open-meteo.com on 2026-07-19 (fee schedule
effective 2026-02-05). NOT live-fired from the machine that wrote it — expect small param
drift; errors are loud on purpose. Market-data GETs need no auth. Be polite: this sleeps
between calls.
"""
import argparse, csv, json, math, os, sys, time
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

BASES = [
    "https://api.kalshi.com/trade-api/v2",
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
]
_BASE = None
UA = {"User-Agent": "chris-weather-research/0.1"}
SLEEP = 0.15

# TODO(chris): run `discover` and correct these tickers before trusting anything below.
# lat/lon should approximate the SETTLEMENT STATION (e.g., NYC settles on the Central Park
# ASOS climate report), not the city center. bias_mu/sigma_extra: leave 0 until fitted.
CITIES = {
    "NYC": dict(series="KXHIGHNY",   lat=40.779, lon=-73.969, tz="America/New_York", bias_mu=0.0, sigma_extra=0.0),
    "CHI": dict(series="KXHIGHCHI",  lat=41.786, lon=-87.752, tz="America/Chicago",  bias_mu=0.0, sigma_extra=0.0),
    "MIA": dict(series="KXHIGHMIA",  lat=25.788, lon=-80.317, tz="America/New_York", bias_mu=0.0, sigma_extra=0.0),
    "AUS": dict(series="KXHIGHAUS",  lat=30.183, lon=-97.680, tz="America/Chicago",  bias_mu=0.0, sigma_extra=0.0),
    "DEN": dict(series="KXHIGHDEN",  lat=39.847, lon=-104.656, tz="America/Denver",  bias_mu=0.0, sigma_extra=0.0),
    "PHIL": dict(series="KXHIGHPHIL", lat=39.873, lon=-75.227, tz="America/New_York", bias_mu=0.0, sigma_extra=0.0),
}
# TODO(chris): confirm these model ids exist on the ensemble endpoint (ecmwf 51 members + GFS).
ENSEMBLE_MODELS = "ecmwf_ifs025,gfs_seamless"

BANKROLL = 2000.0
RISK_CAP = 0.02 * BANKROLL          # max $ at risk per trade ($40)
EV_THRESHOLD = 0.02                 # TODO(chris): entry threshold; revisit after paper data
BACKTEST_SLIP = 0.01                # +1c adverse fill assumption when book history is unknown
PARTICIPATION = 0.15                # TODO(chris): max share of that hour's volume you assume fillable
DECISION_HOUR_LOCAL = 10            # TODO(chris): 10:00 local; try 14:00 too — edges differ by hour

# ---------------------------------------------------------------- HTTP helpers
def kget(path, **params):
    """GET against the first Kalshi base that answers; loud on failure."""
    global _BASE
    bases = [_BASE] if _BASE else BASES
    last = None
    for b in bases:
        try:
            r = requests.get(b + path, params=params, headers=UA, timeout=20)
            if r.status_code == 429:
                time.sleep(2); r = requests.get(b + path, params=params, headers=UA, timeout=20)
            if r.ok:
                _BASE = b; time.sleep(SLEEP); return r.json()
            last = f"{r.status_code} {r.text[:200]}"
        except requests.RequestException as e:
            last = repr(e)
    raise RuntimeError(f"Kalshi GET {path} failed on all bases: {last}")

def oget(host, **params):
    r = requests.get(host, params=params, headers=UA, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Open-Meteo {host} -> {r.status_code}: {r.text[:200]}")
    time.sleep(SLEEP)
    return r.json()

def fnum(d, *keys, scale_cents_ok=True):
    """Pull first present numeric field; auto-descale legacy cent fields (>1.5 -> /100)."""
    for k in keys:
        v = d.get(k)
        if v in (None, ""):
            continue
        x = float(v)
        if scale_cents_ok and x > 1.5:
            x /= 100.0
        return x
    return None

# ---------------------------------------------------------------- fees & sizing
def kalshi_fee(price, n, mult=0.07):
    """Per-order fee, rounded UP to next cent (schedule eff. 2026-02-05). Maker mult=0.0175."""
    if n <= 0:
        return 0.0
    cents = mult * n * price * (1.0 - price) * 100.0
    return math.ceil(round(cents, 6)) / 100.0

def fee_rate(price, mult=0.07):
    """Unrounded per-contract fee, for EV display."""
    return mult * price * (1.0 - price)

def size_for(cost, risk_cap=RISK_CAP, depth=None):
    n = int(math.floor(round(risk_cap / cost, 9))) if cost > 0 else 0
    if depth is not None:
        n = min(n, int(depth))
    return max(n, 0)

# ---------------------------------------------------------------- orderbook walking
def _levels(side):
    out = []
    for lvl in side or []:
        p, q = float(lvl[0]), float(lvl[1])
        if p > 1.5:
            p /= 100.0
        out.append((p, q))
    return out

def walk_fill(book, side, n):
    """
    Taker fill for n contracts. Kalshi book = resting BIDS on 'yes' and 'no'.
    Buy YES -> consume NO bids (yes cost = 1 - no_bid), best = highest no_bid.
    Buy NO  -> consume YES bids (no cost = 1 - yes_bid).
    Returns (vwap_cost, filled_n, worst_cost) or (None, 0, None) if book empty.
    """
    ob = book.get("orderbook", book)
    ladder = _levels(ob.get("no") if side == "yes" else ob.get("yes"))
    ladder.sort(key=lambda x: -x[0])
    filled, spend, worst = 0, 0.0, None
    for p_bid, q in ladder:
        take = min(n - filled, int(q))
        if take <= 0:
            break
        cost = 1.0 - p_bid
        spend += cost * take; filled += take; worst = cost
    if filled == 0:
        return None, 0, None
    return spend / filled, filled, worst

# ---------------------------------------------------------------- buckets & model
def bucket_bounds(m):
    """
    Continuous (lo, hi) for P(max temp in bucket). Settlement is an INTEGER deg-F from the
    NWS climate report; Kalshi temp strikes are usually x.5 already. If a strike parses as
    an integer, widen by 0.5 to cover the rounding cell.
    TODO(chris): verify inclusivity against one market's rules_primary before trusting.
    """
    st = (m.get("strike_type") or "").lower()
    lo = fnum(m, "floor_strike", scale_cents_ok=False)
    hi = fnum(m, "cap_strike", scale_cents_ok=False)
    def adj(x, d):
        if x is None:
            return None
        return x + d if abs(x - round(x)) < 1e-9 else x
    if st == "between":
        return adj(lo, -0.5), adj(hi, +0.5)
    if st == "greater":                                       # strict > strike -> {strike+1,...}
        return adj(lo, +0.5), math.inf
    if st == "greater_or_equal":                              # inclusive >= strike -> {strike,...}
        return adj(lo, -0.5), math.inf
    if st == "less":                                          # strict < strike -> {...,strike-1}
        s = hi if hi is not None else lo
        return -math.inf, adj(s, -0.5)
    if st == "less_or_equal":                                 # inclusive <= strike -> {...,strike}
        s = hi if hi is not None else lo
        return -math.inf, adj(s, +0.5)
    return None, None

def norm_cdf(x, mu, sd):
    return 0.5 * (1.0 + math.erf((x - mu) / (sd * math.sqrt(2))))

def gaussian_bucket_p(fc, mu, sd, lo, hi):
    sd = max(sd, 1.0)
    a = 0.0 if lo == -math.inf else norm_cdf(lo, fc + mu, sd)
    b = 1.0 if hi == math.inf else norm_cdf(hi, fc + mu, sd)
    return max(min(b - a, 1.0), 0.0)

def ensemble_bucket_p(members_max, mu, lo, hi):
    if not members_max:
        return None
    hits = sum(1 for t in members_max if (lo == -math.inf or round(t + mu) >= lo)
               and (hi == math.inf or round(t + mu) <= hi))
    return hits / len(members_max)

def ensemble_daily_max(city, target_date):
    """Live ensemble members' max temp (deg F) for target local date. Returns list."""
    c = CITIES[city]
    j = oget("https://ensemble-api.open-meteo.com/v1/ensemble",
             latitude=c["lat"], longitude=c["lon"], hourly="temperature_2m",
             models=ENSEMBLE_MODELS, temperature_unit="fahrenheit",
             timezone=c["tz"], forecast_days=2)
    h = j["hourly"]; times = h["time"]
    idx = [i for i, t in enumerate(times) if t.startswith(target_date.isoformat())]
    out = []
    for k, series in h.items():
        if not k.startswith("temperature_2m"):
            continue
        vals = [series[i] for i in idx if series[i] is not None]
        if vals:
            out.append(max(vals))
    return out

def prevday1_daily_max(city, start, end):
    """{date: max temp forecast issued ~24h earlier} — no look-ahead (Previous Runs API)."""
    c = CITIES[city]
    j = oget("https://previous-runs-api.open-meteo.com/v1/forecast",
             latitude=c["lat"], longitude=c["lon"],
             hourly="temperature_2m_previous_day1", temperature_unit="fahrenheit",
             timezone=c["tz"], start_date=start.isoformat(), end_date=end.isoformat())
    h = j["hourly"]; out = {}
    for t, v in zip(h["time"], h["temperature_2m_previous_day1"]):
        if v is None:
            continue
        d = t[:10]; out[d] = max(out.get(d, -999.0), v)
    return {date.fromisoformat(k): v for k, v in out.items()}

def actual_daily_max(city, start, end):
    c = CITIES[city]
    j = oget("https://historical-forecast-api.open-meteo.com/v1/forecast",
             latitude=c["lat"], longitude=c["lon"], daily="temperature_2m_max",
             temperature_unit="fahrenheit", timezone=c["tz"],
             start_date=start.isoformat(), end_date=end.isoformat())
    d = j["daily"]
    return {date.fromisoformat(t): v for t, v in zip(d["time"], d["temperature_2m_max"])
            if v is not None}

def fit_error(city, fit_start, fit_end):
    """(mu, sigma) of actual - prevday1 forecast on a window BEFORE the backtest starts.
    This absorbs grid-vs-station bias — the #1 way this strategy silently loses money.
    TODO(chris): eyeball residuals per city; refit monthly; sigma floors at 1.5F."""
    fc = prevday1_daily_max(city, fit_start, fit_end)
    ac = actual_daily_max(city, fit_start, fit_end)
    res = [ac[d] - fc[d] for d in fc if d in ac]
    if len(res) < 20:
        print(f"  [warn] {city}: only {len(res)} fit days; using mu=0, sigma=3.0")
        return 0.0, 3.0
    mu = sum(res) / len(res)
    sd = (sum((r - mu) ** 2 for r in res) / (len(res) - 1)) ** 0.5
    return mu, max(sd, 1.5) + CITIES[city]["sigma_extra"]

# ---------------------------------------------------------------- kalshi data
def settled_markets(series, start_ts, end_ts):
    out, cursor = [], None
    while True:
        p = dict(series_ticker=series, status="settled", limit=1000,
                 min_close_ts=start_ts, max_close_ts=end_ts)
        if cursor:
            p["cursor"] = cursor
        j = kget("/markets", **p)
        out += j.get("markets", [])
        cursor = j.get("cursor")
        if not cursor or not j.get("markets"):
            return out

import re as _re
_MON = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
        'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
def event_date_from_ticker(ticker):
    """Parse event date from 'KXHIGHNY-26JUL18-T86' -> date(2026,7,18)."""
    m = _re.search(r'-(\d{2})([A-Z]{3})(\d{2})(?:-|$)', ticker)
    if not m: return None
    yy, mon, dd = m.groups()
    if mon not in _MON: return None
    try: return date(2000+int(yy), _MON[mon], int(dd))
    except ValueError: return None

def candles(series, ticker, start_ts, end_ts, interval=60):
    for path in (f"/series/{series}/markets/{ticker}/candlesticks",
                 f"/historical/markets/{ticker}/candlesticks"):
        try:
            j = kget(path, start_ts=start_ts, end_ts=end_ts, period_interval=interval)
            cs = j.get("candlesticks") or []
            if cs:
                return cs
        except RuntimeError:
            continue
    return []

def candle_quote(c):
    def side(name):
        s = c.get(name) or {}
        return fnum(s, "close_dollars", "close")
    bid, ask = side("yes_bid"), side("yes_ask")
    vol = float(c.get("volume_fp") or c.get("volume") or 0)
    return bid, ask, vol

def series_meta(series, cache={}):
    if series not in cache:
        try:
            s = kget(f"/series/{series}").get("series", {})
        except RuntimeError:
            s = {}
        m = s.get("fee_multiplier")
        try:
            m = float(m)
        except (TypeError, ValueError):
            m = None
        cache[series] = dict(category=s.get("category", "?"),
                             mult=m if (m and 0 < m <= 0.2) else 0.07)
    return cache[series]

# ---------------------------------------------------------------- subcommands
def cmd_discover(_):
    print("Series with 'temperature' in the title (fix CITIES to match):\n")
    found = 0
    try:
        for s in kget("/series", category="Climate and Weather").get("series", []):
            if "temp" in (s.get("title") or "").lower():
                print(f"  {s['ticker']:<16} {s.get('title','')}"); found += 1
    except RuntimeError as e:
        print(f"  /series filter failed ({e}); falling back to open events scan")
    if not found:
        cursor = None
        for _ in range(5):
            p = dict(status="open", limit=200)
            if cursor:
                p["cursor"] = cursor
            j = kget("/events", **p)
            for ev in j.get("events", []):
                if "high temp" in (ev.get("title") or "").lower():
                    print(f"  {ev.get('series_ticker','?'):<16} {ev.get('title','')}"); found += 1
            cursor = j.get("cursor")
            if not cursor:
                break
    if not found:
        print("  Nothing found — inspect one /events page manually; ticker patterns may have changed.")

def todays_event(series, tz):
    today = datetime.now(ZoneInfo(tz)).date().isoformat()
    evs = kget("/events", series_ticker=series, status="open",
               with_nested_markets=True).get("events", [])
    for ev in evs:
        mkts = ev.get("markets") or []
        if any(today in (m.get("close_time") or "") or today in (m.get("expected_expiration_time") or "")
               for m in mkts):
            return ev
    return evs[0] if evs else None

def cmd_snapshot(a):
    rows, now = [], datetime.now(timezone.utc).isoformat(timespec="seconds")
    for city in a.cities.split(","):
        c = CITIES[city]; meta = series_meta(c["series"]); mult = meta["mult"]
        ev = todays_event(c["series"], c["tz"])
        if not ev:
            print(f"[{city}] no open event found"); continue
        members = ensemble_daily_max(city, datetime.now(ZoneInfo(c["tz"])).date())
        print(f"\n[{city}] {ev.get('title','?')}  ensemble members={len(members)}  fee_mult={mult}")
        print(f"  {'ticker':<26}{'bucket':<14}{'p_mod':>6}{'bid':>6}{'ask':>6}"
              f"{'EVyes':>7}{'EVno':>7}{'fill@$40':>9}")
        for m in ev.get("markets", []):
            lo, hi = bucket_bounds(m)
            if lo is None and hi is None:
                continue
            p = ensemble_bucket_p(members, c["bias_mu"], lo, hi)
            if p is None:
                continue
            book = kget(f"/markets/{m['ticker']}/orderbook")
            bid = fnum(m, "yes_bid_dollars", "yes_bid")
            ask = fnum(m, "yes_ask_dollars", "yes_ask")
            ev_yes = ev_no = None
            vy, fy, _ = walk_fill(book, "yes", size_for(ask or 0.99))
            if vy:
                ev_yes = p - vy - fee_rate(vy, mult)
            vn, fn_, _ = walk_fill(book, "no", size_for(1 - (bid or 0.01)))
            if vn:
                ev_no = (1 - p) - vn - fee_rate(vn, mult)
            tag = " <== " if max(x for x in (ev_yes, ev_no, -9) if x is not None) >= EV_THRESHOLD else ""
            print(f"  {m['ticker']:<26}{str(lo)+'-'+str(hi):<14}{p:>6.2f}"
                  f"{(bid if bid is not None else float('nan')):>6.2f}"
                  f"{(ask if ask is not None else float('nan')):>6.2f}"
                  f"{(ev_yes if ev_yes is not None else float('nan')):>7.3f}"
                  f"{(ev_no if ev_no is not None else float('nan')):>7.3f}"
                  f"{fy:>5}/{fn_:<3}{tag}")
            rows.append(dict(ts=now, city=city, ticker=m["ticker"], lo=lo, hi=hi, p_model=p,
                             yes_bid=bid, yes_ask=ask, vwap_yes=vy, vwap_no=vn,
                             ev_yes=ev_yes, ev_no=ev_no, n_members=len(members)))
    if rows:
        newf = not os.path.exists(a.log)
        with open(a.log, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            if newf:
                w.writeheader()
            w.writerows(rows)
        print(f"\nappended {len(rows)} rows -> {a.log}  (this file IS the paper-trade record)")

def cmd_backtest(a):
    import pandas as pd
    start, end = date.fromisoformat(a.start), date.fromisoformat(a.end)
    all_tr, briers = [], []
    for city in a.cities.split(","):
        c = CITIES[city]; tz = ZoneInfo(c["tz"])
        meta = series_meta(c["series"]); mult = meta["mult"]
        mu, sd = fit_error(city, start - timedelta(days=a.fit_days), start - timedelta(days=1))
        fcs = prevday1_daily_max(city, start, end)
        print(f"[{city}] fitted mu={mu:+.2f}F sigma={sd:.2f}F on {a.fit_days}d pre-window; "
              f"{len(fcs)} forecast days; fee_mult={mult}")
        t0 = int(datetime(start.year, start.month, start.day, tzinfo=tz).timestamp())
        t1 = int((datetime(end.year, end.month, end.day, tzinfo=tz) + timedelta(days=2)).timestamp())
        mkts = [m for m in settled_markets(c["series"], t0, t1)
                if m.get("result") in ("yes", "no") and not m.get("mve_collection_ticker")]
        print(f"[{city}] settled strike markets in range: {len(mkts)}")
        skipped = dict(no_edate=0, edate_not_in_fcs=0, no_candles=0, no_quote=0, no_bounds=0)
        for m in mkts:
            eday = event_date_from_ticker(m["ticker"])
            if eday is None:
                close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
                eday = close.astimezone(tz).date()
            if eday not in fcs:
                skipped["edate_not_in_fcs"] += 1; continue
            dts = int(datetime(eday.year, eday.month, eday.day, DECISION_HOUR_LOCAL,
                               tzinfo=tz).timestamp())
            cs = candles(c["series"], m["ticker"], dts - 24 * 3600, dts + 3600, 60)
            cs = [x for x in cs if int(x.get("end_period_ts", 0)) <= dts]
            if not cs:
                skipped["no_candles"] += 1; continue
            bid, ask, vol = candle_quote(cs[-1])
            if bid is None or ask is None:
                skipped["no_quote"] += 1; continue
            lo, hi = bucket_bounds(m)
            if lo is None and hi is None:
                skipped["no_bounds"] += 1; continue
            p = gaussian_bucket_p(fcs[eday], mu, sd, lo, hi)
            y = 1.0 if m["result"] == "yes" else 0.0
            briers.append(dict(city=city, model=(p - y) ** 2, market=((bid + ask) / 2 - y) ** 2))
            for side, pr, cost in (("yes", p, ask + BACKTEST_SLIP),
                                   ("no", 1 - p, 1 - bid + BACKTEST_SLIP)):
                if not (0 < cost < 1):
                    continue
                evx = pr - cost - fee_rate(cost, mult)
                if evx < EV_THRESHOLD:
                    continue
                n = size_for(cost, depth=PARTICIPATION * vol)
                if n < 1:
                    continue
                won = (side == m["result"])
                pnl = ((1.0 if won else 0.0) - cost) * n - kalshi_fee(cost, n, mult)
                all_tr.append(dict(city=city, date=eday, ticker=m["ticker"], side=side,
                                   p_model=round(pr, 3), cost=round(cost, 3), n=n,
                                   ev_pred=round(evx, 3), won=won, pnl=round(pnl, 2)))
        print(f"[{city}] skipped: {skipped}")
    if not briers:
        print("No evaluable markets. Check tickers (discover), date range, decision hour."); return
    bd, td = pd.DataFrame(briers), pd.DataFrame(all_tr)
    print("\n=== Brier (lower=better): does the model beat the market at the decision hour? ===")
    print(bd.groupby("city").agg(n=("model", "size"), brier_model=("model", "mean"),
                                 brier_market=("market", "mean")).round(4).to_string())
    print(f"\nOVERALL n={len(bd)}  model={bd.model.mean():.4f}  market={bd.market.mean():.4f}  "
          f"edge={bd.market.mean()-bd.model.mean():+.4f}  (gate: >= +0.0100)")
    if len(td):
        td.to_csv(a.out, index=False)
        print(f"\n=== Threshold rule (EV>={EV_THRESHOLD}, slip={BACKTEST_SLIP}, "
              f"participation={PARTICIPATION}) ===")
        g = td.groupby("city").agg(trades=("pnl", "size"), hit=("won", "mean"),
                                   avg_ev_pred=("ev_pred", "mean"), pnl=("pnl", "sum")).round(3)
        print(g.to_string())
        print(f"TOTAL trades={len(td)}  pnl=${td.pnl.sum():.2f}  -> {a.out}")
        print("Treat a positive number with suspicion until the paper log agrees (Section 5 gates).")
    else:
        print("Threshold rule found zero trades — either no edge at this hour or threshold too high.")

def cmd_calibration_pull(a):
    import pandas as pd
    end_ts = int(time.time()); start_ts = end_ts - a.days * 86400

    # 1) List all series and drop the parlay generators (they dominate settled volume).
    print("fetching series list...")
    all_series = kget("/series").get("series", [])
    def is_parlay(s):
        t = s.get("ticker", "")
        return (t.startswith("KXMV") or "MULTI" in t or "EXTENDED" in t
                or "CROSSCATEGORY" in t or "MVE" in t)
    ser = [s for s in all_series if not is_parlay(s)]
    ser.sort(key=lambda s: -float(s.get("volume_fp") or 0))
    print(f"got {len(all_series)} series total; {len(ser)} non-parlay")
    if a.series:
        wanted = set(a.series.split(","))
        ser = [s for s in ser if s["ticker"] in wanted]
        print(f"filtered to {len(ser)} matching --series")
    else:
        ser = ser[: a.max_series]
        print(f"top {len(ser)} by lifetime volume")

    # 2) Per series, pull settled markets in window.
    rows = []
    for i, s in enumerate(ser):
        stk = s["ticker"]; cat = s.get("category", "?")
        cursor = None
        while True:
            p = dict(series_ticker=stk, status="settled", limit=1000,
                     min_close_ts=start_ts, max_close_ts=end_ts)
            if cursor:
                p["cursor"] = cursor
            try:
                j = kget("/markets", **p)
            except RuntimeError:
                break
            batch = j.get("markets", [])
            for m in batch:
                if m.get("result") not in ("yes", "no"):
                    continue
                if float(m.get("volume_fp") or 0) < a.min_volume:
                    continue
                m["_series"] = stk; m["_category"] = cat
                rows.append(m)
            cursor = j.get("cursor")
            if not cursor:
                break
        if (i + 1) % 25 == 0:
            print(f"  ...scanned {i+1}/{len(ser)} series, {len(rows)} markets kept")
        if len(rows) >= a.max_markets:
            break
    print(f"kept {len(rows)} markets across {len(set(r['_series'] for r in rows))} series")
    if not rows:
        print("nothing to calibrate — try --min-volume 0 or a wider --days window."); return

    # 3) For each, T-horizon_hours candlestick price.
    out = []
    for i, m in enumerate(rows[: a.max_candles]):
        series = m["_series"]
        close = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        tts = int(close.timestamp()) - a.horizon_hours * 3600
        cs = candles(series, m["ticker"], tts - 6 * 3600, tts + 3600, 60)
        cs = [x for x in cs if int(x.get("end_period_ts", 0)) <= tts]
        if not cs:
            continue
        px = fnum(cs[-1].get("price") or {},
                  "close_dollars", "close", "previous_dollars", "previous")
        if px is None:
            continue
        out.append(dict(ticker=m["ticker"], series=series, category=m["_category"],
                        p_t=px, y=1 if m["result"] == "yes" else 0,
                        volume=float(m.get("volume_fp") or 0), close=m["close_time"]))
        if (i + 1) % 100 == 0:
            print(f"  ...fetched candles for {i+1}/{min(len(rows), a.max_candles)}")
    df = pd.DataFrame(out)
    if df.empty:
        print("No candlestick prices recovered — widen --days or check the historical endpoint."); return
    df.to_csv(a.out, index=False)

    def wilson(s, n, z=1.96):
        ph = s / n; d = 1 + z * z / n
        c0 = ph + z * z / (2 * n); mrg = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
        return (c0 - mrg) / d, (c0 + mrg) / d
    print(f"\nT-{a.horizon_hours}h calibration, n={len(df)} -> {a.out}")
    edges = [0, .05, .15, .25, .35, .45, .55, .65, .75, .85, .95, 1.001]
    for i in range(len(edges) - 1):
        b = df[(df.p_t >= edges[i]) & (df.p_t < edges[i + 1])]
        if len(b) == 0:
            continue
        loq, hiq = wilson(b.y.sum(), len(b))
        flag = "  n<30 TOO SMALL" if len(b) < 30 else ""
        print(f"  [{edges[i]:.2f},{edges[i+1]:.2f}) n={len(b):4d} mean_p={b.p_t.mean():.3f} "
              f"yes={b.y.mean():.3f} CI=[{loq:.3f},{hiq:.3f}]{flag}")
    print("\nBy category (Brier lower=sharper; large |yes-mean_p| = mispriced):")
    g = df.groupby("category").agg(n=("y", "size"), mean_p=("p_t", "mean"), yes=("y", "mean"))
    g["brier"] = df.groupby("category").apply(lambda x: ((x.p_t - x.y) ** 2).mean(),
                                              include_groups=False)
    g["gap"] = (g["yes"] - g["mean_p"]).round(3)
    g["flag"] = ["n<30 TOO SMALL" if n < 30 else "" for n in g.n]
    print(g.round(3).to_string())

def cmd_poly_calibration(a):
    """Polymarket calibration: recent resolved CLOB-active markets + T-{horizon}h prices."""
    import pandas as pd
    GAMMA = "https://gamma-api.polymarket.com"
    CLOB = "https://clob.polymarket.com"

    end_ts_now = int(time.time())
    min_end_iso = datetime.fromtimestamp(end_ts_now - a.days * 86400,
                                          tz=timezone.utc).isoformat()
    print(f"fetching Polymarket resolved markets closed since {min_end_iso[:10]} "
          f"with volumeNum >= ${a.min_volume:,.0f}...")
    markets, offset, LIMIT = [], 0, 100
    while len(markets) < a.max_markets and offset < 2000:            # gamma caps offset at ~2000
        params = {"closed": "true", "limit": LIMIT, "offset": offset,
                  "end_date_min": min_end_iso, "volume_num_min": a.min_volume,
                  "order": "volumeNum", "ascending": "false"}
        try:
            r = requests.get(GAMMA + "/markets", params=params, headers=UA, timeout=30)
        except requests.RequestException as e:
            print(f"gamma error: {e}"); break
        if not r.ok:
            print(f"gamma {r.status_code}: {r.text[:200]}"); break
        batch = r.json()
        if not batch:
            break
        markets.extend(batch)
        offset += LIMIT
        time.sleep(SLEEP)
        if len(batch) < LIMIT:
            break
        if offset % 500 == 0:
            print(f"  ...paginated {offset} markets; smallest volume in batch "
                  f"${float(batch[-1].get('volumeNum') or 0):,.0f}")
    print(f"got {len(markets)} closed markets with volume >= ${a.min_volume:,.0f}")

    good, skip = [], dict(not_binary=0, not_resolved=0, low_vol=0, no_clob=0, bad_tokens=0)
    for m in markets:
        try:
            outcomes = json.loads(m.get("outcomes") or "[]")
            op = json.loads(m.get("outcomePrices") or "[]")
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            if outcomes != ["Yes", "No"] or len(tokens) != 2:
                skip["not_binary"] += 1; continue
            op = [float(x) for x in op]
            if len(op) != 2 or max(op) < 0.99:
                skip["not_resolved"] += 1; continue
            vol = float(m.get("volumeNum") or 0)
            if vol < a.min_volume:
                skip["low_vol"] += 1; continue
            # Filter to markets that actually traded on CLOB (pre-2023 AMM markets
            # have tokens but no CLOB history — the whole reason last run recovered zero).
            clob_vol = float(m.get("volume1yrClob") or m.get("volumeClob") or 0)
            if clob_vol < 100:
                skip["no_clob"] += 1; continue
            if not (str(tokens[0]).isdigit() and str(tokens[1]).isdigit()):
                skip["bad_tokens"] += 1; continue
            yes_won = 1 if op[0] > op[1] else 0
            end = m.get("endDate") or m.get("endDateIso") or ""
            cat = ((m.get("category") or "").strip()
                   or ",".join(t.get("label", "") for t in (m.get("events") or [{}])[0].get("tags", [])[:2])
                   or "?")
            good.append(dict(yes_token=tokens[0], yes_won=yes_won, cat=cat,
                             vol=vol, end=end, q=(m.get("question") or "")[:60]))
        except (json.JSONDecodeError, ValueError, TypeError, KeyError, IndexError):
            continue
    print(f"{len(good)} markets pass all filters; skipped: {skip}")
    if not good:
        print("no markets to price. Try lower --min-volume or wider --days."); return

    out, empty_count = [], 0
    for i, g in enumerate(good[: a.max_prices]):
        try:
            end_dt = datetime.fromisoformat(g["end"].replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        target_ts = int(end_dt.timestamp()) - a.horizon_hours * 3600
        px = None
        for params in (
            dict(market=g["yes_token"], interval="max", fidelity=60),   # whole history
            dict(market=g["yes_token"], startTs=target_ts - 5 * 86400,
                 endTs=target_ts + 3 * 3600, fidelity=60),
        ):
            try:
                r = requests.get(CLOB + "/prices-history", params=params,
                                 headers=UA, timeout=20)
                if not r.ok:
                    continue
                pts = r.json().get("history", [])
                if not pts:
                    continue
                valid = [p for p in pts if p.get("t", 0) <= target_ts]
                if not valid:
                    continue                                           # market not open yet at T-horizon
                px = float(valid[-1]["p"])
                break
            except (requests.RequestException, json.JSONDecodeError,
                    ValueError, KeyError, IndexError):
                continue
        time.sleep(SLEEP)
        if px is None:
            empty_count += 1; continue
        out.append(dict(cat=g["cat"], p_t=px, y=g["yes_won"], vol=g["vol"],
                        end=g["end"], question=g["q"]))
        if (i + 1) % 50 == 0:
            print(f"  ...fetched {i+1}/{min(len(good), a.max_prices)}, "
                  f"recovered {len(out)}, empty {empty_count}")

    df = pd.DataFrame(out)
    if df.empty:
        print(f"no prices recovered ({empty_count} empty responses). "
              f"Possible causes: CLOB endpoint changed, or all markets are pre-CLOB despite the filter."); return
    df.to_csv(a.out, index=False)
    print(f"\nrecovered {len(df)} / {len(good[:a.max_prices])} attempted "
          f"({empty_count} empty CLOB responses)")

    def wilson(s, n, z=1.96):
        ph = s / n; d = 1 + z * z / n
        c0 = ph + z * z / (2 * n); mrg = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
        return (c0 - mrg) / d, (c0 + mrg) / d
    print(f"\nT-{a.horizon_hours}h Polymarket calibration, n={len(df)} -> {a.out}")
    edges = [0, .05, .15, .25, .35, .45, .55, .65, .75, .85, .95, 1.001]
    for i in range(len(edges) - 1):
        b = df[(df.p_t >= edges[i]) & (df.p_t < edges[i + 1])]
        if len(b) == 0:
            continue
        loq, hiq = wilson(b.y.sum(), len(b))
        flag = "  n<30 TOO SMALL" if len(b) < 30 else ""
        print(f"  [{edges[i]:.2f},{edges[i+1]:.2f}) n={len(b):4d} mean_p={b.p_t.mean():.3f} "
              f"yes={b.y.mean():.3f} gap={b.y.mean()-b.p_t.mean():+.3f} "
              f"CI=[{loq:.3f},{hiq:.3f}]{flag}")
    print("\nBy category (sorted by n, gap = yes_rate - mean_price):")
    g = df.groupby("cat").agg(n=("y", "size"), mean_p=("p_t", "mean"), yes=("y", "mean"))
    g["brier"] = df.groupby("cat").apply(lambda x: ((x.p_t - x.y) ** 2).mean(),
                                         include_groups=False)
    g["gap"] = (g["yes"] - g["mean_p"]).round(3)
    g["flag"] = ["n<30 TOO SMALL" if n < 30 else "" for n in g.n]
    print(g.sort_values("n", ascending=False).round(3).to_string())

def cmd_poly_smartmoney(a):
    """Identify smart-money wallets via Polymarket's /v1/leaderboard endpoint.
    Cross-refs ALL-time PnL with 30d activity so lucky-once wallets are dropped.
    Also pulls POLITICS category specifically since H3 lives there.
    Endpoint: docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings"""
    import pandas as pd
    DATA = "https://data-api.polymarket.com"

    def fetch_leaderboard(category, time_period):
        rows, offset = [], 0
        while offset <= 1000 and len(rows) < a.top_n + 50:
            params = dict(category=category, timePeriod=time_period,
                          orderBy="PNL", limit=50, offset=offset)
            try:
                r = requests.get(DATA + "/v1/leaderboard", params=params,
                                 headers=UA, timeout=25)
            except requests.RequestException as e:
                print(f"  {category}/{time_period}: {e}"); break
            if not r.ok:
                print(f"  {category}/{time_period}: {r.status_code} {r.text[:150]}"); break
            batch = r.json() or []
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch); offset += 50
            time.sleep(SLEEP)
            if len(batch) < 50:
                break
        return rows

    print("fetching OVERALL/ALL leaderboard...")
    all_time = fetch_leaderboard("OVERALL", "ALL")
    print(f"  got {len(all_time)} entries")
    if not all_time:
        print("empty response — check API status"); return
    print(f"  sample: {json.dumps(all_time[0], indent=2)[:300]}")

    print("fetching OVERALL/MONTH leaderboard (recency filter)...")
    recent = fetch_leaderboard("OVERALL", "MONTH")
    print(f"  got {len(recent)} entries")

    print("fetching POLITICS/ALL leaderboard (H3-relevant category)...")
    politics = fetch_leaderboard("POLITICS", "ALL")
    print(f"  got {len(politics)} entries")

    def to_df(rows, prefix):
        rec = []
        for r in rows:
            w = (r.get("proxyWallet") or "").lower()
            if not w.startswith("0x"):
                continue
            rec.append({
                "wallet": w,
                "name": r.get("userName") or "",
                "verified": bool(r.get("verifiedBadge")),
                f"{prefix}pnl": float(r.get("pnl") or 0),
                f"{prefix}vol": float(r.get("vol") or 0),
                f"{prefix}rank": int(r.get("rank") or 0) if r.get("rank") else None,
            })
        return pd.DataFrame(rec)

    A, R, P = to_df(all_time, "all_"), to_df(recent, "r30_"), to_df(politics, "pol_")
    if A.empty:
        print("parsed 0 wallets — schema drift, check sample above"); return

    df = A[["wallet", "name", "verified", "all_pnl", "all_vol", "all_rank"]].copy()
    if not R.empty:
        df = df.merge(R[["wallet", "r30_pnl", "r30_rank"]], on="wallet", how="left")
    if not P.empty:
        df = df.merge(P[["wallet", "pol_pnl", "pol_rank"]], on="wallet", how="left")

    # Smart = top-N all-time AND active in 30d (drop lucky-once wallets).
    if "r30_rank" in df.columns and df["r30_rank"].notna().any():
        active = df[df["r30_rank"].notna()]
        print(f"\n{len(active)}/{len(df)} all-time top wallets active in last 30d")
        smart = active
    else:
        print(f"\nno recency data; keeping all {len(df)} all-time entries")
        smart = df
    smart = smart.sort_values("all_rank").head(a.top_n).reset_index(drop=True)
    smart.to_csv(a.out, index=False)
    print(f"saved {len(smart)} smart wallets -> {a.out}")

    show = ["all_rank", "wallet", "name", "verified", "all_pnl", "all_vol"]
    if "r30_rank" in smart.columns: show.insert(-2, "r30_rank")
    if "pol_rank" in smart.columns: show.append("pol_rank")
    print(f"\ntop 20 by all-time PnL:")
    with pd.option_context("display.width", 200, "display.max_colwidth", 30):
        print(smart[show].head(20).to_string(index=False))

    if not P.empty:
        pol_in_smart = smart[smart["pol_rank"].notna()] if "pol_rank" in smart.columns else pd.DataFrame()
        print(f"\n{len(pol_in_smart)}/{len(smart)} smart wallets also rank in POLITICS top-{a.top_n}"
              f" — those are the H3-relevant follow candidates")

def cmd_poly_scan(a):
    """Find LIVE Polymarket markets matching H3 criteria (Yes/No, low-price, short-deadline)
    and rank by smart-wallet YES holdings. Requires poly-smartmoney run first."""
    import pandas as pd
    GAMMA = "https://gamma-api.polymarket.com"
    DATA = "https://data-api.polymarket.com"

    try:
        smart = pd.read_csv(a.smart_file)
    except FileNotFoundError:
        print(f"missing {a.smart_file} — run: python kalshi_weather_edge.py poly-smartmoney"); return
    wallets = [w.lower() for w in smart["wallet"].tolist() if isinstance(w, str) and w.startswith("0x")]
    print(f"loaded {len(wallets)} smart wallets from {a.smart_file}")

    now_dt = datetime.now(timezone.utc)
    end_min_iso = now_dt.isoformat()
    end_max_iso = (now_dt + timedelta(days=a.max_days)).isoformat()
    print(f"fetching live Yes/No markets closing in next {a.max_days} days "
          f"with volume >= ${a.min_volume:,.0f}...")
    markets, offset, LIMIT = [], 0, 100
    while len(markets) < a.max_markets and offset < 2000:
        params = {"closed": "false", "active": "true", "limit": LIMIT, "offset": offset,
                  "end_date_min": end_min_iso, "end_date_max": end_max_iso,
                  "volume_num_min": a.min_volume,
                  "order": "volumeNum", "ascending": "false"}
        try:
            r = requests.get(GAMMA + "/markets", params=params, headers=UA, timeout=30)
        except requests.RequestException as e:
            print(f"gamma error: {e}"); break
        if not r.ok:
            print(f"gamma {r.status_code}: {r.text[:200]}"); break
        batch = r.json() or []
        if not batch:
            break
        markets.extend(batch); offset += LIMIT
        time.sleep(SLEEP)
        if len(batch) < LIMIT:
            break
    print(f"got {len(markets)} live markets in window")

    candidates = []
    for m in markets:
        try:
            oc = json.loads(m.get("outcomes") or "[]")
            if oc != ["Yes", "No"]:
                continue
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            if len(tokens) != 2:
                continue
            price = None
            lp = m.get("lastTradePrice")
            if lp not in (None, ""):
                try: price = float(lp)
                except (ValueError, TypeError): pass
            if price is None:
                try:
                    op = json.loads(m.get("outcomePrices") or "[]")
                    price = float(op[0]) if op else None
                except (json.JSONDecodeError, ValueError, IndexError): pass
            if price is None or not (a.min_price <= price <= a.max_price):
                continue
            try:
                end_dt = datetime.fromisoformat(m.get("endDate", "").replace("Z", "+00:00"))
                days_left = (end_dt - now_dt).total_seconds() / 86400
            except (ValueError, AttributeError):
                days_left = None
            if days_left is not None and days_left < a.min_days:
                continue
            candidates.append(dict(
                cid=m.get("conditionId"), yes_token=str(tokens[0]),
                price=round(price, 4), volume=float(m.get("volumeNum") or 0),
                days_left=round(days_left, 1) if days_left else None,
                question=(m.get("question") or "")[:70],
                n_smart=0, smart_yes_shares=0.0, smart_avg_entry=None))
        except (json.JSONDecodeError, ValueError, TypeError, KeyError):
            continue
    print(f"{len(candidates)} candidates: Yes/No, price {a.min_price}-{a.max_price}, "
          f">= {a.min_days}d to close")
    if not candidates:
        print("nothing matched — try wider --min-price/--max-price or --min-days 0"); return

    interesting_cids = {c["cid"] for c in candidates}
    interesting_yes_tokens = {c["yes_token"] for c in candidates}

    print(f"\nfetching positions for {len(wallets)} smart wallets (est ~{len(wallets)*SLEEP:.0f}s)...")
    holdings = {}                                # cid -> [(wallet, size, avgPrice)]
    for i, w in enumerate(wallets):
        try:
            r = requests.get(DATA + "/positions", params={"user": w},
                             headers=UA, timeout=20)
            if not r.ok:
                continue
            positions = r.json() or []
        except (requests.RequestException, json.JSONDecodeError):
            continue
        for p in (positions if isinstance(positions, list) else []):
            cid = p.get("conditionId")
            asset = str(p.get("asset") or "")
            size = float(p.get("size") or 0)
            if cid in interesting_cids and asset in interesting_yes_tokens and size > 0:
                holdings.setdefault(cid, []).append(
                    (w, size, float(p.get("avgPrice") or 0)))
        time.sleep(SLEEP)
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(wallets)} wallets scanned, hits so far: "
                  f"{sum(len(v) for v in holdings.values())}")

    for c in candidates:
        h = holdings.get(c["cid"], [])
        c["n_smart"] = len(h)
        c["smart_yes_shares"] = sum(x[1] for x in h)
        c["smart_avg_entry"] = (sum(x[1] * x[2] for x in h) / c["smart_yes_shares"]
                                if c["smart_yes_shares"] > 0 else None)

    df = pd.DataFrame(candidates).sort_values(
        ["n_smart", "volume"], ascending=[False, False]).reset_index(drop=True)
    df.to_csv(a.out, index=False)
    print(f"\nsaved {len(df)} candidates -> {a.out}")

    hits = df[df.n_smart >= a.min_smart]
    print(f"\n{len(hits)} markets with >= {a.min_smart} smart wallets net-long YES:")
    if len(hits) == 0:
        print("  (relax --min-smart to 1 to see any smart-money-tracked markets)")
        hits = df[df.n_smart > 0].head(20)
        if len(hits):
            print(f"\ntop {len(hits)} with any smart-money presence:")
    with pd.option_context("display.max_colwidth", 60, "display.width", 220):
        cols = ["n_smart", "price", "days_left", "volume", "smart_avg_entry", "question"]
        print(hits[cols].head(25).to_string(index=False))

def cmd_poly_backtest(a):
    """Simulate blind buy-YES-at-low-price strategy on poly_calibration.csv.
    Includes Polymarket fee schedule (geopolitics=0%, others=5% taker multiplier),
    topic decomposition, drawdown, and Sharpe. Answers: 'would $2k execution have worked?'"""
    import pandas as pd
    import numpy as np

    try:
        d = pd.read_csv(a.data)
    except FileNotFoundError:
        print(f"missing {a.data} — run: python kalshi_weather_edge.py poly-calibration"); return

    def topic(q):
        q = (q or "").lower()
        # Order matters — check specific before generic
        if any(k in q for k in ("iran", "khamenei", "israel", "lebanon", "hormuz", "kharg")):
            return "iran_israel"
        if any(k in q for k in ("cartel", "venezuela", "russia", "putin", "china",
                                 "qatar", "north korea", "honduras", "chile", "portugal")):
            return "geopol_other"
        if "shutdown" in q or "government" in q: return "US_policy"
        if "fed" in q or "interest rate" in q or "cpi" in q: return "fed"
        if any(k in q for k in ("bitcoin", "btc", "ether", "eth", "crypto")): return "crypto"
        if any(k in q for k in ("mayoral", "election", "president", "senate")): return "election"
        if any(k in q for k in ("trump", "biden", "harris")): return "trump_admin"
        if any(k in q for k in ("world cup", "champions", "masters", "boxing",
                                 "verstappen", "grand slam", "nba", "nfl")): return "sports"
        return "other"
    d["topic"] = d["question"].fillna("").map(topic)

    # Polymarket taker fee ≈ multiplier × price × (1-price). Geopolitics is fee-free.
    FEE_FREE_TOPICS = {"iran_israel", "geopol_other", "US_policy"}
    FEE_MULT = 0.05

    def simulate(sub, price_lo, price_hi, bet_dollars, bankroll):
        rows = sub[(sub.p_t >= price_lo) & (sub.p_t < price_hi)].copy()
        if len(rows) == 0:
            return None
        rows = rows.sort_values("end").reset_index(drop=True)
        pnls, running, peak, dd = [], bankroll, bankroll, 0.0
        losing_streak, longest_losing = 0, 0
        equity, dates, wins = [bankroll], [], []
        buying_yes = (a.side == "yes")
        for _, r in rows.iterrows():
            fee_mult = 0.0 if r.topic in FEE_FREE_TOPICS else FEE_MULT
            entry_price = r.p_t if buying_yes else (1.0 - r.p_t)
            fee_per_contract = fee_mult * entry_price * (1 - entry_price)
            contracts = bet_dollars / (entry_price + fee_per_contract)
            won = (r.y == 1) if buying_yes else (r.y == 0)
            payout = contracts * (1.0 if won else 0.0)
            cost = contracts * (entry_price + fee_per_contract)
            pnl = payout - cost
            pnls.append(pnl); running += pnl; peak = max(peak, running)
            dd = min(dd, running - peak)
            equity.append(running); dates.append(r.end); wins.append(won)
            if not won:
                losing_streak += 1; longest_losing = max(longest_losing, losing_streak)
            else:
                losing_streak = 0
        s = pd.Series(pnls)
        return dict(
            n=len(rows), hit_rate=float(sum(1 for p in pnls if p > 0) / len(pnls)),
            mean_yes_price=float(rows.p_t.mean()),
            total_pnl=float(s.sum()), mean_pnl=float(s.mean()),
            sharpe_per_trade=float(s.mean() / s.std()) if len(s) > 1 and s.std() > 0 else 0.0,
            max_dd=float(dd), max_dd_pct=float(dd / bankroll),
            longest_losing_streak=longest_losing,
            final_bankroll=float(running),
            roi_pct=float((running - bankroll) / bankroll * 100),
            equity=equity, dates=dates, wins=wins,
        )

    print(f"data: {len(d)} markets from {a.data}")
    action = "BUY YES at YES price" if a.side == "yes" else "BUY NO (fade favorites) at (1-YES price)"
    print(f"strategy: {action} where YES price in [{a.min_price}, {a.max_price})")
    print(f"\ntopic distribution in this band:")
    band = d[(d.p_t >= a.min_price) & (d.p_t < a.max_price)]
    print(band.groupby("topic").agg(n=("y", "size"), yes=("y", "mean"),
                                    mean_p=("p_t", "mean")).sort_values("n", ascending=False).round(3).to_string())

    print(f"\n=== Simulation: ${a.bet:.0f}/trade, ${a.bankroll:.0f} bankroll ===")
    print(f"    (fee-free topics: {sorted(FEE_FREE_TOPICS)}; others pay {FEE_MULT} taker multiplier)")
    scenarios = [
        ("ALL markets in band", d),
        ("Iran/Israel only",    d[d.topic == "iran_israel"]),
        ("Non-Iran only",       d[d.topic != "iran_israel"]),
        ("Fee-free topics",     d[d.topic.isin(FEE_FREE_TOPICS)]),
        ("Fee-paying topics",   d[~d.topic.isin(FEE_FREE_TOPICS)]),
    ]
    results = []
    for name, sub in scenarios:
        r = simulate(sub, a.min_price, a.max_price, a.bet, a.bankroll)
        if r: r["scenario"] = name; results.append(r)
    R = pd.DataFrame(results).set_index("scenario")
    cols = ["n", "hit_rate", "mean_yes_price", "total_pnl", "roi_pct",
            "sharpe_per_trade", "max_dd", "max_dd_pct", "longest_losing_streak"]
    with pd.option_context("display.width", 200, "display.max_colwidth", 30):
        print(R[cols].round(3).to_string())

    print(f"\n=== By sub-band (all markets, ${a.bet:.0f}/trade) ===")
    subbands = [(0.02, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.25)]
    subs = []
    for lo, hi in subbands:
        r = simulate(d, lo, hi, a.bet, a.bankroll)
        if r: r["band"] = f"[{lo:.2f},{hi:.2f})"; subs.append(r)
    S = pd.DataFrame(subs).set_index("band")
    print(S[cols].round(3).to_string())

    print(f"\n=== Sanity: what does the edge look like per topic ({a.min_price}-{a.max_price} band, side={a.side}) ===")
    for tp in sorted(d.topic.unique()):
        r = simulate(d[d.topic == tp], a.min_price, a.max_price, a.bet, a.bankroll)
        if r and r["n"] > 0:
            print(f"  {tp:15s} n={r['n']:3d}  hit={r['hit_rate']:.3f}  mean_yes_p={r['mean_yes_price']:.3f}  "
                  f"total_pnl=${r['total_pnl']:>8.0f}  sharpe={r['sharpe_per_trade']:+.3f}  "
                  f"longest_loss_streak={r['longest_losing_streak']}")

    print("\ninterpretation notes:")
    print("  - Sharpe per trade > 0.15 with n>=30 is roughly interview-defensible")
    print("  - max_dd_pct > 50% at $2k bankroll = position sizing too aggressive OR strategy unsound")
    print("  - longest_losing_streak > 15 = you will quit before edge shows up in real time")

    if a.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except ImportError:
            print("\n[--plot skipped] pip install matplotlib in your venv first"); return

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8),
                                        gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
        colors = {"ALL markets in band": "#1f77b4", "Iran/Israel only": "#d62728",
                  "Non-Iran only": "#2ca02c", "Fee-free topics": "#9467bd",
                  "Fee-paying topics": "#ff7f0e"}
        for name, sub in scenarios:
            r = simulate(sub, a.min_price, a.max_price, a.bet, a.bankroll)
            if not r or r["n"] < 3:
                continue
            xs = pd.to_datetime(r["dates"])
            eq = r["equity"][1:]
            ax1.plot(xs, eq, label=f"{name} (n={r['n']}, Sharpe={r['sharpe_per_trade']:+.3f})",
                     color=colors.get(name), linewidth=1.6)
        ax1.axhline(a.bankroll, color="gray", linestyle="--", linewidth=0.8, label="starting bankroll")
        ax1.set_ylabel("Bankroll ($)")
        ax1.set_title(f"Equity curves — {a.side.upper()} at YES price [{a.min_price},{a.max_price})  "
                       f"— ${a.bet:.0f}/trade, ${a.bankroll:.0f} bankroll")
        ax1.legend(loc="upper left", fontsize=9)
        ax1.grid(alpha=0.3)

        # Drawdown panel for the "ALL markets" scenario
        r_all = simulate(d, a.min_price, a.max_price, a.bet, a.bankroll)
        if r_all:
            xs = pd.to_datetime(r_all["dates"])
            eq = pd.Series(r_all["equity"][1:])
            peak = eq.cummax()
            dd = (eq - peak) / peak * 100
            ax2.fill_between(xs, dd, 0, color="#d62728", alpha=0.35)
            ax2.plot(xs, dd, color="#d62728", linewidth=1)
            ax2.set_ylabel("Drawdown (%)")
            ax2.set_xlabel("Trade close date")
            ax2.grid(alpha=0.3)
            ax2.axhline(0, color="black", linewidth=0.5)
        ax2.xaxis.set_major_locator(mdates.MonthLocator())
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))

        plt.tight_layout()
        out_png = a.out or f"poly_backtest_{a.side}_{a.min_price}-{a.max_price}.png"
        plt.savefig(out_png, dpi=110)
        print(f"\nchart saved -> {out_png} (open it in Finder / Preview)")

def cmd_poly_consistency(a):
    """Falsification test for 'consistent weekly returns' hypothesis.
    Pulls Polymarket leaderboards at DAY/WEEK/MONTH/ALL windows and cross-references
    to identify wallets with steady multi-window profitability."""
    import pandas as pd
    DATA = "https://data-api.polymarket.com"

    def fetch_lb(window):
        rows, offset = [], 0
        while offset <= 1000:
            try:
                r = requests.get(DATA + "/v1/leaderboard",
                    params=dict(category="OVERALL", timePeriod=window,
                                orderBy="PNL", limit=50, offset=offset),
                    headers=UA, timeout=25)
            except requests.RequestException:
                break
            if not r.ok:
                break
            batch = r.json() or []
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch); offset += 50
            time.sleep(SLEEP)
            if len(batch) < 50:
                break
        return rows

    dfs = {}
    for w in ["DAY", "WEEK", "MONTH", "ALL"]:
        print(f"pulling {w} leaderboard...")
        rows = fetch_lb(w)
        recs = []
        for r in rows:
            wallet = (r.get("proxyWallet") or "").lower()
            if not wallet.startswith("0x"):
                continue
            recs.append({"wallet": wallet, "name": r.get("userName") or "",
                         f"{w}_pnl": float(r.get("pnl") or 0),
                         f"{w}_vol": float(r.get("vol") or 0),
                         f"{w}_rank": int(r.get("rank") or 0) if r.get("rank") else None})
        dfs[w] = pd.DataFrame(recs)
        print(f"  got {len(dfs[w])} entries")

    if dfs["ALL"].empty:
        print("no ALL-time leaderboard data — abort"); return
    base = dfs["ALL"][["wallet", "name", "ALL_pnl", "ALL_vol", "ALL_rank"]]
    for w in ["MONTH", "WEEK", "DAY"]:
        if not dfs[w].empty:
            base = base.merge(dfs[w][["wallet", f"{w}_pnl", f"{w}_vol", f"{w}_rank"]],
                              on="wallet", how="left")
    base.to_csv(a.out, index=False)
    print(f"\nsaved {len(base)} wallets -> {a.out}")

    all_four = base.dropna(subset=[c for c in ["DAY_pnl", "WEEK_pnl", "MONTH_pnl", "ALL_pnl"]
                                    if c in base.columns])
    print(f"\n{len(all_four)}/{len(base)} wallets present in ALL FOUR windows")
    print("   (i.e., top-500 by PnL over every timeframe simultaneously — the 'consistently on top' set)")

    print(f"\n=== FALSIFICATION TEST for '$250/week on $2k bankroll' target ===")
    print(f"That's $250/week edge on ~$2k of weekly turnover = 12.5% weekly ROI on notional.")
    print(f"If sustained, someone doing this for 6 months hits ~$130k, easily leaderboard-visible.\n")

    # Metric 1: weekly PnL vs weekly volume — proxies edge per dollar traded
    live = base.dropna(subset=["WEEK_pnl", "WEEK_vol"])
    live = live[live.WEEK_vol > 100].copy()                # skip near-zero-volume wallets
    live["week_roi_pct"] = live.WEEK_pnl / live.WEEK_vol * 100

    for thresh_pct, thresh_label in [(2, "2%"), (5, "5%"), (10, "10%"), (12.5, "12.5% (your target)")]:
        n_all = len(live[live.week_roi_pct > thresh_pct])
        small = live[(live.WEEK_vol < 10000) & (live.week_roi_pct > thresh_pct)]
        print(f"  WEEK_pnl / WEEK_vol > {thresh_label}: {n_all} wallets total, "
              f"{len(small)} with weekly volume < $10k")

    # Metric 2: sustained profitability — positive in every window
    if all(c in base.columns for c in ["DAY_pnl", "WEEK_pnl", "MONTH_pnl"]):
        multiwin = base.dropna(subset=["DAY_pnl", "WEEK_pnl", "MONTH_pnl"])
        pos_all = multiwin[(multiwin.DAY_pnl > 0) & (multiwin.WEEK_pnl > 0)
                           & (multiwin.MONTH_pnl > 0) & (multiwin.ALL_pnl > 0)]
        print(f"\nPositive PnL in DAY & WEEK & MONTH & ALL simultaneously: {len(pos_all)}/{len(multiwin)}")
        if len(pos_all) > 0:
            # Estimate weekly-run-rate consistency: monthly / 4.33 vs weekly
            pos_all = pos_all.copy()
            pos_all["monthly_avg_week"] = pos_all.MONTH_pnl / 4.33
            pos_all["consistency_ratio"] = pos_all.WEEK_pnl / pos_all.monthly_avg_week.replace(0, float("nan"))
            steady = pos_all[pos_all.consistency_ratio.between(0.3, 3.0)]
            print(f"  of which {len(steady)} have week_pnl within 30%-300% of their monthly average "
                  f"(rough 'steady' filter, drops big-recent-week outliers)")

            print(f"\ntop 15 'steady multi-window profitable' wallets:")
            show = steady.sort_values("MONTH_pnl", ascending=False).head(15)
            cols = ["wallet", "name", "DAY_pnl", "WEEK_pnl", "WEEK_vol",
                    "MONTH_pnl", "MONTH_vol", "ALL_pnl", "ALL_vol", "consistency_ratio"]
            with pd.option_context("display.width", 240, "display.max_colwidth", 25):
                print(show[cols].round(0).to_string(index=False))

    # Metric 3: small-account high-return specifically — the profile you're proposing
    print(f"\n=== SMALL-ACCOUNT + HIGH-RETURN PROFILE (weekly vol $500-$10k, weekly ROI > 10%) ===")
    target = live[(live.WEEK_vol >= 500) & (live.WEEK_vol <= 10000) & (live.week_roi_pct > 10)]
    if len(target) == 0:
        print("  ZERO wallets match this profile in the top-500 by all-time PnL.")
        print("  Interpretation: any small-account 10%+ weekly returner has EITHER (a) not yet")
        print("  compounded enough to reach top-500, OR (b) doesn't exist in that population.")
    else:
        print(f"  {len(target)} wallets match. Showing them (this is your falsification data):")
        cols = ["wallet", "name", "WEEK_pnl", "WEEK_vol", "week_roi_pct",
                "MONTH_pnl", "MONTH_vol", "ALL_pnl"]
        with pd.option_context("display.width", 220, "display.max_colwidth", 25):
            print(target.sort_values("week_roi_pct", ascending=False).head(20)[cols].round(1).to_string(index=False))

def cmd_poly_follow(a):
    """What are the smart wallets currently long? Aggregate all positions across the
    smart-wallet set, group by (market, side), rank by consensus. The direct
    copy-trade signal — no candidate pre-filter, we see everything they hold."""
    import pandas as pd
    DATA = "https://data-api.polymarket.com"

    try:
        smart = pd.read_csv(a.smart_file)
    except FileNotFoundError:
        print(f"missing {a.smart_file} — run poly-smartmoney or poly-consistency first"); return
    wallets = [w.lower() for w in smart["wallet"].tolist()
               if isinstance(w, str) and w.startswith("0x")]
    print(f"loaded {len(wallets)} smart wallets from {a.smart_file}")

    print(f"fetching current positions for each wallet (~{len(wallets)*SLEEP:.0f}s)...")
    all_positions, ok_count = [], 0
    for i, w in enumerate(wallets):
        try:
            r = requests.get(DATA + "/positions", params={"user": w, "limit": 500},
                             headers=UA, timeout=20)
            if r.ok:
                positions = r.json() or []
                if isinstance(positions, list):
                    for p in positions:
                        p["_wallet"] = w
                        all_positions.append(p)
                    ok_count += 1
        except (requests.RequestException, json.JSONDecodeError):
            pass
        time.sleep(SLEEP)
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(wallets)} wallets ({ok_count} responsive), "
                  f"positions collected: {len(all_positions)}")
    print(f"\ncollected {len(all_positions)} position records from {ok_count} responsive wallets")
    if not all_positions:
        print("no positions to analyze"); return

    df = pd.DataFrame(all_positions)
    print(f"available fields on positions: {sorted(df.columns.tolist())}")

    # Coerce numerics
    for col in ["size", "avgPrice", "curPrice", "initialValue", "currentValue", "cashPnl", "percentPnl"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "size" in df.columns:
        df = df[df["size"] > 0]
    if "redeemable" in df.columns:
        df = df[df["redeemable"] != True]
    print(f"{len(df)} open, non-redeemable positions after filters")

    # Group by asset (token id) — uniquely identifies each side of each market
    if "asset" not in df.columns:
        print("no 'asset' field in position response — schema drifted"); return

    agg = df.groupby("asset").agg(
        n_wallets=("_wallet", "nunique"),
        total_size=("size", "sum"),
        avg_entry=("avgPrice", "mean") if "avgPrice" in df.columns else ("size", "count"),
        cur_price=("curPrice", "mean") if "curPrice" in df.columns else ("size", "count"),
        total_value=("currentValue", "sum") if "currentValue" in df.columns else ("size", "sum"),
        outcome=("outcome", "first") if "outcome" in df.columns else ("_wallet", "first"),
        title=("title", "first") if "title" in df.columns else ("_wallet", "first"),
    ).reset_index()
    agg = agg.sort_values(["n_wallets", "total_value"], ascending=[False, False])
    agg.to_csv(a.out, index=False)
    print(f"\nsaved {len(agg)} distinct (market, side) records -> {a.out}")

    print(f"\n=== TOP MARKETS BY SMART-WALLET CONSENSUS (>= {a.min_wallets} wallets same side) ===")
    top = agg[agg.n_wallets >= a.min_wallets].head(a.top)
    if top.empty:
        print(f"  no positions with {a.min_wallets}+ wallets agreeing. Showing top 20 by wallet count:")
        top = agg.head(20)
    show_cols = [c for c in ["n_wallets", "outcome", "cur_price", "avg_entry",
                              "total_value", "total_size", "title"] if c in top.columns]
    with pd.option_context("display.max_colwidth", 60, "display.width", 260):
        print(top[show_cols].to_string(index=False))

    # Bonus: which wallets are the most active (help you spot the operator you'd copy)
    print(f"\n=== MOST ACTIVE SMART WALLETS (# open positions) ===")
    if "_wallet" in df.columns:
        active = df.groupby("_wallet").agg(
            n_positions=("asset", "nunique"),
            total_value=("currentValue", "sum") if "currentValue" in df.columns else ("size", "sum"),
        ).sort_values("n_positions", ascending=False).head(15)
        active = active.merge(smart[["wallet", "name"]].rename(columns={"wallet": "_wallet"}),
                               left_index=True, right_on="_wallet", how="left")
        print(active[["_wallet", "name", "n_positions", "total_value"]].to_string(index=False))

def cmd_poly_timing(a):
    """Analyze holding periods per wallet — is anyone actually scalping?
    Pulls /trades, matches buys to sells FIFO, computes median hold time, classifies:
      SCALP     <1h median hold (market maker / arb)
      DAY       1-24h (news reaction)
      SWING     1-7d (thesis-driven)
      POSITION  >7d (deadline/conviction)"""
    import pandas as pd
    DATA = "https://data-api.polymarket.com"

    try:
        smart = pd.read_csv(a.smart_file)
    except FileNotFoundError:
        print(f"missing {a.smart_file}"); return

    if a.wallets:
        raw = [x.strip() for x in a.wallets.split(",")]
        wallets = []
        for x in raw:
            if x.lower().startswith("0x"):
                wallets.append(x.lower())
            else:                                         # treat as name; look up address
                m = smart[smart["name"].str.lower() == x.lower()]
                if not m.empty:
                    wallets.append(m.iloc[0]["wallet"].lower())
                else:
                    print(f"  [warn] no wallet found for name '{x}'")
    else:
        wallets = smart.sort_values("all_pnl", ascending=False).head(a.top_n)["wallet"].str.lower().tolist()
    name_map = dict(zip(smart["wallet"].str.lower(), smart["name"]))
    print(f"analyzing {len(wallets)} wallets")

    def fetch_trades(wallet):
        rows, offset = [], 0
        while offset < a.max_trades:
            try:
                r = requests.get(DATA + "/trades",
                    params=dict(user=wallet, limit=500, offset=offset),
                    headers=UA, timeout=25)
            except requests.RequestException:
                break
            if not r.ok:
                if r.status_code == 429:
                    time.sleep(3); continue
                break
            batch = r.json() or []
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch); offset += 500
            time.sleep(SLEEP)
            if len(batch) < 500:
                break
        return rows

    def classify_hold(med_hrs):
        if med_hrs is None: return "OPEN_ONLY"
        if med_hrs < 1: return "SCALP"
        if med_hrs < 24: return "DAY"
        if med_hrs < 168: return "SWING"
        return "POSITION"

    all_results = []
    for i, w in enumerate(wallets):
        nm = name_map.get(w, "?")
        print(f"\n[{i+1}/{len(wallets)}] {nm} ({w[:10]}...)")
        trades = fetch_trades(w)
        print(f"  {len(trades)} trades fetched")
        if not trades:
            continue
        if i == 0:
            print(f"  sample: {json.dumps(trades[0], indent=2)[:400]}")

        df = pd.DataFrame(trades)
        for col in ["price", "size", "timestamp"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "timestamp" not in df.columns:
            print(f"  no timestamp field, skipping"); continue
        df["ts"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
        df = df.dropna(subset=["price", "size", "ts"])
        if df.empty:
            continue

        n_trades = len(df); first_ts, last_ts = df.ts.min(), df.ts.max()
        span_days = max((last_ts - first_ts).total_seconds() / 86400, 1)
        trades_per_day = n_trades / span_days

        # FIFO match buys → sells per asset
        holds = []
        if "asset" in df.columns and "side" in df.columns:
            df_sorted = df.sort_values("ts")
            positions = {}                              # asset -> [(ts, size, price)]
            for _, r in df_sorted.iterrows():
                asset = r.get("asset"); side = str(r.get("side") or "").upper()
                size = r["size"]
                if not asset or size <= 0:
                    continue
                if side in ("BUY", "MINT"):
                    positions.setdefault(asset, []).append((r["ts"], size, r["price"]))
                elif side in ("SELL", "REDEEM"):
                    remaining = size
                    while remaining > 0 and positions.get(asset):
                        buy_ts, buy_size, buy_price = positions[asset][0]
                        match = min(buy_size, remaining)
                        holds.append((r["ts"] - buy_ts).total_seconds())
                        remaining -= match; buy_size -= match
                        if buy_size <= 0:
                            positions[asset].pop(0)
                        else:
                            positions[asset][0] = (buy_ts, buy_size, buy_price)

        med_hold_hrs = p10_hold_hrs = p90_hold_hrs = None
        pct_under_1min = pct_under_1hr = None
        if holds:
            hs = pd.Series(holds)
            med_hold_hrs = hs.median() / 3600
            p10_hold_hrs = hs.quantile(0.10) / 3600
            p90_hold_hrs = hs.quantile(0.90) / 3600
            pct_under_1min = (hs < 60).mean() * 100
            pct_under_1hr = (hs < 3600).mean() * 100

        avg_notional = (df["size"] * df["price"]).mean() if "size" in df.columns and "price" in df.columns else None
        n_markets = df["asset"].nunique() if "asset" in df.columns else None

        all_results.append(dict(
            name=nm[:15], wallet=w[:10] + "..",
            n_trades=n_trades, span_d=round(span_days, 1),
            trades_per_day=round(trades_per_day, 2),
            n_markets=n_markets,
            avg_notional=round(avg_notional, 0) if avg_notional else None,
            n_closes=len(holds),
            med_hold_hrs=round(med_hold_hrs, 2) if med_hold_hrs is not None else None,
            p10_hold_hrs=round(p10_hold_hrs, 3) if p10_hold_hrs is not None else None,
            p90_hold_hrs=round(p90_hold_hrs, 1) if p90_hold_hrs is not None else None,
            pct_under_1min=round(pct_under_1min, 1) if pct_under_1min is not None else None,
            pct_under_1hr=round(pct_under_1hr, 1) if pct_under_1hr is not None else None,
            archetype=classify_hold(med_hold_hrs),
        ))

    if not all_results:
        print("no wallets produced usable trade data"); return
    R = pd.DataFrame(all_results).sort_values("trades_per_day", ascending=False)
    R.to_csv(a.out, index=False)
    print(f"\n=== TRADE TIMING ANALYSIS (sorted by trades/day) ===\nsaved -> {a.out}")
    with pd.option_context("display.width", 260, "display.max_colwidth", 18):
        print(R.to_string(index=False))

    print("\narchetype legend:")
    print("  SCALP    <1h median hold — market maker / arb")
    print("  DAY      1-24h — news trader")
    print("  SWING    1-7d — thesis-driven")
    print("  POSITION >7d — conviction/deadline")
    print("  OPEN_ONLY no closes detected — pure accumulation, still holding, or MERGE/REDEEM instead of SELL")

def cmd_poly_recent(a):
    """Fresh-buy monitor: what have your target day-trader wallets bought in the last N hours?
    Includes current mid price so you can see whether the entry is still gettable."""
    import pandas as pd
    DATA = "https://data-api.polymarket.com"
    CLOB = "https://clob.polymarket.com"

    try:
        smart = pd.read_csv(a.smart_file)
    except FileNotFoundError:
        print(f"missing {a.smart_file}"); return

    if a.wallets:
        raw = [x.strip() for x in a.wallets.split(",")]
        wallets = []
        for x in raw:
            if x.lower().startswith("0x"):
                wallets.append(x.lower())
            else:
                m = smart[smart["name"].str.lower() == x.lower()]
                if not m.empty:
                    wallets.append(m.iloc[0]["wallet"].lower())
                else:
                    print(f"  [warn] no wallet found for name '{x}'")
    else:
        # Default: small-account DAY-archetype traders from poly_timing.csv
        try:
            timing = pd.read_csv("poly_timing.csv")
            good = timing[
                (timing.avg_notional < 10000) &
                (timing.archetype.isin(["DAY", "SWING"])) &
                (timing.n_trades >= 100)
            ].sort_values("trades_per_day", ascending=False)
            wallets = []
            for _, row in good.head(a.top_n).iterrows():
                nm = str(row["name"])
                m = smart[smart["name"].str[:15] == nm]
                if not m.empty:
                    wallets.append(m.iloc[0]["wallet"].lower())
        except FileNotFoundError:
            print("no --wallets and no poly_timing.csv — pass --wallets explicitly"); return
        if not wallets:
            print("no wallets matched default filter; pass --wallets explicitly"); return

    name_map = dict(zip(smart["wallet"].str.lower(), smart["name"]))
    print(f"scanning {len(wallets)} wallets for buys in last {a.hours}h:")
    for w in wallets:
        print(f"  - {name_map.get(w, w[:10])}")

    cutoff_ts = int(time.time()) - a.hours * 3600
    recent_buys = []
    for w in wallets:
        nm = name_map.get(w, "?")
        try:
            r = requests.get(DATA + "/trades",
                params=dict(user=w, limit=500, offset=0),
                headers=UA, timeout=25)
        except requests.RequestException:
            continue
        if not r.ok:
            if r.status_code == 429:
                time.sleep(3)
            continue
        trades = r.json() or []
        if not isinstance(trades, list):
            continue
        count = 0
        for t in trades:
            ts = int(t.get("timestamp") or 0)
            if ts < cutoff_ts:
                continue
            side = str(t.get("side") or "").upper()
            if side not in ("BUY", "MINT"):
                continue
            price = float(t.get("price") or 0)
            size = float(t.get("size") or 0)
            recent_buys.append({
                "wallet_name": nm,
                "hours_ago": round((int(time.time()) - ts) / 3600, 1),
                "outcome": t.get("outcome") or "?",
                "price": round(price, 3),
                "size": round(size, 1),
                "notional": round(price * size, 0),
                "asset": str(t.get("asset") or ""),
                "market_id": str(t.get("conditionId") or ""),
                "title": (t.get("title") or "")[:75],
            })
            count += 1
        if count:
            print(f"  {nm}: {count} buys in last {a.hours}h")
        time.sleep(SLEEP)

    if not recent_buys:
        print(f"\nno buys in last {a.hours}h across {len(wallets)} target wallets."); return
    df = pd.DataFrame(recent_buys).sort_values("hours_ago")
    print(f"\n{len(recent_buys)} buys collected total")

    unique_assets = [a for a in df["asset"].dropna().unique() if a]
    print(f"fetching current mid prices for {len(unique_assets)} unique tokens...")
    prices = {}
    for asset in unique_assets:
        try:
            r = requests.get(CLOB + "/midpoint",
                params=dict(token_id=asset), headers=UA, timeout=15)
            if r.ok:
                data = r.json()
                if isinstance(data, dict) and "mid" in data:
                    prices[asset] = float(data["mid"])
        except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError):
            pass
        time.sleep(SLEEP)

    df["cur_mid"] = df["asset"].map(prices).round(3)
    df["move"] = (df["cur_mid"] - df["price"]).round(3)
    df["still_gettable"] = df["cur_mid"] <= (df["price"] * 1.15)

    print(f"\n=== RECENT BUYS ({a.hours}h, sorted by recency) ===")
    cols = ["hours_ago", "wallet_name", "outcome", "price", "cur_mid", "move",
            "notional", "still_gettable", "title"]
    with pd.option_context("display.width", 260, "display.max_colwidth", 55):
        print(df[cols].head(50).to_string(index=False))
    df.to_csv(a.out, index=False)
    print(f"\nsaved -> {a.out}")

    consensus = df.groupby(["market_id", "outcome"]).agg(
        n_wallets=("wallet_name", "nunique"),
        wallets=("wallet_name", lambda x: ",".join(sorted(set(x)))),
        avg_buy_price=("price", "mean"),
        cur_mid=("cur_mid", "first"),
        total_notional=("notional", "sum"),
        title=("title", "first"),
    ).reset_index()
    consensus = consensus[consensus.n_wallets >= 2].sort_values(
        ["n_wallets", "total_notional"], ascending=[False, False])
    if len(consensus) > 0:
        print(f"\n=== FRESH CONSENSUS (2+ target wallets buying same side within {a.hours}h) ===")
        with pd.option_context("display.width", 260, "display.max_colwidth", 55):
            print(consensus.round(3).to_string(index=False))
    else:
        print(f"\n(no fresh consensus buys — try wider --hours or more --top-n wallets)")

def cmd_poly_copy_backtest(a):
    """Historical backtest of copy-trading: for each realized round-trip by target
    wallets, simulate copying at their entry (+ slippage) and exiting at their exit
    (- slippage), with per-topic Polymarket fees. Reports Sharpe, drawdown, per-wallet PnL."""
    import pandas as pd
    DATA = "https://data-api.polymarket.com"

    try:
        smart = pd.read_csv(a.smart_file)
    except FileNotFoundError:
        print(f"missing {a.smart_file}"); return

    if a.wallets:
        raw = [x.strip() for x in a.wallets.split(",")]
        wallets = []
        for x in raw:
            if x.lower().startswith("0x"):
                wallets.append(x.lower())
            else:
                m = smart[smart["name"].str.lower() == x.lower()]
                if not m.empty:
                    wallets.append(m.iloc[0]["wallet"].lower())
    else:
        try:
            timing = pd.read_csv("poly_timing.csv")
            good = timing[(timing.avg_notional < 10000)
                          & (timing.archetype.isin(["DAY", "SWING"]))
                          & (timing.n_trades >= 100)
                          ].sort_values("trades_per_day", ascending=False)
            wallets = []
            for _, row in good.head(a.top_n).iterrows():
                nm = str(row["name"])
                m = smart[smart["name"].str[:15] == nm]
                if not m.empty:
                    wallets.append(m.iloc[0]["wallet"].lower())
        except FileNotFoundError:
            print("no poly_timing.csv — pass --wallets explicitly"); return
    if not wallets:
        print("no wallets selected"); return
    name_map = dict(zip(smart["wallet"].str.lower(), smart["name"]))
    print(f"backtesting copy of {len(wallets)} wallets")

    def topic(q):
        q = (q or "").lower()
        if any(k in q for k in ("iran","khamenei","israel","lebanon","hormuz","kharg")):
            return "iran_israel"
        if any(k in q for k in ("cartel","venezuela","russia","putin","china","qatar","north korea","honduras")):
            return "geopol_other"
        if "shutdown" in q or "government" in q: return "US_policy"
        if "fed" in q or "interest rate" in q or "powell" in q or "cpi" in q: return "fed"
        if any(k in q for k in ("bitcoin","btc","ether","eth","crypto")): return "crypto"
        if any(k in q for k in ("mayoral","election","president","senate","mamdani")): return "election"
        if any(k in q for k in ("trump","biden","harris","vance")): return "trump_admin"
        if any(k in q for k in ("world cup","champions","masters","boxing","verstappen","nba","nfl")): return "sports"
        return "other"
    FEE_FREE = {"iran_israel", "geopol_other", "US_policy"}
    FEE_MULT = 0.05

    def fetch_trades(w):
        rows, offset = [], 0
        while offset < a.max_trades:
            try:
                r = requests.get(DATA + "/trades",
                    params=dict(user=w, limit=500, offset=offset), headers=UA, timeout=25)
            except requests.RequestException:
                break
            if not r.ok:
                if r.status_code == 429:
                    time.sleep(3); continue
                break
            batch = r.json() or []
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch); offset += 500
            time.sleep(SLEEP)
            if len(batch) < 500:
                break
        return rows

    all_rt = []
    for i, w in enumerate(wallets):
        nm = name_map.get(w, "?")
        print(f"[{i+1}/{len(wallets)}] {nm}: fetching trades...")
        trades = fetch_trades(w)
        if not trades:
            continue
        df = pd.DataFrame(trades)
        for col in ["price", "size", "timestamp"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "timestamp" not in df.columns:
            continue
        df["ts"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
        df = df.dropna(subset=["price", "size", "ts"])
        if df.empty:
            continue
        df_sorted = df.sort_values("ts")
        positions = {}                              # asset -> [(ts, size, price, title)]
        realized = 0
        for _, r in df_sorted.iterrows():
            asset = r.get("asset"); side = str(r.get("side") or "").upper()
            size = r["size"]
            if not asset or size <= 0:
                continue
            if side in ("BUY", "MINT"):
                positions.setdefault(asset, []).append(
                    (r["ts"], size, r["price"], (r.get("title") or "")[:70]))
            elif side in ("SELL", "REDEEM"):
                remaining = size
                while remaining > 0 and positions.get(asset):
                    buy_ts, buy_size, buy_price, title = positions[asset][0]
                    match = min(buy_size, remaining)
                    all_rt.append({
                        "wallet_name": nm, "entry_ts": buy_ts, "exit_ts": r["ts"],
                        "entry_price": buy_price, "exit_price": r["price"],
                        "size": match,
                        "hold_hrs": (r["ts"] - buy_ts).total_seconds() / 3600,
                        "title": title or (r.get("title") or "")[:70],
                    })
                    remaining -= match; buy_size -= match
                    realized += 1
                    if buy_size <= 0:
                        positions[asset].pop(0)
                    else:
                        positions[asset][0] = (buy_ts, buy_size, buy_price, title)
        print(f"  {len(trades)} trades -> {realized} realized round-trips")

    if not all_rt:
        print("no realized round-trips found"); return
    rt = pd.DataFrame(all_rt).sort_values("entry_ts").reset_index(drop=True)
    rt["topic"] = rt["title"].map(topic)
    if a.min_hold_hrs > 0:
        rt = rt[rt.hold_hrs >= a.min_hold_hrs]
    print(f"\n{len(rt)} round-trips, {rt['wallet_name'].nunique()} wallets, "
          f"range {rt.entry_ts.min().date()} → {rt.exit_ts.max().date()}")

    slip = a.slippage
    running = a.bankroll; peak = running; dd = 0
    equity = [running]; dates = []; pnls = []
    for _, r in rt.iterrows():
        fee_mult = 0.0 if r.topic in FEE_FREE else FEE_MULT
        entry = min(max(r.entry_price + slip, 0.01), 0.99)
        exit_ = min(max(r.exit_price - slip, 0.0), 1.0)
        entry_fee = fee_mult * entry * (1 - entry)
        exit_fee  = fee_mult * exit_ * (1 - exit_) if 0 < exit_ < 1 else 0.0
        contracts = a.bet / (entry + entry_fee)
        cost = contracts * (entry + entry_fee)
        payout = contracts * (exit_ - exit_fee)
        pnl = payout - cost
        pnls.append(pnl); running += pnl
        peak = max(peak, running); dd = min(dd, running - peak)
        equity.append(running); dates.append(r.exit_ts)

    n = len(pnls); s = pd.Series(pnls); wins = sum(1 for p in pnls if p > 0)
    print(f"\n=== COPY-TRADE BACKTEST ===")
    print(f"  ${a.bet}/trade, ${a.bankroll} bankroll, {slip*100:.0f}c one-way slippage, "
          f"fee-free topics: {sorted(FEE_FREE)}")
    print(f"  trades:            {n}")
    print(f"  hit rate:          {wins/n:.1%}")
    print(f"  total PnL:         ${s.sum():,.0f}  (ROI {s.sum()/a.bankroll*100:+.0f}%)")
    print(f"  mean PnL/trade:    ${s.mean():+.2f}")
    print(f"  Sharpe/trade:      {s.mean()/s.std():+.3f}" if s.std() > 0 else "  Sharpe: N/A")
    print(f"  max drawdown:      ${dd:.0f} ({dd/a.bankroll*100:.1f}%)")
    print(f"  final bankroll:    ${running:,.0f}")

    print(f"\n=== BY WALLET ===")
    rows = []
    for wname, sub in rt.groupby("wallet_name"):
        sub_pnls = []
        for _, r in sub.iterrows():
            fee_mult = 0.0 if r.topic in FEE_FREE else FEE_MULT
            entry = min(max(r.entry_price + slip, 0.01), 0.99)
            exit_ = min(max(r.exit_price - slip, 0.0), 1.0)
            ef = fee_mult * entry * (1 - entry)
            xf = fee_mult * exit_ * (1 - exit_) if 0 < exit_ < 1 else 0.0
            c = a.bet / (entry + ef)
            sub_pnls.append(c * (exit_ - xf) - c * (entry + ef))
        ss = pd.Series(sub_pnls); w = sum(1 for p in sub_pnls if p > 0)
        rows.append({"wallet": wname, "n": len(sub), "hit": w/len(sub),
                     "total_pnl": ss.sum(),
                     "mean_pnl": ss.mean(),
                     "sharpe": ss.mean()/ss.std() if ss.std() > 0 else 0})
    R = pd.DataFrame(rows).sort_values("total_pnl", ascending=False)
    with pd.option_context("display.width", 200):
        print(R.round({"hit": 3, "total_pnl": 0, "mean_pnl": 2, "sharpe": 3}).to_string(index=False))

    rt.to_csv(a.out, index=False)
    print(f"\nround-trips -> {a.out}")

    if a.plot:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates
        except ImportError:
            print("pip install matplotlib for --plot"); return
        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 7),
            gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
        xs = pd.to_datetime(dates); eq = equity[1:]
        ax1.plot(xs, eq, linewidth=1.5, color="#2ca02c", label=f"copy-trade equity (n={n})")
        ax1.axhline(a.bankroll, color="gray", linestyle="--", linewidth=0.8, label="starting bankroll")
        ax1.set_ylabel("Bankroll ($)")
        ax1.set_title(f"Copy-trade backtest: ${a.bet}/trade, {rt['wallet_name'].nunique()} wallets, "
                       f"{slip*100:.0f}c slip")
        ax1.legend(); ax1.grid(alpha=0.3)
        eq_s = pd.Series(eq); peak_s = eq_s.cummax()
        dd_pct = (eq_s - peak_s) / peak_s * 100
        ax2.fill_between(xs, dd_pct, 0, color="#d62728", alpha=0.35)
        ax2.plot(xs, dd_pct, color="#d62728", linewidth=1)
        ax2.set_ylabel("Drawdown (%)"); ax2.grid(alpha=0.3)
        ax2.xaxis.set_major_locator(mdates.MonthLocator())
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
        plt.tight_layout()
        plt.savefig(a.plot_out, dpi=110)
        print(f"chart -> {a.plot_out}")

    print("\nHONEST CAVEATS:")
    print("  1. Assumes you copied EVERY realized trade. Cron every 4-6h misses fast round-trips.")
    print("  2. Slippage of 1c is optimistic. Real delayed entries can be 2-5c worse than shown.")
    print("  3. STILL-OPEN positions excluded — survivorship bias unclear direction.")
    print("  4. Fixed bet size, no capital constraint. Real $2k bankroll with 5 open positions caps you.")
    print("  5. Bet size scaled up (--bet 500 etc) doesn't magically produce equivalent live results — orderbook depth caps you.")

def cmd_poly_scalper_scan(a):
    """Scalper discovery. Samples active markets to find wallets doing lots of trades
    across many markets, then verifies with full history + holding-period stats.
    Finds high-frequency traders invisible to leaderboard-based searches."""
    import pandas as pd
    DATA = "https://data-api.polymarket.com"
    GAMMA = "https://gamma-api.polymarket.com"

    print(f"fetching top {a.n_markets} active markets by volume >= ${a.min_market_vol:,.0f}...")
    try:
        r = requests.get(GAMMA + "/markets",
            params={"closed": "false", "active": "true", "limit": a.n_markets,
                    "volume_num_min": a.min_market_vol,
                    "order": "volumeNum", "ascending": "false"},
            headers=UA, timeout=30)
    except requests.RequestException as e:
        print(f"gamma error: {e}"); return
    if not r.ok:
        print(f"gamma {r.status_code}: {r.text[:200]}"); return
    markets = r.json() or []
    print(f"got {len(markets)} markets")
    if not markets:
        return
    market_info = {}
    for m in markets:
        cid = m.get("conditionId")
        if cid:
            market_info[cid] = {
                "title": (m.get("question") or "")[:60],
                "volume": float(m.get("volumeNum") or 0),
            }

    print(f"\npulling recent trades from {len(market_info)} markets...")
    all_trades = []
    for i, (cid, info) in enumerate(market_info.items()):
        try:
            r = requests.get(DATA + "/trades",
                params={"market": cid, "limit": 500},
                headers=UA, timeout=25)
        except requests.RequestException:
            continue
        if not r.ok:
            if r.status_code == 429:
                time.sleep(3)
            continue
        trades = r.json() or []
        if isinstance(trades, list):
            for t in trades:
                t["_market_title"] = info["title"]
                all_trades.append(t)
        time.sleep(SLEEP)
        if (i + 1) % 5 == 0:
            print(f"  ...{i+1}/{len(market_info)} markets, {len(all_trades)} trades collected")

    print(f"\ncollected {len(all_trades)} trades")
    if not all_trades:
        print("no trades collected — /trades market filter may not work; try smaller --n-markets"); return

    df = pd.DataFrame(all_trades)
    for col in ["price", "size", "timestamp"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "timestamp" not in df.columns or "proxyWallet" not in df.columns:
        print(f"trade schema missing expected fields. Sample: {json.dumps(all_trades[0], indent=2)[:400]}"); return
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
    df = df.dropna(subset=["price", "size", "ts"])
    df["wallet"] = df["proxyWallet"].str.lower()
    df["notional"] = df["price"] * df["size"]

    span_hours = (df.ts.max() - df.ts.min()).total_seconds() / 3600
    print(f"trades span: {span_hours:.1f}h ({df.ts.min()} to {df.ts.max()})")

    agg = df.groupby("wallet").agg(
        n_trades=("ts", "count"),
        n_markets=("_market_title", "nunique"),
        total_notional=("notional", "sum"),
        first_ts=("ts", "min"),
        last_ts=("ts", "max"),
    ).reset_index()
    agg["activity_hours"] = (agg.last_ts - agg.first_ts).dt.total_seconds() / 3600
    agg["trades_per_hour"] = agg.n_trades / agg.activity_hours.clip(lower=1/60)

    candidates = agg[(agg.n_trades >= a.min_trades_in_sample)
                     & (agg.n_markets >= a.min_markets)].sort_values("n_trades", ascending=False)
    print(f"\n{len(candidates)} wallets meet discovery threshold "
          f"(>={a.min_trades_in_sample} trades, >={a.min_markets} markets)")
    if candidates.empty:
        print("no active wallets — try lower --min-trades-in-sample or larger --n-markets"); return

    print(f"\nTop 20 candidates by trade count in sample:")
    show_cols = ["wallet", "n_trades", "n_markets", "trades_per_hour", "total_notional"]
    with pd.option_context("display.width", 200):
        print(candidates.head(20)[show_cols].round(2).to_string(index=False))

    top_candidates = candidates.head(a.verify_n)["wallet"].tolist()
    print(f"\nverifying top {len(top_candidates)} candidates with full history ...")

    def fetch_trades(w, max_trades):
        rows, offset = [], 0
        while offset < max_trades:
            try:
                r = requests.get(DATA + "/trades",
                    params={"user": w, "limit": 500, "offset": offset},
                    headers=UA, timeout=25)
            except requests.RequestException:
                break
            if not r.ok:
                if r.status_code == 429:
                    time.sleep(3); continue
                break
            batch = r.json() or []
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch); offset += 500
            time.sleep(SLEEP)
            if len(batch) < 500:
                break
        return rows

    def classify(med_hrs):
        if med_hrs is None: return "OPEN_ONLY"
        if med_hrs < 1/60: return "ULTRA_SCALP"
        if med_hrs < 1/6:  return "SCALP"
        if med_hrs < 1:    return "FAST_INTRADAY"
        if med_hrs < 24:   return "DAY"
        if med_hrs < 168:  return "SWING"
        return "POSITION"

    verified = []
    for i, w in enumerate(top_candidates):
        print(f"[{i+1}/{len(top_candidates)}] {w[:12]}...")
        trades = fetch_trades(w, a.max_trades)
        if not trades:
            continue
        tdf = pd.DataFrame(trades)
        for col in ["price", "size", "timestamp"]:
            if col in tdf.columns:
                tdf[col] = pd.to_numeric(tdf[col], errors="coerce")
        if "timestamp" not in tdf.columns:
            continue
        tdf["ts"] = pd.to_datetime(tdf["timestamp"], unit="s", errors="coerce")
        tdf = tdf.dropna(subset=["price", "size", "ts"])
        if tdf.empty:
            continue
        n_trades = len(tdf)
        span_days = max((tdf.ts.max() - tdf.ts.min()).total_seconds() / 86400, 1)
        avg_notional = (tdf["size"] * tdf["price"]).mean()
        n_markets_all = tdf["asset"].nunique() if "asset" in tdf.columns else None

        # FIFO for holds + rough gross PnL
        tdf_sorted = tdf.sort_values("ts")
        positions, holds, realized = {}, [], []
        for _, r in tdf_sorted.iterrows():
            asset = r.get("asset"); side = str(r.get("side") or "").upper()
            size = r["size"]
            if not asset or size <= 0:
                continue
            if side in ("BUY", "MINT"):
                positions.setdefault(asset, []).append((r["ts"], size, r["price"]))
            elif side in ("SELL", "REDEEM"):
                remaining = size
                while remaining > 0 and positions.get(asset):
                    buy_ts, buy_size, buy_price = positions[asset][0]
                    match = min(buy_size, remaining)
                    holds.append((r["ts"] - buy_ts).total_seconds())
                    realized.append((buy_price, r["price"], match))
                    remaining -= match; buy_size -= match
                    if buy_size <= 0:
                        positions[asset].pop(0)
                    else:
                        positions[asset][0] = (buy_ts, buy_size, buy_price)

        med_hold_min = None; pct_1min = pct_5min = pct_1hr = None
        gross_pnl = wins = None
        if holds:
            hs = pd.Series(holds)
            med_hold_min = hs.median() / 60
            pct_1min = (hs < 60).mean() * 100
            pct_5min = (hs < 300).mean() * 100
            pct_1hr = (hs < 3600).mean() * 100
            gross_pnl = sum((ex - en) * sz for en, ex, sz in realized)
            wins = sum(1 for en, ex, _ in realized if ex > en)

        verified.append({
            "wallet_short": w[:12] + "..", "full_wallet": w, "n_trades": n_trades,
            "span_d": round(span_days, 1),
            "trades_per_day": round(n_trades / span_days, 2),
            "n_markets": n_markets_all,
            "avg_notional": round(avg_notional, 0) if avg_notional else None,
            "n_closes": len(holds),
            "med_hold_min": round(med_hold_min, 2) if med_hold_min else None,
            "pct_1min": round(pct_1min, 1) if pct_1min else None,
            "pct_5min": round(pct_5min, 1) if pct_5min else None,
            "pct_1hr": round(pct_1hr, 1) if pct_1hr else None,
            "gross_pnl_est": round(gross_pnl, 0) if gross_pnl else None,
            "hit_rate": round(wins / len(realized), 3) if realized else None,
            "archetype": classify(med_hold_min / 60 if med_hold_min else None),
        })

    if not verified:
        print("no verified data"); return
    R = pd.DataFrame(verified).sort_values("trades_per_day", ascending=False)
    R.to_csv(a.out, index=False)

    print(f"\n=== VERIFIED WALLETS (sorted by trades/day) ===")
    show_cols = ["wallet_short", "n_trades", "span_d", "trades_per_day", "n_markets",
                 "avg_notional", "med_hold_min", "pct_1min", "pct_5min", "pct_1hr",
                 "gross_pnl_est", "hit_rate", "archetype"]
    with pd.option_context("display.width", 300, "display.max_colwidth", 14):
        print(R[show_cols].to_string(index=False))
    print(f"\nsaved -> {a.out}")

    fast = R[R.archetype.isin(["ULTRA_SCALP", "SCALP", "FAST_INTRADAY"])]
    print(f"\n=== SCALPERS FOUND: {len(fast)} wallets with <1h median hold ===")
    if len(fast) > 0:
        cols = ["full_wallet", "trades_per_day", "med_hold_min", "pct_1min",
                "gross_pnl_est", "hit_rate", "archetype"]
        print(fast[cols].to_string(index=False))
        print(f"\nCross-reference gross_pnl_est: positive AND high trades/day = profitable scalper.")
        print(f"Then run: python kalshi_weather_edge.py poly-timing --wallets <addr> for full analysis.")
    else:
        print("This is a real empirical finding: sampling active flow across "
              f"{len(market_info)} markets found no scalper cohort. Interpretation:")
        print("  - Real scalping on Polymarket may not exist at retail-accessible scale")
        print("  - OR scalpers concentrate on markets not in our sample (very-new listings, illiquid tails)")
        print("  - OR MMs run pure quoting strategies where 'holding period' is meaningless")

def cmd_poly_scalper_analyze(a):
    """Deep analysis of scalper wallets: MM vs directional classification,
    per-wallet market concentration, and cross-wallet market overlap.
    Identifies which specific markets multiple scalpers focus on — those are
    the exploitable microstructure spots."""
    import pandas as pd
    DATA = "https://data-api.polymarket.com"

    if a.wallets:
        wallets = [w.strip().lower() for w in a.wallets.split(",") if w.strip().startswith("0x")]
    else:
        try:
            scalpers = pd.read_csv(a.scalper_file)
            scalpers = scalpers[scalpers.archetype.isin(["ULTRA_SCALP", "SCALP", "FAST_INTRADAY"])]
            wallets = scalpers.sort_values("trades_per_day", ascending=False).head(a.top_n)["full_wallet"].tolist()
        except FileNotFoundError:
            print(f"missing {a.scalper_file}, pass --wallets"); return
    if not wallets:
        print("no scalpers to analyze"); return
    print(f"analyzing {len(wallets)} scalper wallets")

    def fetch_trades(w, max_trades):
        rows, offset = [], 0
        while offset < max_trades:
            try:
                r = requests.get(DATA + "/trades",
                    params={"user": w, "limit": 500, "offset": offset},
                    headers=UA, timeout=25)
            except requests.RequestException:
                break
            if not r.ok:
                if r.status_code == 429:
                    time.sleep(3); continue
                break
            batch = r.json() or []
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch); offset += 500
            time.sleep(SLEEP)
            if len(batch) < 500:
                break
        return rows

    wallet_profiles = []
    market_activity = []
    for i, w in enumerate(wallets):
        print(f"[{i+1}/{len(wallets)}] {w[:12]}...")
        trades = fetch_trades(w, a.max_trades)
        if not trades:
            continue
        df = pd.DataFrame(trades)
        for col in ["price", "size", "timestamp"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["ts"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
        df = df.dropna(subset=["price", "size", "ts"])
        df["side_upper"] = df["side"].astype(str).str.upper() if "side" in df.columns else ""

        n_trades = len(df)
        buys = df[df.side_upper.isin(["BUY", "MINT"])]
        buy_ratio = len(buys) / n_trades if n_trades else 0.0

        # Median time buy→sell on same asset (inventory turnover)
        turnovers = []
        if "asset" in df.columns:
            positions = {}
            for _, r in df.sort_values("ts").iterrows():
                asset = r.get("asset"); side = r.side_upper
                if not asset:
                    continue
                if side in ("BUY", "MINT"):
                    positions.setdefault(asset, []).append(r["ts"])
                elif side in ("SELL", "REDEEM"):
                    if positions.get(asset):
                        turnovers.append((r["ts"] - positions[asset].pop(0)).total_seconds())
        median_turnover_s = pd.Series(turnovers).median() if turnovers else None
        median_turnover_min = median_turnover_s / 60 if median_turnover_s else None

        n_markets = df["conditionId"].nunique() if "conditionId" in df.columns else 0
        top5_share = 0.0
        top_markets_titles = []
        if "conditionId" in df.columns:
            mc = df.groupby("conditionId").size().sort_values(ascending=False)
            if n_trades:
                top5_share = mc.head(5).sum() / n_trades
            for cid in mc.head(a.top_markets_per_wallet).index:
                title = ""
                if "title" in df.columns:
                    sub = df[df.conditionId == cid]
                    if not sub.empty:
                        title = str(sub["title"].iloc[0] or "")[:60]
                top_markets_titles.append((cid, title, int(mc[cid])))
                market_activity.append({
                    "wallet": w[:12], "market_id": cid,
                    "trades": int(mc[cid]), "title": title,
                })

        # Classify
        if buy_ratio > 0.65:
            profile = "DIRECTIONAL_LONG"
        elif buy_ratio < 0.35:
            profile = "DIRECTIONAL_SHORT"
        elif median_turnover_s and median_turnover_s < 180:
            profile = "MARKET_MAKER"
        elif median_turnover_s and median_turnover_s < 900:
            profile = "SCALPER_MIXED"
        else:
            profile = "FAST_DAY"

        wallet_profiles.append({
            "wallet": w, "n_trades": n_trades,
            "buy_ratio": round(buy_ratio, 3), "n_markets": n_markets,
            "top5_share": round(top5_share, 3),
            "med_turnover_min": round(median_turnover_min, 2) if median_turnover_min else None,
            "profile": profile,
        })

        print(f"  {n_trades} trades  buy%={buy_ratio*100:.0f}  markets={n_markets}  "
              f"top5={top5_share*100:.0f}%  turnover~{median_turnover_min:.1f}min  -> {profile}"
              if median_turnover_min else
              f"  {n_trades} trades  buy%={buy_ratio*100:.0f}  markets={n_markets}  -> {profile}")
        for cid, title, cnt in top_markets_titles[:5]:
            print(f"    {cnt:4d}x  {title}")

    if not wallet_profiles:
        print("no profiles built"); return

    P = pd.DataFrame(wallet_profiles).sort_values("n_trades", ascending=False)
    P.to_csv(a.out, index=False)
    print(f"\n=== SCALPER PROFILES ===")
    with pd.option_context("display.width", 240, "display.max_colwidth", 20):
        print(P.to_string(index=False))
    print(f"\nsaved -> {a.out}")

    if market_activity:
        M = pd.DataFrame(market_activity)
        overlap = M.groupby(["market_id", "title"]).agg(
            n_scalpers=("wallet", "nunique"),
            wallets=("wallet", lambda x: ",".join(sorted(set(x)))),
            total_scalper_trades=("trades", "sum"),
        ).reset_index().sort_values("n_scalpers", ascending=False)
        overlap.to_csv(a.out_markets, index=False)
        print(f"\n=== MARKETS WITH MULTIPLE SCALPERS ACTIVE ===")
        show = overlap[overlap.n_scalpers >= 2]
        if len(show) == 0:
            print(f"no markets shared across {len(wallets)} scalpers — they trade different universes")
        else:
            print(f"{len(show)} markets with 2+ scalpers, "
                  f"{len(overlap[overlap.n_scalpers >= 3])} with 3+, "
                  f"{len(overlap[overlap.n_scalpers >= 4])} with 4+\n")
            with pd.option_context("display.width", 260, "display.max_colwidth", 60):
                print(show.head(20).to_string(index=False))
        print(f"\nsaved -> {a.out_markets}")

    print("\nINTERPRETATION GUIDE:")
    print("  MARKET_MAKER          ~50/50 buy-sell, seconds-to-minute turnover")
    print("                        their 'hit rate' is spread capture, not directional edge")
    print("                        NOT COPYABLE — you'd take the other side of their quotes")
    print("  DIRECTIONAL_LONG      >65% buys, held minutes-to-hours")
    print("                        real directional bets — COPYABLE if you can enter <5min after")
    print("  DIRECTIONAL_SHORT     >65% sells / NO side")
    print("                        also copyable directionally")
    print("  SCALPER_MIXED         balanced buy/sell but slower turnover than MM")
    print("                        news-reaction or event scalping — copyable with lag")
    print("  FAST_DAY              same profile as your existing day traders")
    print("\nThe cross-market table shows where multiple scalpers overlap. Markets with 3+")
    print("scalpers active have identifiable microstructure — likely the ones with regular")
    print("news flow, thin books, or predictable retail cycles.")

def cmd_poly_monitor(a):
    """Real-time monitor: polls target wallets every N seconds, alerts on new BUYs.
    Optional cross-reference with a hotspot market list — buys on those markets get
    HIGH-PRIORITY flags. macOS notifications via osascript if --notify."""
    import pandas as pd, subprocess
    DATA = "https://data-api.polymarket.com"

    if not a.wallets:
        try:
            smart = pd.read_csv(a.smart_file)
        except FileNotFoundError:
            print("no --wallets and no smart_wallets_v2.csv"); return
        wallets = [w.strip().lower() for _, w in smart[["wallet"]].dropna().head(5).values.flatten()
                   if isinstance(w, str) and w.startswith("0x")]
        print(f"no --wallets given, using top 5 from {a.smart_file}")
    else:
        try:
            smart = pd.read_csv(a.smart_file)
        except FileNotFoundError:
            smart = pd.DataFrame(columns=["wallet", "name"])
        raw = [x.strip() for x in a.wallets.split(",")]
        wallets = []
        for x in raw:
            if x.lower().startswith("0x"):
                wallets.append(x.lower())
            else:
                m = smart[smart["name"].str.lower() == x.lower()] if not smart.empty else pd.DataFrame()
                if not m.empty:
                    wallets.append(m.iloc[0]["wallet"].lower())
                else:
                    print(f"  [warn] no wallet found for name '{x}'")
    if not wallets:
        print("no valid wallets"); return
    name_map = dict(zip(smart["wallet"].str.lower(), smart["name"])) if not smart.empty else {}

    hotspots = set()
    if a.hotspots_file:
        try:
            h = pd.read_csv(a.hotspots_file)
            hs = h[h.n_scalpers >= a.min_hotspot_scalpers] if "n_scalpers" in h.columns else h
            hotspots = set(hs["market_id"].astype(str).tolist())
            print(f"loaded {len(hotspots)} hotspot markets (>= {a.min_hotspot_scalpers} scalpers)")
        except FileNotFoundError:
            print(f"[warn] no {a.hotspots_file}, running without hotspot filter")

    print(f"\nmonitoring {len(wallets)} wallets, polling every {a.interval}s")
    for w in wallets:
        print(f"  - {name_map.get(w, w[:10])}")
    print(f"press ctrl-c to stop\n")

    last_seen_ts = {}                                    # wallet -> most recent trade ts observed
    start_ts = int(time.time())

    def notify(title, body):
        if not a.notify:
            return
        # macOS native notification via osascript
        try:
            script = f'display notification "{body}" with title "{title}" sound name "Ping"'
            subprocess.run(["osascript", "-e", script],
                           check=False, capture_output=True, timeout=5)
        except (FileNotFoundError, subprocess.SubprocessError):
            pass                                          # not macOS or osascript missing

    poll_num = 0
    try:
        while True:
            poll_num += 1
            new_alerts = 0
            for w in wallets:
                nm = name_map.get(w, w[:10])
                try:
                    r = requests.get(DATA + "/trades",
                        params={"user": w, "limit": 100}, headers=UA, timeout=15)
                except requests.RequestException:
                    continue
                if not r.ok:
                    continue
                trades = r.json() or []
                if not isinstance(trades, list) or not trades:
                    continue
                last_ts = last_seen_ts.get(w, start_ts)
                new_buys = []
                for t in trades:
                    ts = int(t.get("timestamp") or 0)
                    if ts <= last_ts:
                        continue
                    if str(t.get("side") or "").upper() not in ("BUY", "MINT"):
                        continue
                    new_buys.append(t)
                if trades:
                    last_seen_ts[w] = max(last_seen_ts.get(w, 0),
                                          max(int(t.get("timestamp") or 0) for t in trades))

                for t in reversed(new_buys):              # oldest first for readable log
                    ts = int(t.get("timestamp") or 0)
                    mins_ago = round((int(time.time()) - ts) / 60, 1)
                    cid = str(t.get("conditionId") or "")
                    is_hotspot = cid in hotspots
                    tag = "*** HOTSPOT ***" if is_hotspot else ""
                    price = float(t.get("price") or 0)
                    size = float(t.get("size") or 0)
                    notional = price * size
                    outcome = t.get("outcome") or "?"
                    title = (t.get("title") or "")[:70]
                    print(f"\n[{time.strftime('%H:%M:%S')}] {tag}")
                    print(f"  {nm} BUY {outcome} @ ${price:.3f}  size ${notional:.0f}  ({mins_ago}m ago)")
                    print(f"  market: {title}")
                    print(f"  https://polymarket.com/market/{cid}" if cid else "")
                    if is_hotspot:
                        notify(f"HOTSPOT: {nm} bought", f"{outcome} @ ${price:.3f} — {title[:40]}")
                    else:
                        notify(f"{nm} bought", f"{outcome} @ ${price:.3f}")
                    new_alerts += 1
                time.sleep(SLEEP)
            if poll_num % 10 == 0:
                print(f"[{time.strftime('%H:%M:%S')}] poll #{poll_num}, no new activity" if new_alerts == 0
                      else f"[{time.strftime('%H:%M:%S')}] poll #{poll_num}, {new_alerts} alerts fired")
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print(f"\nstopped after {poll_num} polls")

def cmd_poly_wallet_deepdive(a):
    """For each wallet, slice realized round-trips along multiple dimensions
    (topic, price band, hold duration, outcome side, day of week, time of day)
    to find sub-edges. Also does cross-dimensional cuts. Same fee/slippage model
    as poly-copy-backtest so numbers are comparable."""
    import pandas as pd
    DATA = "https://data-api.polymarket.com"

    try:
        smart = pd.read_csv(a.smart_file)
    except FileNotFoundError:
        print(f"missing {a.smart_file}"); return

    if a.wallets:
        raw = [x.strip() for x in a.wallets.split(",")]
        wallets = []
        for x in raw:
            if x.lower().startswith("0x"):
                wallets.append(x.lower())
            else:
                m = smart[smart["name"].str.lower() == x.lower()]
                if not m.empty:
                    wallets.append(m.iloc[0]["wallet"].lower())
    else:
        wallets = []
        for n in ["Fredi9999", "mostly_harmless"]:
            m = smart[smart["name"].str.lower() == n.lower()]
            if not m.empty:
                wallets.append(m.iloc[0]["wallet"].lower())
    if not wallets:
        print("no wallets"); return
    name_map = dict(zip(smart["wallet"].str.lower(), smart["name"]))

    def topic(q):
        q = (q or "").lower()
        if any(k in q for k in ("iran","khamenei","israel","lebanon","hormuz","kharg")): return "iran_israel"
        if any(k in q for k in ("cartel","venezuela","russia","putin","china","qatar","north korea","honduras")): return "geopol_other"
        if "shutdown" in q or "government" in q: return "US_policy"
        if "fed" in q or "interest rate" in q or "powell" in q or "cpi" in q: return "fed"
        if any(k in q for k in ("bitcoin","btc","ether","eth","crypto")): return "crypto"
        if any(k in q for k in ("mayoral","election","president","senate","mamdani")): return "election"
        if any(k in q for k in ("trump","biden","harris","vance")): return "trump_admin"
        if any(k in q for k in ("world cup","champions","masters","boxing","verstappen","nba","nfl")): return "sports"
        return "other"
    FEE_FREE = {"iran_israel", "geopol_other", "US_policy"}
    FEE_MULT = 0.05
    SLIP = 0.01

    def fetch_trades(w, max_trades):
        rows, offset = [], 0
        while offset < max_trades:
            try:
                r = requests.get(DATA + "/trades",
                    params=dict(user=w, limit=500, offset=offset), headers=UA, timeout=25)
            except requests.RequestException: break
            if not r.ok:
                if r.status_code == 429:
                    time.sleep(3); continue
                break
            batch = r.json() or []
            if not isinstance(batch, list) or not batch: break
            rows.extend(batch); offset += 500
            time.sleep(SLEEP)
            if len(batch) < 500: break
        return rows

    def price_band(p):
        if p < 0.15: return "1_veryLow(<15c)"
        if p < 0.30: return "2_low(15-30c)"
        if p < 0.50: return "3_mid(30-50c)"
        if p < 0.70: return "4_high(50-70c)"
        if p < 0.85: return "5_veryHigh(70-85c)"
        return "6_favorite(>85c)"
    def hold_band(h):
        if h < 1: return "1_<1h"
        if h < 6: return "2_1-6h"
        if h < 24: return "3_6-24h"
        if h < 72: return "4_1-3d"
        if h < 168: return "5_3-7d"
        return "6_>7d"
    def time_band(h):
        if 13 <= h < 21: return "US_afternoon(13-21UTC)"
        if h >= 21 or h < 3: return "US_night(21-3UTC)"
        return "EU_asia_hrs(3-13UTC)"

    def compute_pnl(row):
        fm = 0 if row.topic in FEE_FREE else FEE_MULT
        en = min(max(row.entry_price + SLIP, 0.01), 0.99)
        ex = min(max(row.exit_price - SLIP, 0.0), 1.0)
        ef = fm * en * (1-en); xf = fm * ex * (1-ex) if 0 < ex < 1 else 0
        c = a.bet / (en + ef)
        return c * (ex - xf) - c * (en + ef)

    all_wallet_slices = []
    for w in wallets:
        nm = name_map.get(w, w[:10])
        print(f"\n{'='*74}\nDEEP DIVE: {nm}\n{'='*74}")
        trades = fetch_trades(w, a.max_trades)
        if not trades:
            print("no trades fetched"); continue
        df = pd.DataFrame(trades)
        for col in ["price", "size", "timestamp"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df["ts"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
        df = df.dropna(subset=["price", "size", "ts"])

        positions, realized = {}, []
        for _, r in df.sort_values("ts").iterrows():
            asset = r.get("asset"); side = str(r.get("side") or "").upper()
            size = r["size"]
            if not asset or size <= 0: continue
            title = str(r.get("title") or "")[:70]
            outcome = r.get("outcome") or "?"
            if side in ("BUY", "MINT"):
                positions.setdefault(asset, []).append((r["ts"], size, r["price"], outcome, title))
            elif side in ("SELL", "REDEEM"):
                remaining = size
                while remaining > 0 and positions.get(asset):
                    buy_ts, buy_size, buy_price, buy_out, buy_title = positions[asset][0]
                    match = min(buy_size, remaining)
                    realized.append({
                        "entry_ts": buy_ts, "exit_ts": r["ts"],
                        "entry_price": buy_price, "exit_price": r["price"],
                        "outcome": buy_out, "title": buy_title,
                        "size": match,
                        "hold_hrs": (r["ts"] - buy_ts).total_seconds() / 3600,
                    })
                    remaining -= match; buy_size -= match
                    if buy_size <= 0: positions[asset].pop(0)
                    else: positions[asset][0] = (buy_ts, buy_size, buy_price, buy_out, buy_title)
        if not realized:
            print(f"no realized round-trips"); continue
        rt = pd.DataFrame(realized)
        rt["topic"]      = rt["title"].map(topic)
        rt["dow"]        = rt["entry_ts"].dt.day_name()
        rt["is_weekend"] = rt["dow"].isin(["Saturday", "Sunday"])
        rt["hour_utc"]   = rt["entry_ts"].dt.hour
        rt["price_band"] = rt["entry_price"].map(price_band)
        rt["hold_band"]  = rt["hold_hrs"].map(hold_band)
        rt["time_band"]  = rt["hour_utc"].map(time_band)
        rt["pnl"]        = rt.apply(compute_pnl, axis=1)
        rt["wallet"]     = nm

        n = len(rt); tot = rt.pnl.sum(); m = rt.pnl.mean(); s = rt.pnl.std() or 1e-9
        hit = (rt.pnl > 0).mean()
        print(f"Overall: n={n}  total=${tot:+,.0f}  hit={hit:.1%}  Sharpe/trade={m/s:+.3f}")
        print(f"Date range: {rt.entry_ts.min().date()} to {rt.exit_ts.max().date()}")

        def slice_summary(col):
            g = rt.groupby(col).agg(
                n=("pnl", "size"),
                hit=("pnl", lambda x: (x > 0).mean()),
                mean=("pnl", "mean"),
                std=("pnl", "std"),
                total=("pnl", "sum"),
            ).reset_index()
            g["sharpe"] = g["mean"] / g["std"].fillna(1e-9).clip(lower=0.01)
            g["flag"] = g["n"].apply(lambda x: f"n<{a.min_n}" if x < a.min_n else "")
            return g.sort_values("sharpe", ascending=False)

        for dim in ["topic", "outcome", "price_band", "hold_band", "time_band", "dow", "is_weekend"]:
            print(f"\n--- by {dim} ---")
            g = slice_summary(dim)
            with pd.option_context("display.width", 220):
                print(g.round({"hit": 3, "mean": 2, "total": 0, "sharpe": 3, "std": 2}).to_string(index=False))

        print(f"\n--- top 2D slices: topic × price_band (n >= {a.min_n}, sorted by Sharpe) ---")
        cross = rt.groupby(["topic", "price_band"]).agg(
            n=("pnl", "size"),
            hit=("pnl", lambda x: (x > 0).mean()),
            mean=("pnl", "mean"),
            std=("pnl", "std"),
            total=("pnl", "sum"),
        ).reset_index()
        cross["sharpe"] = cross["mean"] / cross["std"].fillna(1e-9).clip(lower=0.01)
        cross = cross[cross["n"] >= a.min_n].sort_values("sharpe", ascending=False)
        with pd.option_context("display.width", 220):
            print(cross.head(15).round({"hit": 3, "mean": 2, "total": 0, "sharpe": 3}).to_string(index=False))

        print(f"\n--- top 2D slices: outcome × topic (n >= {a.min_n}) ---")
        c2 = rt.groupby(["outcome", "topic"]).agg(
            n=("pnl", "size"),
            hit=("pnl", lambda x: (x > 0).mean()),
            mean=("pnl", "mean"),
            std=("pnl", "std"),
            total=("pnl", "sum"),
        ).reset_index()
        c2["sharpe"] = c2["mean"] / c2["std"].fillna(1e-9).clip(lower=0.01)
        c2 = c2[c2["n"] >= a.min_n].sort_values("sharpe", ascending=False)
        with pd.option_context("display.width", 220):
            print(c2.head(15).round({"hit": 3, "mean": 2, "total": 0, "sharpe": 3}).to_string(index=False))

        rt.to_csv(a.out.replace(".csv", f"_{nm}.csv"), index=False)
        print(f"\nsaved -> {a.out.replace('.csv', f'_{nm}.csv')}")
        all_wallet_slices.append(rt)

    # Cross-wallet validation
    if len(all_wallet_slices) >= 2:
        combined = pd.concat(all_wallet_slices, ignore_index=True)
        print(f"\n{'='*74}\nCROSS-WALLET VALIDATION\n{'='*74}")
        print(f"Combined n={len(combined)} across {combined.wallet.nunique()} wallets")
        print(f"\n--- slices where SAME pattern shows up in multiple wallets (min n>={a.min_n} per wallet) ---")
        cross_val = combined.groupby(["topic", "price_band", "wallet"]).agg(
            n=("pnl", "size"),
            sharpe=("pnl", lambda x: x.mean() / (x.std() if x.std() > 0 else 1e-9)),
        ).reset_index()
        cross_val = cross_val[cross_val["n"] >= a.min_n]
        pivot = cross_val.pivot_table(index=["topic","price_band"], columns="wallet",
                                        values="sharpe", aggfunc="first")
        n_pivot = cross_val.pivot_table(index=["topic","price_band"], columns="wallet",
                                          values="n", aggfunc="first")
        if not pivot.empty:
            valid = pivot.dropna(how='any')
            if not valid.empty:
                valid_pos = valid[(valid > 0.15).all(axis=1)]
                print(f"\nslices with Sharpe > 0.15 in ALL wallets shown (real cross-wallet signals):")
                if len(valid_pos):
                    combined_display = valid_pos.round(3).join(
                        n_pivot.rename(columns={c: f"n_{c}" for c in n_pivot.columns}))
                    print(combined_display.to_string())
                else:
                    print("  none — no slice showed positive Sharpe in both wallets simultaneously")
                    print("  (means their edges are wallet-specific, not a universal pattern)")

    print(f"\n{'='*74}\nMULTIPLE HYPOTHESIS WARNING\n{'='*74}")
    print("With 7 dimensions × ~5 buckets each, ~35 slices are tested per wallet.")
    print("At 5% type-I error, ~2 slices/wallet will show 'significant' Sharpe BY CHANCE.")
    print("Only trust a slice if: (a) n >= 50, (b) Sharpe > 0.3, (c) SAME pattern in >=2 wallets,")
    print("AND (d) you can name the mechanism (e.g., 'Fed markets underprice NO because retail")
    print("expects rate cuts more than they happen'). Otherwise it's data-mining noise.")

    print(f"\n{'='*74}\nSTILL TO DO\n{'='*74}")
    print("  #2  widen profitable-wallet net across poly_consistency.csv")
    print("  #3  multi-outcome arb detector")

def cmd_poly_widen_net(a):
    """Test copy-trade viability across many wallets. For each wallet in the source
    file, pulls trade history, computes realized round-trips, simulates copy-trading
    with fees+slippage, and reports Sharpe/DD/hit rate. Filters to qualifiers.
    Answers: are there other profitable wallets besides Fredi/mostly_harmless?"""
    import pandas as pd
    DATA = "https://data-api.polymarket.com"

    try:
        source = pd.read_csv(a.source_file)
    except FileNotFoundError:
        print(f"missing {a.source_file}"); return

    if "week_edge" not in source.columns and "WEEK_pnl" in source.columns:
        source = source.dropna(subset=["DAY_pnl", "WEEK_pnl", "MONTH_pnl", "ALL_pnl", "WEEK_vol"])
        source = source[(source.DAY_pnl >= 0) & (source.WEEK_pnl > 0)
                        & (source.MONTH_pnl > 0) & (source.ALL_pnl > 0)]
        source["week_edge"] = source.WEEK_pnl / source.WEEK_vol
        source = source[(source.week_edge > 0.05) & (source.WEEK_vol.between(500, 100000))]
    if source.empty:
        print("no wallets after filters"); return

    sort_col = "ALL_pnl" if "ALL_pnl" in source.columns else "all_pnl"
    if sort_col in source.columns:
        source = source.sort_values(sort_col, ascending=False)
    wallets = list(zip(source["wallet"].str.lower().head(a.n_wallets),
                        source["name"].head(a.n_wallets).fillna("?")))
    print(f"testing copy-viability across {len(wallets)} wallets "
          f"(target: Sharpe >= {a.min_sharpe}, n >= {a.min_n})")

    def topic(q):
        q = (q or "").lower()
        if any(k in q for k in ("iran","khamenei","israel","lebanon","hormuz","kharg")): return "iran_israel"
        if any(k in q for k in ("cartel","venezuela","russia","putin","china","qatar","north korea","honduras")): return "geopol_other"
        if "shutdown" in q or "government" in q: return "US_policy"
        if "fed" in q or "interest rate" in q or "powell" in q or "cpi" in q: return "fed"
        if any(k in q for k in ("bitcoin","btc","ether","eth","crypto")): return "crypto"
        if any(k in q for k in ("mayoral","election","president","senate","mamdani")): return "election"
        if any(k in q for k in ("trump","biden","harris","vance")): return "trump_admin"
        if any(k in q for k in ("world cup","champions","masters","boxing","verstappen","nba","nfl")): return "sports"
        return "other"
    FEE_FREE = {"iran_israel", "geopol_other", "US_policy"}
    FEE_MULT = 0.05; SLIP = 0.01; BET = 100

    def fetch_trades(w, max_trades):
        rows, offset = [], 0
        while offset < max_trades:
            try:
                r = requests.get(DATA + "/trades",
                    params=dict(user=w, limit=500, offset=offset), headers=UA, timeout=25)
            except requests.RequestException:
                break
            if not r.ok:
                if r.status_code == 429:
                    time.sleep(3); continue
                break
            batch = r.json() or []
            if not isinstance(batch, list) or not batch:
                break
            rows.extend(batch); offset += 500
            time.sleep(SLEEP)
            if len(batch) < 500:
                break
        return rows

    def simulate_wallet(w, nm):
        trades = fetch_trades(w, a.max_trades)
        if not trades:
            return None
        df = pd.DataFrame(trades)
        for col in ["price", "size", "timestamp"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        if "timestamp" not in df.columns:
            return None
        df["ts"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce")
        df = df.dropna(subset=["price", "size", "ts"])
        positions, realized = {}, []
        for _, r in df.sort_values("ts").iterrows():
            asset = r.get("asset"); side = str(r.get("side") or "").upper()
            size = r["size"]
            if not asset or size <= 0:
                continue
            title = str(r.get("title") or "")[:70]
            if side in ("BUY", "MINT"):
                positions.setdefault(asset, []).append((r["ts"], size, r["price"], title))
            elif side in ("SELL", "REDEEM"):
                remaining = size
                while remaining > 0 and positions.get(asset):
                    buy_ts, buy_size, buy_price, buy_title = positions[asset][0]
                    match = min(buy_size, remaining)
                    realized.append({
                        "entry_ts": buy_ts, "exit_ts": r["ts"],
                        "entry_price": buy_price, "exit_price": r["price"],
                        "title": buy_title,
                    })
                    remaining -= match; buy_size -= match
                    if buy_size <= 0:
                        positions[asset].pop(0)
                    else:
                        positions[asset][0] = (buy_ts, buy_size, buy_price, buy_title)
        if not realized:
            return None
        rt = pd.DataFrame(realized)
        rt["topic"] = rt["title"].map(topic)
        pnls, running, peak, dd = [], 2000, 2000, 0
        for _, r in rt.iterrows():
            fm = 0 if r.topic in FEE_FREE else FEE_MULT
            en = min(max(r.entry_price + SLIP, 0.01), 0.99)
            ex = min(max(r.exit_price - SLIP, 0.0), 1.0)
            ef = fm * en * (1-en); xf = fm * ex * (1-ex) if 0 < ex < 1 else 0
            c = BET / (en + ef)
            pnl = c * (ex - xf) - c * (en + ef)
            pnls.append(pnl); running += pnl
            peak = max(peak, running); dd = min(dd, running - peak)
        s = pd.Series(pnls)
        return {
            "wallet": w, "name": nm, "n": len(pnls),
            "hit_rate": float((s > 0).mean()),
            "total_pnl": float(s.sum()),
            "mean_pnl": float(s.mean()),
            "sharpe": float(s.mean() / s.std()) if s.std() > 0 else 0.0,
            "max_dd": float(dd),
            "max_dd_pct": float(dd / 2000 * 100),
            "days_covered": float((rt.exit_ts.max() - rt.entry_ts.min()).total_seconds() / 86400)
                if len(rt) else 0,
        }

    results = []
    for i, (w, nm) in enumerate(wallets):
        if (i + 1) % 5 == 0 or i == 0:
            print(f"[{i+1}/{len(wallets)}] {nm}...")
        r = simulate_wallet(w, nm)
        if r:
            results.append(r)
    if not results:
        print("no wallet produced round-trip data"); return

    R = pd.DataFrame(results).sort_values("sharpe", ascending=False).reset_index(drop=True)
    R["dollars_per_week"] = R["total_pnl"] / (R["days_covered"] / 7).clip(lower=1)

    print(f"\n{'='*74}\nALL TESTED WALLETS (sorted by Sharpe)\n{'='*74}")
    show_cols = ["name", "n", "hit_rate", "sharpe", "total_pnl", "dollars_per_week",
                 "max_dd_pct", "days_covered"]
    with pd.option_context("display.width", 240, "display.max_colwidth", 20):
        print(R[show_cols].round({"hit_rate": 3, "sharpe": 3, "total_pnl": 0,
                                    "dollars_per_week": 1, "max_dd_pct": 1,
                                    "days_covered": 0}).to_string(index=False))

    qualified = R[(R.n >= a.min_n) & (R.sharpe >= a.min_sharpe)
                  & (R.max_dd_pct >= -a.max_dd_pct_allowed)]
    print(f"\n{'='*74}\nQUALIFIED (n>={a.min_n}, Sharpe>={a.min_sharpe}, DD>={-a.max_dd_pct_allowed}%)\n{'='*74}")
    if len(qualified) == 0:
        print("no wallets qualify — try lower --min-sharpe or --min-n")
    else:
        print(qualified[show_cols].round({"hit_rate": 3, "sharpe": 3, "total_pnl": 0,
                                            "dollars_per_week": 1, "max_dd_pct": 1,
                                            "days_covered": 0}).to_string(index=False))
        qualified.to_csv(a.out, index=False)
        print(f"\nsaved {len(qualified)} qualified wallets -> {a.out}")
        print(f"\nUse with: python kalshi_weather_edge.py poly-copy-backtest "
              f"--smart-file {a.out} --wallets {','.join(qualified.head(6)['name'])}")

    print(f"\nNOTE: individual wallet Sharpes above 0.20 with n>=200 are the credible ones.")
    print("      Cross-validate any addition against poly-wallet-deepdive before deploying.")
    print(f"\n{'='*74}\nSTILL TO DO\n{'='*74}")
    print("  #3  multi-outcome arb detector")

def cmd_selftest(_):
    # fee math vs Kalshi's published table (eff. 2026-02-05)
    assert kalshi_fee(0.50, 1) == 0.02
    assert kalshi_fee(0.50, 100) == 1.75
    assert kalshi_fee(0.10, 100) == 0.63
    assert kalshi_fee(0.05, 100) == 0.34
    assert kalshi_fee(0.50, 100, mult=0.0175) == 0.44      # maker
    assert kalshi_fee(0.50, 100, mult=0.035) == 0.88       # INX/NASDAQ100
    # book walk: buy 60 YES against NO bids [(0.40,50),(0.35,100)] -> costs 0.60x50 + 0.65x10
    book = {"orderbook": {"no": [[0.35, 100], [0.40, 50]], "yes": []}}
    v, f, w = walk_fill(book, "yes", 60)
    assert f == 60 and abs(v - (0.60 * 50 + 0.65 * 10) / 60) < 1e-9 and abs(w - 0.65) < 1e-9
    # cent-scaled book handled
    v2, f2, _ = walk_fill({"orderbook": {"no": [[40, 50]], "yes": []}}, "yes", 10)
    assert f2 == 10 and abs(v2 - 0.60) < 1e-9
    # bucket probabilities
    assert abs(ensemble_bucket_p([84.6] * 50, 0.0, 84.5, 85.5) - 1.0) < 1e-9
    p1 = gaussian_bucket_p(85, 0, 0.6, 84.5, 85.5)     # sigma floors at 1.0F by design
    assert 0.35 < p1 < 0.42 and abs(p1 - gaussian_bucket_p(85, 0, 0.1, 84.5, 85.5)) < 1e-12
    lo, hi = bucket_bounds(dict(strike_type="between", floor_strike=82.5, cap_strike=84.5))
    assert (lo, hi) == (82.5, 84.5)
    lo, hi = bucket_bounds(dict(strike_type="between", floor_strike=83, cap_strike=84))
    assert (lo, hi) == (82.5, 84.5)
    assert size_for(0.40) == 100 and size_for(0.40, depth=25) == 25
    print("selftest OK — fees match the published table; walker and buckets behave.")

# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("selftest"); sub.add_parser("discover")
    s = sub.add_parser("snapshot")
    s.add_argument("--cities", default="NYC,CHI,MIA")
    s.add_argument("--log", default="paper_log.csv")
    b = sub.add_parser("backtest")
    b.add_argument("--cities", default="NYC")
    b.add_argument("--start", required=True); b.add_argument("--end", required=True)
    b.add_argument("--fit-days", type=int, default=45)
    b.add_argument("--out", default="backtest_trades.csv")
    c = sub.add_parser("calibration-pull")
    c.add_argument("--days", type=int, default=60)
    c.add_argument("--min-volume", type=float, default=250)
    c.add_argument("--horizon-hours", type=int, default=24)
    c.add_argument("--series", default=None, help="comma-sep series tickers; overrides default top-N")
    c.add_argument("--max-series", type=int, default=150)
    c.add_argument("--max-markets", type=int, default=60000)
    c.add_argument("--max-candles", type=int, default=800)
    c.add_argument("--out", default="kalshi_calibration.csv")
    p = sub.add_parser("poly-calibration")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--min-volume", type=float, default=5000,
                   help="min USD volume filter")
    p.add_argument("--horizon-hours", type=int, default=168,
                   help="default 168 = T-7d, matches deadline-NO thesis")
    p.add_argument("--max-markets", type=int, default=5000)
    p.add_argument("--max-prices", type=int, default=800)
    p.add_argument("--out", default="poly_calibration.csv")
    sm = sub.add_parser("poly-smartmoney")
    sm.add_argument("--top-n", type=int, default=200,
                    help="how many all-time top wallets to keep before recency filter")
    sm.add_argument("--out", default="smart_wallets.csv")
    ps = sub.add_parser("poly-scan")
    ps.add_argument("--smart-file", default="smart_wallets.csv")
    ps.add_argument("--min-price", type=float, default=0.05)
    ps.add_argument("--max-price", type=float, default=0.15)
    ps.add_argument("--min-volume", type=float, default=100000)
    ps.add_argument("--min-days", type=float, default=3)
    ps.add_argument("--max-days", type=int, default=21)
    ps.add_argument("--min-smart", type=int, default=2, help="min smart wallets net-long YES")
    ps.add_argument("--max-markets", type=int, default=2000)
    ps.add_argument("--out", default="poly_scan.csv")
    pb = sub.add_parser("poly-backtest")
    pb.add_argument("--data", default="poly_calibration.csv")
    pb.add_argument("--side", choices=["yes", "no"], default="yes",
                    help="'yes' = buy YES (H3 longshots); 'no' = buy NO / fade favorites")
    pb.add_argument("--min-price", type=float, default=0.05,
                    help="lower bound of YES price band (regardless of side)")
    pb.add_argument("--max-price", type=float, default=0.15,
                    help="upper bound of YES price band")
    pb.add_argument("--bet", type=float, default=100.0, help="dollars per trade")
    pb.add_argument("--bankroll", type=float, default=2000.0)
    pb.add_argument("--plot", action="store_true", help="save equity+drawdown chart as PNG")
    pb.add_argument("--out", default=None, help="output PNG path (default: auto-named)")
    pc = sub.add_parser("poly-consistency")
    pc.add_argument("--out", default="poly_consistency.csv")
    pf = sub.add_parser("poly-follow")
    pf.add_argument("--smart-file", default="smart_wallets_v2.csv")
    pf.add_argument("--min-wallets", type=int, default=2)
    pf.add_argument("--top", type=int, default=30)
    pf.add_argument("--out", default="poly_follow.csv")
    pt = sub.add_parser("poly-timing")
    pt.add_argument("--smart-file", default="smart_wallets_v2.csv")
    pt.add_argument("--wallets", default=None,
                    help="comma-sep names or 0x addresses; default = top-N by all-time PnL")
    pt.add_argument("--top-n", type=int, default=10)
    pt.add_argument("--max-trades", type=int, default=5000)
    pt.add_argument("--out", default="poly_timing.csv")
    pr = sub.add_parser("poly-recent")
    pr.add_argument("--smart-file", default="smart_wallets_v2.csv")
    pr.add_argument("--wallets", default=None,
                    help="comma-sep names or 0x addresses; default = small-account DAY traders from poly_timing.csv")
    pr.add_argument("--hours", type=int, default=24)
    pr.add_argument("--top-n", type=int, default=10)
    pr.add_argument("--out", default="poly_recent.csv")
    cb = sub.add_parser("poly-copy-backtest")
    cb.add_argument("--smart-file", default="smart_wallets_v2.csv")
    cb.add_argument("--wallets", default=None,
                    help="comma-sep names or 0x; default = small-account DAY traders from poly_timing.csv")
    cb.add_argument("--top-n", type=int, default=10)
    cb.add_argument("--max-trades", type=int, default=5000)
    cb.add_argument("--bet", type=float, default=100.0)
    cb.add_argument("--bankroll", type=float, default=2000.0)
    cb.add_argument("--slippage", type=float, default=0.01, help="one-way slippage in dollars")
    cb.add_argument("--min-hold-hrs", type=float, default=0,
                    help="drop round-trips held less than this (uncopyable)")
    cb.add_argument("--plot", action="store_true")
    cb.add_argument("--plot-out", default="poly_copy_backtest.png")
    cb.add_argument("--out", default="poly_copy_backtest.csv")
    sc = sub.add_parser("poly-scalper-scan")
    sc.add_argument("--n-markets", type=int, default=20,
                    help="how many top-volume active markets to sample from")
    sc.add_argument("--min-market-vol", type=float, default=100000)
    sc.add_argument("--min-trades-in-sample", type=int, default=10,
                    help="threshold to qualify as a candidate for verification")
    sc.add_argument("--min-markets", type=int, default=3,
                    help="wallet must have traded this many distinct markets in the sample")
    sc.add_argument("--verify-n", type=int, default=25,
                    help="how many top candidates to verify with full history pull")
    sc.add_argument("--max-trades", type=int, default=3000)
    sc.add_argument("--out", default="poly_scalpers.csv")
    sa = sub.add_parser("poly-scalper-analyze")
    sa.add_argument("--scalper-file", default="poly_scalpers.csv")
    sa.add_argument("--wallets", default=None,
                    help="explicit comma-sep 0x addresses; default = top-N from scalper-file")
    sa.add_argument("--top-n", type=int, default=8)
    sa.add_argument("--max-trades", type=int, default=5000)
    sa.add_argument("--top-markets-per-wallet", type=int, default=15)
    sa.add_argument("--out", default="poly_scalper_profiles.csv")
    sa.add_argument("--out-markets", default="poly_scalper_markets.csv")
    mn = sub.add_parser("poly-monitor")
    mn.add_argument("--wallets", default="Fredi9999,mostly_harmless")
    mn.add_argument("--smart-file", default="smart_wallets_v2.csv")
    mn.add_argument("--hotspots-file", default="poly_scalper_markets.csv",
                    help="cross-reference against markets with N+ scalpers active")
    mn.add_argument("--min-hotspot-scalpers", type=int, default=3)
    mn.add_argument("--interval", type=int, default=90,
                    help="poll interval in seconds")
    mn.add_argument("--notify", action="store_true", help="macOS native notifications")
    dd = sub.add_parser("poly-wallet-deepdive")
    dd.add_argument("--smart-file", default="smart_wallets_v2.csv")
    dd.add_argument("--wallets", default=None,
                    help="comma-sep names/addresses; default = Fredi9999,mostly_harmless")
    dd.add_argument("--max-trades", type=int, default=5000)
    dd.add_argument("--min-n", type=int, default=20,
                    help="minimum sample size per slice — smaller = more noise")
    dd.add_argument("--bet", type=float, default=100.0)
    dd.add_argument("--out", default="poly_deepdive.csv")
    wn = sub.add_parser("poly-widen-net")
    wn.add_argument("--source-file", default="poly_consistency.csv",
                    help="wallets to test; use poly_consistency.csv (raw) or smart_wallets_v2.csv (filtered)")
    wn.add_argument("--n-wallets", type=int, default=100,
                    help="cap wallets tested (from top of source file)")
    wn.add_argument("--max-trades", type=int, default=3000)
    wn.add_argument("--min-n", type=int, default=100,
                    help="min realized round-trips to trust the Sharpe")
    wn.add_argument("--min-sharpe", type=float, default=0.15)
    wn.add_argument("--max-dd-pct-allowed", type=float, default=40.0,
                    help="max drawdown percentage allowed (positive number)")
    wn.add_argument("--out", default="smart_wallets_v3.csv")
    a = ap.parse_args()
    {"selftest": cmd_selftest, "discover": cmd_discover, "snapshot": cmd_snapshot,
     "backtest": cmd_backtest, "calibration-pull": cmd_calibration_pull,
     "poly-calibration": cmd_poly_calibration,
     "poly-smartmoney": cmd_poly_smartmoney,
     "poly-scan": cmd_poly_scan,
     "poly-backtest": cmd_poly_backtest,
     "poly-consistency": cmd_poly_consistency,
     "poly-follow": cmd_poly_follow,
     "poly-timing": cmd_poly_timing,
     "poly-recent": cmd_poly_recent,
     "poly-copy-backtest": cmd_poly_copy_backtest,
     "poly-scalper-scan": cmd_poly_scalper_scan,
     "poly-scalper-analyze": cmd_poly_scalper_analyze,
     "poly-monitor": cmd_poly_monitor,
     "poly-wallet-deepdive": cmd_poly_wallet_deepdive,
     "poly-widen-net": cmd_poly_widen_net}[a.cmd](a)

if __name__ == "__main__":
    main()
