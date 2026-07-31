#!/usr/bin/env python3
"""Historical backtest of the stale-band MECHANISM (not a live scan).

THE QUESTION
------------
Not "is there a dead band right now" but "historically, once a weather
market's outcome was mathematically locked in, did the price actually
lag behind 1.00/0.00 for any real time -- and was there ever a window
where a resting bid/ask let you lock a profit before it converged?"

If yes even sometimes: the mechanism is sound, tonight was just bad
timing (early US afternoon, nothing locked in yet). If no -- prices
converge to 0/1 essentially the instant the outcome is determined --
there was never money here, full stop.

DATA SOURCES (new for this test)
---------------------------------
1. Polymarket CLOB /prices-history -- per-token price time series for
   ALREADY-RESOLVED (closed) markets. Free, no auth.
2. Iowa Environmental Mesonet ASOS archive -- historical METAR going
   back years, unlike aviationweather.gov's rolling ~2-day window.
   https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py

usage:
    python stale_bands_backtest.py --probe
    python stale_bands_backtest.py --n-markets 15
"""
import argparse, re, time
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
ASOS = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

ICAO_RE = re.compile(r"/([A-Z]{4})\b")
NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
DATE_RE = re.compile(r"on\s+([A-Za-z]+)\s+(\d{1,2})", re.I)


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def event_date(title):
    m = DATE_RE.search(title)
    if not m:
        return None
    mo = MONTHS.get(m.group(1).lower())
    if not mo:
        return None
    try:
        return pd.Timestamp(year=2026, month=mo, day=int(m.group(2))).date()
    except ValueError:
        return None


def station_of(event):
    src = str(event.get("resolutionSource") or "")
    hit = ICAO_RE.findall(src.upper())
    if hit:
        return hit[-1]
    for m in (event.get("markets") or []):
        desc = str(m.get("description") or "")
        if "wunderground" in desc.lower():
            hit = ICAO_RE.findall(desc.upper())
            if hit:
                return hit[-1]
    return None


def band_bounds(label):
    s = str(label); low = s.lower()
    nums = [float(x) for x in NUM_RE.findall(s)]
    if not nums:
        return None
    if "below" in low or "under" in low:
        return (-999.0, nums[0])
    if "higher" in low or "above" in low:
        return (nums[0], 999.0)
    if len(nums) >= 2:
        return (nums[0], nums[1])
    return (nums[0], nums[0])


def asos_history(station, start, end):
    """Iowa Mesonet historical ASOS 1-min-ish obs for a date range."""
    try:
        r = requests.get(ASOS, params={
            "station": station, "data": "tmpf", "tz": "UTC",
            "year1": start.year, "month1": start.month, "day1": start.day,
            "year2": end.year, "month2": end.month, "day2": end.day,
            "format": "onlycomma", "latlon": "no", "elev": "no",
            "missing": "M", "trace": "T", "direct": "no", "report_type": "3"
        }, timeout=30)
        if r.status_code != 200:
            return None
        lines = r.text.strip().splitlines()
        if len(lines) < 2:
            return None
        rows = []
        for ln in lines[1:]:
            parts = ln.split(",")
            if len(parts) < 3:
                continue
            ts, tmpf = parts[1], parts[2]
            tf = f(tmpf)
            if tf is None:
                continue
            try:
                when = pd.Timestamp(ts, tz="UTC")
            except Exception:
                continue
            rows.append((when, (tf - 32) * 5 / 9))   # store as Celsius
        return sorted(rows)
    except Exception as e:
        print(f"    asos error: {type(e).__name__}")
        return None


def price_history(token, start_ts, end_ts, fidelity=10):
    try:
        r = requests.get(f"{CLOB}/prices-history", params={
            "market": token, "startTs": int(start_ts), "endTs": int(end_ts),
            "fidelity": fidelity}, timeout=25)
        if r.status_code != 200:
            return None, r.status_code
        j = r.json()
        pts = j.get("history") if isinstance(j, dict) else j
        return pts, 200
    except Exception as e:
        return None, str(e)


