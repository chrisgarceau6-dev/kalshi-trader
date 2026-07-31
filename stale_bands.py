#!/usr/bin/env python3
"""Stale-band scanner: Polymarket temperature bands vs today's observed high.

THE TRADE
---------
"Highest temperature in London on July 23?" has bands 21-or-below, 22,
23 ... 31-or-higher. Exactly one resolves YES. But the day's high is
observable in real time. Once London has already recorded 26.4C, every
band at 25 and under is ALREADY IMPOSSIBLE — its YES is worth exactly
zero and its NO is worth exactly one.

If the book still shows NO offered below 1.00 on a dead band, you buy it
for a guaranteed payout. No forecast, no directional risk. The only
question is whether the quote is real and whether the edge survives fees.

WHY THIS ONE IS DIFFERENT
-------------------------
Every other idea tested required predicting something or renting someone
else's skill. This requires only reading a thermometer faster than the
book updates. The information is public, objective, and already resolved.

TIMING MATTERS
--------------
Daily highs usually land mid-to-late afternoon. At 10am local the temp
will still climb a lot and few bands are safely dead. At 8pm local the
high is effectively locked. The script prints local hour per city so you
can judge; --min-local-hour filters.

usage:
    python stale_bands.py
    python stale_bands.py --min-local-hour 16 --margin 1.0
    python stale_bands.py --city london
"""
import argparse, json, re, time
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
METEO = "https://api.open-meteo.com/v1/forecast"

# city -> (lat, lon). extend freely; keys are matched case-insensitively
# against the event title.
CITIES = {
    "london": (51.4775, -0.4614),        # Heathrow
    "paris": (48.8566, 2.3522),
    "munich": (48.1351, 11.5820),
    "milan": (45.4642, 9.1900),
    "istanbul": (41.0082, 28.9784),
    "ankara": (39.9334, 32.8597),
    "moscow": (55.7558, 37.6173),
    "beijing": (39.9042, 116.4074),
    "hong kong": (22.3193, 114.1694),
    "guangzhou": (23.1291, 113.2644),
    "singapore": (1.3521, 103.8198),
    "kuala lumpur": (3.1390, 101.6869),
    "seoul": (37.5665, 126.9780),
    "tokyo": (35.6762, 139.6503),
    "sydney": (-33.8688, 151.2093),
    "sao paulo": (-23.5505, -46.6333),
    "buenos aires": (-34.6037, -58.3816),
    "mexico city": (19.4326, -99.1332),
    "toronto": (43.6532, -79.3832),
    "new york city": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "los angeles": (33.9382, -118.3866),  # LAX
    "chicago": (41.8781, -87.6298),
    "miami": (25.7617, -80.1918),
    "seattle": (47.6062, -122.3321),
    "denver": (39.7392, -104.9903),
    "phoenix": (33.4484, -112.0740),
    "houston": (29.7604, -95.3698),
    "atlanta": (33.7490, -84.3880),
    "boston": (42.3601, -71.0589),
}


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def usable(x):
    return x is not None and 0.0 < x < 1.0


def jparse(v, d=None):
    if v is None:
        return d
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return d


def observed_today(lat, lon, fahrenheit=False):
    """Return (max_so_far, local_hour, unit) using observed hourly temps."""
    unit = "fahrenheit" if fahrenheit else "celsius"
    try:
        r = requests.get(METEO, params={
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m", "timezone": "auto",
            "forecast_days": 1, "temperature_unit": unit}, timeout=25)
        if r.status_code != 200:
            return None, None
        j = r.json()
        times = j["hourly"]["time"]
        temps = j["hourly"]["temperature_2m"]
        now = pd.Timestamp.utcnow().tz_localize(None) + pd.Timedelta(
            seconds=j.get("utc_offset_seconds", 0))
        cur_hour = now.hour
        vals = [t for ts, t in zip(times, temps)
                if t is not None and pd.Timestamp(ts).hour <= cur_hour]
        if not vals:
            return None, cur_hour
        return max(vals), cur_hour
    except Exception:
        return None, None


