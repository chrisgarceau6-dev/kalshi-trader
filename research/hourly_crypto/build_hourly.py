#!/usr/bin/env python3
"""Build a candle archive for the hourly crypto ladders (KXBTCD / KXETHD).

Mirrors scripts/archive_candles.py exactly — same ask band (88-96c), same secs
window (100-800s), same NO-ask convention (100 - yes_bid), same prior_1..3 —
so rows are directly comparable to data/candles/*.csv.gz and can be fed to
scripts/backtest.py's qualifies().

Public Kalshi endpoints only; no credentials.

Only strikes that spot came near are fetched: a 188-strike ladder at $100 spacing
has at most a couple of strikes anywhere near the 88-96c band in any given hour.
"""
import csv, gzip, json, os, sys, time, urllib.request, urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.elections.kalshi.com/trade-api/v2"
ASK_MIN, ASK_MAX = 88, 96
SECS_MIN, SECS_MAX = 100, 800
FIELDS = ["series", "ticker", "close_ts", "close_hour", "candle_idx", "side", "ask",
          "secs_left", "won", "prior_1", "prior_2", "prior_3", "floor_strike"]
SPOT = {"KXBTCD": "BTC-USD", "KXETHD": "ETH-USD"}
PAD = {"KXBTCD": 150.0, "KXETHD": 8.0}     # $ around the observed spot range


def get(path, params=None, tries=5):
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for a in range(tries):
        try:
            r = urllib.request.Request(url, headers={"User-Agent": "hourly-archive/1.0"})
            return json.loads(urllib.request.urlopen(r, timeout=30).read())
        except Exception as e:
            if a == tries - 1:
                return None
            time.sleep(min(1.5 * (a + 1), 6))
    return None


def fetch_markets(series, lo, hi):
    out, cursor = [], None
    while True:
        p = {"series_ticker": series, "min_close_ts": lo, "max_close_ts": hi,
             "limit": 1000, "status": "settled"}
        if cursor:
            p["cursor"] = cursor
        r = get("/markets", p)
        if not r:
            break
        out += r.get("markets", [])
        cursor = r.get("cursor")
        if not cursor:
            break
        time.sleep(0.08)
    return out


def candle_price(c, side):
    try:
        if side == "yes":
            return int(round(float(c["yes_ask"]["close_dollars"]) * 100))
        yb = int(round(float(c["yes_bid"]["close_dollars"]) * 100))
        return 100 - yb if yb > 0 else None
    except (KeyError, ValueError, TypeError):
        return None


def main(series, days, end_day):
    spot = {int(k): v for k, v in
            json.load(open(os.path.join(HERE, f"spot_{SPOT[series]}.json"))).items()}
    end = datetime.strptime(end_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    hi = int(end.timestamp()) + 86400
    lo = hi - days * 86400
    print(f"{series}: markets closing {datetime.fromtimestamp(lo, timezone.utc)} "
          f"-> {datetime.fromtimestamp(hi, timezone.utc)}", flush=True)

    mk = fetch_markets(series, lo, hi)
    print(f"  {len(mk):,} settled markets", flush=True)
    by_close = defaultdict(list)
    for m in mk:
        try:
            cts = int(datetime.fromisoformat(
                m["close_time"].replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        if m.get("result"):
            by_close[cts].append(m)
    print(f"  {len(by_close)} hourly closes", flush=True)

    rows, skipped_nospot, jobs = [], 0, []
    for cts, ms in sorted(by_close.items()):
        px = [spot[t] for t in range(cts - 900, cts, 60) if t in spot]
        if len(px) < 5:
            skipped_nospot += 1
            continue
        lo_s, hi_s = min(px) - PAD[series], max(px) + PAD[series]
        for m in ms:
            if lo_s <= float(m.get("floor_strike") or 0) <= hi_s:
                jobs.append((cts, m))
    print(f"  {len(jobs)} candle fetches queued", flush=True)

    done = [0]

    def work(job):
        cts, m = job
        r = get(f"/series/{series}/markets/{m['ticker']}/candlesticks",
                {"start_ts": cts - 900, "end_ts": cts + 10, "period_interval": 1})
        done[0] += 1
        if done[0] % 500 == 0:
            print(f"    {done[0]}/{len(jobs)} fetched", flush=True)
        if not r:
            return []
        cs = sorted(r.get("candlesticks", []), key=lambda c: c.get("end_period_ts", 0))
        strike = float(m.get("floor_strike") or 0)
        hour = datetime.fromtimestamp(cts, tz=timezone.utc).hour
        out = []
        for i, c in enumerate(cs):
            sl = cts - c.get("end_period_ts", 0)
            if not (SECS_MIN <= sl <= SECS_MAX):
                continue
            for side in ("yes", "no"):
                ask = candle_price(c, side)
                if ask is None or not (ASK_MIN <= ask <= ASK_MAX):
                    continue
                pc = lambda o: (candle_price(cs[i - o], side) if i - o >= 0 else None)
                out.append({
                    "series": series, "ticker": m["ticker"], "close_ts": cts,
                    "close_hour": hour, "candle_idx": i, "side": side,
                    "ask": ask, "secs_left": int(sl), "won": m["result"] == side,
                    "prior_1": pc(1) if pc(1) is not None else "",
                    "prior_2": pc(2) if pc(2) is not None else "",
                    "prior_3": pc(3) if pc(3) is not None else "",
                    "floor_strike": strike})
        return out

    with ThreadPoolExecutor(max_workers=12) as ex:
        for res in ex.map(work, jobs):
            rows += res
    fetched = len(jobs)

    out = os.path.join(HERE, f"hourly_{series}.csv.gz")
    with gzip.open(out, "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"{series}: {len(rows):,} rows from {fetched} candle fetches "
          f"({skipped_nospot} closes skipped, no spot) -> {out}", flush=True)


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-08-19"
    import urllib.parse
    for s in ("KXBTCD", "KXETHD"):
        main(s, days, end)