def probe():
    print("=== finding one CLOSED temperature event ===")
    r = requests.get(f"{GAMMA}/events", params={
        "closed": "true", "limit": 100, "order": "volume24hr",
        "ascending": "false"}, timeout=30)
    evs = [e for e in r.json()
          if "temperature" in str(e.get("title") or "").lower()]
    if not evs:
        print("no closed temperature events in this page -- try more pages")
        return
    e = evs[0]
    print(f"event: {e.get('title')}")
    st = station_of(e)
    print(f"station: {st}")
    edate = event_date(e.get("title"))
    print(f"date: {edate}")

    ms = e.get("markets") or []
    print(f"markets: {len(ms)}")
    if ms:
        m = ms[0]
        print(f"first market outcome/question: {m.get('question')}")
        toks = m.get("clobTokenIds")
        print(f"clobTokenIds raw: {str(toks)[:150]}")
        import json
        try:
            toks = json.loads(toks) if isinstance(toks, str) else toks
        except Exception:
            pass
        if toks:
            tok = toks[0]
            print(f"\n=== CLOB prices-history probe, token {str(tok)[:16]}... ===")
            start = int(pd.Timestamp(edate).timestamp()) if edate else 0
            end = start + 172800
            pts, code = price_history(tok, start, end)
            print(f"status: {code}")
            print(f"points: {len(pts) if pts else 0}")
            if pts:
                print(f"sample: {pts[:5]}")

    if st and edate:
        print(f"\n=== ASOS historical probe, station {st}, date {edate} ===")
        obs = asos_history(st, edate - pd.Timedelta(days=1),
                           edate + pd.Timedelta(days=1))
        print(f"observations: {len(obs) if obs else 0}")
        if obs:
            print(f"sample: {obs[:5]}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true")
    p.add_argument("--n-markets", type=int, default=15)
    p.add_argument("--pages", type=int, default=3)
    a = p.parse_args()

    if a.probe:
        probe(); return

    print("pulling closed temperature events...")
    events, offset = [], 0
    for _ in range(a.pages):
        r = requests.get(f"{GAMMA}/events", params={
            "closed": "true", "limit": 100, "offset": offset,
            "order": "volume24hr", "ascending": "false"}, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        events += [e for e in batch
                  if "temperature" in str(e.get("title") or "").lower()]
        offset += 100
        time.sleep(0.15)
    events = events[:a.n_markets]
    print(f"  testing {len(events)} closed temperature events")

    results = []
    for e in events:
        title = str(e.get("title")); low = title.lower()
        want_high = "highest" in low
        edate = event_date(title)
        st = station_of(e)
        if edate is None or not st:
            continue
        obs = asos_history(st, edate - pd.Timedelta(days=1),
                           edate + pd.Timedelta(days=1))
        time.sleep(0.3)
        if not obs:
            continue
        day = [(w, t) for w, t in obs if w.date() == edate]
        if len(day) < 6:
            continue

        # find the timestamp when the day's eventual max/min was FIRST hit
        # (mechanically: outcome locked in the moment no later reading
        # could beat it, i.e. right when the extreme itself occurs, since
        # temps generally don't re-exceed a same-day peak/trough quickly)
        extreme_val = max(t for _, t in day) if want_high else min(t for _, t in day)
        lock_ts = next(w for w, t in day if t == extreme_val)

        for m in (e.get("markets") or []):
            lab = m.get("groupItemTitle") or m.get("question")
            b = band_bounds(lab)
            if not b:
                continue
            dead_from_lock = (extreme_val > b[1]) if want_high else (extreme_val < b[0])
            if not dead_from_lock:
                continue    # this band was the WINNER, not a dead one
            import json
            toks = m.get("clobTokenIds")
            try:
                toks = json.loads(toks) if isinstance(toks, str) else toks
            except Exception:
                toks = None
            if not toks:
                continue
            start = int(lock_ts.timestamp())
            end = start + 6 * 3600     # 6 hours after lock-in
            pts, code = price_history(toks[0], start, end, fidelity=5)
            time.sleep(0.2)
            if not pts:
                continue
            prices = [f(pt.get("p")) for pt in pts if f(pt.get("p")) is not None]
            if not prices:
                continue
            first_after_lock = prices[0]
            max_after_lock = max(prices)
            mins_to_settle = None
            for i, pt in enumerate(pts):
                pr = f(pt.get("p"))
                if pr is not None and pr <= 0.03:
                    mins_to_settle = i * 5
                    break
            results.append({
                "event": title[:40], "station": st, "band": str(lab)[:18],
                "price_at_lock": round(first_after_lock, 3),
                "max_price_after": round(max_after_lock, 3),
                "residual_edge": round(1 - first_after_lock, 3),
                "mins_to_near_zero": mins_to_settle,
            })

    if not results:
        print("\nno usable historical dead-band instances found -- either "
              "the station/date pairs failed, or price-history returned "
              "nothing. Run --probe to see which step is failing.")
        return

    R = pd.DataFrame(results).sort_values("residual_edge", ascending=False)
    with pd.option_context("display.width", 200, "display.max_colwidth", 42):
        print(f"\n=== {len(R)} historical dead-band instances, "
              f"price at the moment of lock-in ===")
        print(R.to_string(index=False))

    print("\n--- READ THIS ---")
    print("residual_edge = 1 - price right when the outcome became")
    print("mathematically dead. If this is consistently near 0, the market")
    print("prices these correctly in real time and the mechanism has no")
    print("edge. If it is meaningfully positive across several instances,")
    print("that is evidence the strategy is real -- you were just early")
    print("or late on any single day, not wrong about the mechanism.")


if __name__ == "__main__":
    main()