BAND_C = re.compile(r"(-?\d+(?:\.\d+)?)\s*°?\s*C", re.I)
BAND_F = re.compile(r"(-?\d+(?:\.\d+)?)\s*°?\s*F", re.I)
RANGE = re.compile(r"(-?\d+(?:\.\d+)?)\s*[-–]\s*(-?\d+(?:\.\d+)?)")


def band_ceiling(label):
    """Upper bound of a band label. None if unparseable or open-ended up."""
    s = str(label)
    low = s.lower()
    if "higher" in low or "above" in low or "or more" in low or "+" in s:
        return None                      # open upward, never dead from below
    rng = RANGE.search(s)
    if rng:
        return float(rng.group(2)) + 0.49
    m = BAND_C.search(s) or BAND_F.search(s)
    if not m:
        return None
    v = float(m.group(1))
    if "below" in low or "under" in low or "less" in low:
        return v + 0.49
    return v + 0.49                      # "24°C" means the high rounds to 24


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=25)
    p.add_argument("--margin", type=float, default=0.5,
                   help="degrees of safety above the band ceiling")
    p.add_argument("--min-local-hour", type=int, default=13)
    p.add_argument("--city", default="")
    p.add_argument("--out", default="stale_bands.csv")
    a = p.parse_args()

    print("pulling Polymarket temperature events...")
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
        for e in batch:
            t = str(e.get("title") or "")
            if "temperature" in t.lower():
                events.append(e)
        offset += 100
        time.sleep(0.15)
    print(f"  {len(events)} temperature events")
    if not events:
        print("none found — raise --pages"); return

    obs_cache = {}
    rows = []
    for e in events:
        title = str(e.get("title"))
        low = title.lower()
        if a.city and a.city.lower() not in low:
            continue
        city = next((c for c in CITIES if c in low), None)
        if not city:
            continue
        is_f = "°f" in low or " f " in low or "fahrenheit" in low
        key = (city, is_f)
        if key not in obs_cache:
            obs_cache[key] = observed_today(*CITIES[city], fahrenheit=is_f)
            time.sleep(0.2)
        omax, hour = obs_cache[key]
        if omax is None:
            continue
        if hour is not None and hour < a.min_local_hour:
            continue

        for m in (e.get("markets") or []):
            if m.get("closed") or m.get("acceptingOrders") is False:
                continue
            label = m.get("groupItemTitle") or m.get("question")
            ceil = band_ceiling(label)
            if ceil is None:
                continue
            if omax <= ceil + a.margin:
                continue                  # not safely dead yet
            yes_bid = f(m.get("bestBid"))
            yes_ask = f(m.get("bestAsk"))
            no_ask = (1.0 - yes_bid) if usable(yes_bid) else None
            rows.append({
                "city": city, "hr": hour,
                "obs_max": round(omax, 1),
                "band": str(label)[:22],
                "ceil": ceil,
                "yes_bid": yes_bid, "yes_ask": yes_ask,
                "no_ask": round(no_ask, 4) if no_ask else None,
                "edge_buy_no": round(1.0 - no_ask, 4) if no_ask else None,
                "edge_sell_yes": yes_bid if usable(yes_bid) else None,
                "event": title[:44],
            })

    if not rows:
        print("\nno safely-dead bands with live quotes right now.")
        print("try later in the day, or --min-local-hour 10 --margin 0.0 to")
        print("see marginal ones (riskier: the high can still climb)")
        return

    R = pd.DataFrame(rows)
    R["best"] = R[["edge_buy_no", "edge_sell_yes"]].max(axis=1)
    R = R.sort_values("best", ascending=False)
    R.to_csv(a.out, index=False)
    with pd.option_context("display.width", 250, "display.max_colwidth", 46):
        print(f"\n=== {len(R)} already-impossible bands still quoted ===")
        print(R.head(35).to_string(index=False))
    print(f"\nsaved -> {a.out}")
    print("\nedge_buy_no   = profit per contract buying NO at (1 - bestBid)")
    print("edge_sell_yes = someone is BIDDING this for a worthless YES")
    print("fees: 0.05*p*(1-p) per contract, tiny at p near 0 or 1")
    print("\nverify on the live CLOB book before acting — gamma quotes were")
    print("already proven stale in this project")


if __name__ == "__main__":
    main()
