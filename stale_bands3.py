#!/usr/bin/env python3
"""Stale-band scanner v3 — resolves against the ACTUAL settlement station.

WHY v2 FAILED
-------------
All five city-days disagreed with the market, errors 0.7-2.0C in both
directions. The cause, from the market's own metadata:

  resolutionSource: wunderground.com/history/daily/gb/london/EGLC
  "highest temperature recorded at the London City Airport Station"

v2 used an open-meteo grid point at Heathrow (EGLL) ~30km away, and a
MODEL value rather than a station observation. Two errors stacked.

v3 FIX
------
1. Parse the 4-letter ICAO station code out of each event's
   resolutionSource URL. No guessing which airport.
2. Pull real METAR observations for that exact station from
   aviationweather.gov. METAR is what Wunderground displays.
3. Whole degrees: the description says the source "measures temperatures
   to whole degrees Celsius ... this is the level of precision that will
   be used when resolving." So round before comparing.
4. Keep the agreement check. If METAR still disagrees with a market
   bidding >0.95 on a band, something is still wrong and that city is
   excluded rather than traded.

usage:
    python stale_bands3.py --probe          # check the METAR feed
    python stale_bands3.py --show-disagree
"""
import argparse, re, time
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
AWC = "https://aviationweather.gov/api/data/metar"
METEO = "https://api.open-meteo.com/v1/forecast"

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


def usable(x):
    return x is not None and 0.0 < x < 1.0


def station_of(event):
    src = str(event.get("resolutionSource") or "")
    if not src:
        for m in (event.get("markets") or []):
            src = str(m.get("description") or "")
            if "wunderground" in src.lower():
                break
    hit = ICAO_RE.findall(src.upper())
    return hit[-1] if hit else None


def event_date(title):
    m = DATE_RE.search(title)
    if not m:
        return None
    mo = MONTHS.get(m.group(1).lower())
    if not mo:
        return None
    try:
        return pd.Timestamp(year=pd.Timestamp.utcnow().year,
                            month=mo, day=int(m.group(2))).date()
    except ValueError:
        return None


def metar(station, hours=36):
    try:
        r = requests.get(AWC, params={"ids": station, "format": "json",
                                      "hours": hours}, timeout=25)
        if r.status_code != 200:
            return None
        j = r.json()
        out = []
        for o in j:
            t = f(o.get("temp"))
            ts = o.get("obsTime") or o.get("reportTime")
            if t is None or ts is None:
                continue
            try:
                when = (pd.Timestamp(int(ts), unit="s", tz="UTC")
                        if str(ts).isdigit() else
                        pd.Timestamp(ts).tz_localize("UTC"))
            except Exception:
                continue
            out.append((when, t))
        return sorted(out)
    except Exception as e:
        print(f"    metar error {station}: {type(e).__name__}")
        return None


def tz_offset(station_lat=None, lon=None, cache={}):
    return 0


def band_bounds(label):
    s = str(label); low = s.lower()
    nums = [float(x) for x in NUM_RE.findall(s)]
    if not nums:
        return None
    if "below" in low or "lower" in low or "under" in low:
        return (-999.0, nums[0])
    if "higher" in low or "above" in low or "more" in low:
        return (nums[0], 999.0)
    if len(nums) >= 2:
        return (nums[0], nums[1])
    return (nums[0], nums[0])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=25)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--show-disagree", action="store_true")
    p.add_argument("--utc-offset", type=float, default=None,
                   help="override local-day offset in hours for all stations")
    p.add_argument("--out", default="stale_bands3.csv")
    a = p.parse_args()

    if a.probe:
        print("=== METAR probe: EGLC (London City) ===")
        obs = metar("EGLC")
        if not obs:
            print("  no data — endpoint or param wrong"); return
        print(f"  {len(obs)} observations")
        for w, t in obs[-8:]:
            print(f"    {w}  {t}C")
        print(f"\n  max over window: {max(t for _, t in obs)}C")
        print(f"  min over window: {min(t for _, t in obs)}C")
        return

    print("pulling temperature events...")
    events, offset = [], 0
    for _ in range(a.pages):
        r = requests.get(f"{GAMMA}/events",
                         params={"closed": "false", "limit": 100,
                                 "offset": offset, "order": "volume24hr",
                                 "ascending": "false"}, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        events += [e for e in batch
                   if "temperature" in str(e.get("title") or "").lower()]
        offset += 100
        time.sleep(0.15)
    print(f"  {len(events)} temperature events")

    cache, rows, disagree, nostation = {}, [], [], 0
    for e in events:
        title = str(e.get("title")); low = title.lower()
        want_high = "highest" in low
        want_low = "lowest" in low
        if not (want_high or want_low):
            continue
        edate = event_date(title)
        if edate is None:
            continue
        st = station_of(e)
        if not st:
            nostation += 1
            continue
        if st not in cache:
            cache[st] = metar(st)
            time.sleep(0.25)
        obs = cache[st]
        if not obs:
            continue

        off = pd.Timedelta(hours=a.utc_offset) if a.utc_offset is not None \
            else pd.Timedelta(0)
        day = [(w, t) for w, t in obs
               if (w + off).date() == edate]
        if len(day) < 6:
            continue                       # not enough of the day observed
        ref = max(t for _, t in day) if want_high else min(t for _, t in day)
        ref = round(ref)                   # source resolves to whole degrees
        last = (day[-1][0] + off)

        mkts = []
        for m in (e.get("markets") or []):
            if m.get("closed") or m.get("acceptingOrders") is False:
                continue
            lab = m.get("groupItemTitle") or m.get("question")
            b = band_bounds(lab)
            if not b:
                continue
            mkts.append((lab, b, f(m.get("bestBid"))))
        if not mkts:
            continue

        lead = max(mkts, key=lambda t: (t[2] or 0))
        if not (lead[1][0] <= ref <= lead[1][1]):
            disagree.append({"station": st, "date": str(edate),
                             "kind": "high" if want_high else "low",
                             "metar": ref, "market_band": str(lead[0])[:16],
                             "bid": lead[2], "obs_n": len(day),
                             "last_obs": str(last)[:16]})
            continue

        for lab, (blo, bhi), bid in mkts:
            dead = (ref > bhi) if want_high else (ref < blo)
            if not dead or not usable(bid):
                continue
            rows.append({"station": st, "date": str(edate),
                         "kind": "high" if want_high else "low",
                         "metar": ref, "band": str(lab)[:16],
                         "yes_bid": bid, "buy_no_at": round(1 - bid, 4),
                         "edge": round(bid, 4), "obs_n": len(day),
                         "event": title[:40]})

    if nostation:
        print(f"  {nostation} events had no parseable station")
    if disagree:
        D = pd.DataFrame(disagree)
        print(f"\n!! {len(D)} city-days still disagreeing — EXCLUDED")
        if a.show_disagree:
            with pd.option_context("display.width", 220):
                print(D.to_string(index=False))

    if not rows:
        print("\nno verified dead bands right now.")
        return

    R = pd.DataFrame(rows).sort_values("edge", ascending=False)
    R.to_csv(a.out, index=False)
    with pd.option_context("display.width", 240, "display.max_colwidth", 42):
        print(f"\n=== {len(R)} dead bands, METAR-verified ===")
        print(R.head(30).to_string(index=False))
    print(f"\nsaved -> {a.out}")
    print("edge = what you collect per contract selling that worthless YES,")
    print("or 1 - buy_no_at if you buy NO instead. verify on the CLOB book.")


if __name__ == "__main__":
    main()
