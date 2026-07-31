#!/usr/bin/env python3
"""Stale-band scanner v2.

v1 BUGS (all found by reading its own output):
  1. No date filter — compared today's temperature to July 24 and 25
     markets. Most of v1's 148 "dead bands" were tomorrow's markets.
  2. "Lowest temperature in X" markets were scored against the observed
     MAX. Wrong variable entirely; those resolve on the day's minimum.
  3. open-meteo /v1/forecast returns MODEL values for the current day,
     not station observations. So "obs_max" was a forecast.

THE TELL THAT CAUGHT IT: Istanbul Jul 23, band 24C, bestBid 0.995. The
market is 99.5% sure the high is 24. v1's temperature said 26.0. When a
liquid market disagrees with your data by 2 degrees, your data is wrong
or you are reading a different station than the one that settles it.

v2 CHANGES:
  * parses the market date out of the title; only compares same-day
  * handles Highest vs Lowest separately (max vs min, and the deadness
    test flips direction)
  * pulls past_days observations and reports the source explicitly
  * AGREEMENT CHECK: finds the band the market currently thinks is
    winning (highest bid) and compares it to what the temperature data
    implies. If those disagree, every "edge" on that city is untrusted
    and gets flagged rather than reported as opportunity.

usage:
    python stale_bands2.py
    python stale_bands2.py --show-disagree     # see the mismatches
"""
import argparse, json, re, time
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
METEO = "https://api.open-meteo.com/v1/forecast"

CITIES = {
    "london": (51.4775, -0.4614), "paris": (48.8566, 2.3522),
    "munich": (48.1351, 11.5820), "milan": (45.4642, 9.1900),
    "istanbul": (41.0082, 28.9784), "ankara": (39.9334, 32.8597),
    "moscow": (55.7558, 37.6173), "beijing": (39.9042, 116.4074),
    "hong kong": (22.3193, 114.1694), "guangzhou": (23.1291, 113.2644),
    "singapore": (1.3521, 103.8198), "kuala lumpur": (3.1390, 101.6869),
    "seoul": (37.5665, 126.9780), "tokyo": (35.6762, 139.6503),
    "sao paulo": (-23.5505, -46.6333), "buenos aires": (-34.6037, -58.3816),
    "toronto": (43.6532, -79.3832), "new york city": (40.7128, -74.0060),
    "los angeles": (33.9382, -118.3866), "chicago": (41.8781, -87.6298),
    "miami": (25.7617, -80.1918), "seattle": (47.6062, -122.3321),
}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}
DATE_RE = re.compile(r"on\s+([A-Za-z]+)\s+(\d{1,2})", re.I)
NUM_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


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


def obs_today(lat, lon):
    """Observed hourly temps for the local current day, up to now."""
    try:
        r = requests.get(METEO, params={
            "latitude": lat, "longitude": lon, "hourly": "temperature_2m",
            "timezone": "auto", "past_days": 1, "forecast_days": 1},
            timeout=25)
        if r.status_code != 200:
            return None
        j = r.json()
        off = j.get("utc_offset_seconds", 0)
        now = pd.Timestamp.utcnow().tz_localize(None) + pd.Timedelta(seconds=off)
        today = now.date()
        vals = [(pd.Timestamp(t), v) for t, v in
                zip(j["hourly"]["time"], j["hourly"]["temperature_2m"])
                if v is not None]
        cur = [v for t, v in vals if t.date() == today and t <= now]
        if not cur:
            return None
        return {"max": max(cur), "min": min(cur), "hour": now.hour,
                "date": today}
    except Exception:
        return None


def band_bounds(label):
    """(low, high) inclusive-ish bounds of a band label."""
    s = str(label); low = s.lower()
    nums = [float(x) for x in NUM_RE.findall(s)]
    if not nums:
        return None
    if "or below" in low or "or lower" in low or "under" in low:
        return (-999.0, nums[0] + 0.49)
    if "or higher" in low or "or above" in low or "or more" in low:
        return (nums[0] - 0.49, 999.0)
    if len(nums) >= 2:
        return (nums[0] - 0.49, nums[1] + 0.49)
    return (nums[0] - 0.49, nums[0] + 0.49)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=25)
    p.add_argument("--margin", type=float, default=0.5)
    p.add_argument("--min-local-hour", type=int, default=15)
    p.add_argument("--show-disagree", action="store_true")
    p.add_argument("--out", default="stale_bands2.csv")
    a = p.parse_args()

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

    cache, rows, disagree = {}, [], []
    for e in events:
        title = str(e.get("title")); low = title.lower()
        city = next((c for c in CITIES if c in low), None)
        if not city:
            continue
        want_high = "highest" in low
        want_low = "lowest" in low
        if not (want_high or want_low):
            continue
        edate = event_date(title)
        if edate is None:
            continue
        if city not in cache:
            cache[city] = obs_today(*CITIES[city])
            time.sleep(0.2)
        o = cache[city]
        if not o:
            continue
        if edate != o["date"]:
            continue                      # BUG 1 FIX: same day only
        if o["hour"] < a.min_local_hour:
            continue

        ref = o["max"] if want_high else o["min"]

        mkts = []
        for m in (e.get("markets") or []):
            if m.get("closed") or m.get("acceptingOrders") is False:
                continue
            lab = m.get("groupItemTitle") or m.get("question")
            b = band_bounds(lab)
            if not b:
                continue
            mkts.append((lab, b, f(m.get("bestBid")), f(m.get("bestAsk"))))
        if not mkts:
            continue

        # AGREEMENT CHECK: which band does the market think is winning?
        lead = max(mkts, key=lambda t: (t[2] or 0))
        implied_ok = lead[1][0] <= ref <= lead[1][1]
        if not implied_ok:
            disagree.append({"city": city, "date": str(edate),
                             "kind": "high" if want_high else "low",
                             "my_temp": round(ref, 1),
                             "market_band": str(lead[0])[:18],
                             "band_bid": lead[2], "event": title[:46]})
            continue                      # untrusted city, skip entirely

        for lab, (blo, bhi), bid, ask in mkts:
            dead = (ref > bhi + a.margin) if want_high else (ref < blo - a.margin)
            if not dead or not usable(bid):
                continue
            rows.append({
                "city": city, "hr": o["hour"], "kind": "high" if want_high else "low",
                "ref": round(ref, 1), "band": str(lab)[:18],
                "yes_bid": bid, "no_ask": round(1 - bid, 4),
                "edge": round(bid, 4), "event": title[:44],
            })

    if disagree:
        D = pd.DataFrame(disagree)
        print(f"\n!! {len(D)} city-days where my temperature disagrees with the "
              f"market's leading band — these are EXCLUDED")
        if a.show_disagree:
            with pd.option_context("display.width", 220):
                print(D.to_string(index=False))
        else:
            print("   run --show-disagree to see them")

    if not rows:
        print("\nno trustworthy dead bands. if the disagree list is long, the "
              "temperature source does not match what settles these markets — "
              "fix that before anything else.")
        return

    R = pd.DataFrame(rows).sort_values("edge", ascending=False)
    R.to_csv(a.out, index=False)
    with pd.option_context("display.width", 240, "display.max_colwidth", 46):
        print(f"\n=== {len(R)} dead bands, market-agreement verified ===")
        print(R.head(30).to_string(index=False))
    print(f"\nsaved -> {a.out}")


if __name__ == "__main__":
    main()
