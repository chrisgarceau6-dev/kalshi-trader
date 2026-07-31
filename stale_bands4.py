#!/usr/bin/env python3
"""Stale-band scanner v4 -- station map built from LIVE resolutionSource,
not guessed from city name.

WHY v3 STILL DISAGREED
-----------------------
v3 hardcoded a city -> ICAO lookup. The confirmed probe just now showed
real stations differ from the obvious guess in ways that matter: NYC ->
KLGA (LaGuardia, not JFK), and some cities returned no resolutionSource
at the event level at all. v4 extracts the ICAO code from EVERY open
temperature event's own resolutionSource/description at run time -- no
hardcoded map.

usage:
    python stale_bands4.py --probe
    python stale_bands4.py --show-disagree
"""
import argparse, re, time
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
AWC = "https://aviationweather.gov/api/data/metar"

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
    except Exception:
        return None


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


def fetch_temp_events(pages):
    events, offset = [], 0
    for _ in range(pages):
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
    return events


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=25)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--show-disagree", action="store_true")
    p.add_argument("--min-local-hour", type=int, default=15)
    p.add_argument("--out", default="stale_bands4.csv")
    a = p.parse_args()

    print("pulling temperature events...")
    events = fetch_temp_events(a.pages)
    print(f"  {len(events)} temperature events")

    stations = {}
    for e in events:
        st = station_of(e)
        title = str(e.get("title"))
        stations[title] = st

    if a.probe:
        found = sum(1 for v in stations.values() if v)
        print(f"\nstation resolved for {found}/{len(stations)} events")
        for t, st in list(stations.items())[:25]:
            print(f"  {st or '???':6s}  {t}")
        return

    cache, rows, disagree = {}, [], []
    for e in events:
        title = str(e.get("title")); low = title.lower()
        want_high = "highest" in low
        want_low = "lowest" in low
        if not (want_high or want_low):
            continue
        edate = event_date(title)
        if edate is None:
            continue
        st = stations.get(title)
        if not st:
            continue
        if st not in cache:
            cache[st] = metar(st)
            time.sleep(0.25)
        obs = cache[st]
        if not obs:
            continue

        day = [(w, t) for w, t in obs if w.date() == edate]
        if len(day) < 6:
            continue
        ref = round(max(t for _, t in day) if want_high else min(t for _, t in day))
        hour = day[-1][0].hour
        if hour < a.min_local_hour:
            continue

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
                             "metar": ref, "market_band": str(lead[0])[:18],
                             "bid": lead[2], "event": title[:42]})
            continue

        for lab, (blo, bhi), bid in mkts:
            dead = (ref > bhi) if want_high else (ref < blo)
            if not dead or not usable(bid):
                continue
            rows.append({"station": st, "date": str(edate),
                         "kind": "high" if want_high else "low",
                         "metar": ref, "band": str(lab)[:18],
                         "yes_bid": bid, "edge": round(bid, 4),
                         "event": title[:42]})

    if disagree:
        D = pd.DataFrame(disagree)
        print(f"\n!! {len(D)} city-days disagreeing -- EXCLUDED")
        if a.show_disagree:
            with pd.option_context("display.width", 220):
                print(D.to_string(index=False))

    if not rows:
        print("\nno verified dead bands right now.")
        return

    R = pd.DataFrame(rows).sort_values("edge", ascending=False)
    R.to_csv(a.out, index=False)
    with pd.option_context("display.width", 220, "display.max_colwidth", 44):
        print(f"\n=== {len(R)} dead bands, METAR-verified (live station map) ===")
        print(R.to_string(index=False))
    print(f"\nsaved -> {a.out}")
    print("edge = profit per contract selling that worthless YES / buying NO")
    print("verify on the live CLOB book before acting")


if __name__ == "__main__":
    main()
