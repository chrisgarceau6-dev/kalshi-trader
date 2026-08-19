#!/usr/bin/env python3
"""What a halt actually cost: score the trades the live gates would have taken.

Replays the live entry rules against Kalshi's public candle data for every market
that closed inside a window, and settles them against the real result. Public
endpoints only — no account credentials needed.

    python3 scripts/missed_pnl.py 2026-08-19T14:45:00Z 2026-08-19T18:40:00Z

Caveats, so the number is not over-read:
  - Entry is taken at the EARLIEST qualifying candle, which is what the live poller
    does. The backtest sees every candle, so this is an upper bound on capture.
  - Fills are assumed at the quoted ask. Measured live slippage is +0.105c, and one
    tick removes ~69% of profit, so treat this as optimistic.
  - MAX_CONCURRENT is applied per close cluster, since the series settle together.
"""
import json, sys, time, urllib.request
from collections import defaultdict
from datetime import datetime, timezone
import math

API = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]
MIN_ASK, MAX_ASK, MIN_SECS, MAX_SECS = 90, 93, 150, 600
PRIOR_MIN, MAX_CONCURRENT, BET = 75, 2, 50.0


def get(path, params=""):
    req = urllib.request.Request(f"{API}/{path}?{params}",
                                 headers={"User-Agent": "missed-pnl/1.0"})
    for a in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.6 * (a + 1))
    return {}


def cents(v):
    try:
        return round(float(v) * 100, 2)
    except (TypeError, ValueError):
        return None


def main():
    lo = datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
    hi = datetime.fromisoformat(sys.argv[2].replace("Z", "+00:00"))
    print(f"window {lo:%Y-%m-%d %H:%M}Z -> {hi:%H:%M}Z  ({(hi-lo).total_seconds()/3600:.1f}h)\n")

    entries = []
    for s in SERIES:
        r = get("markets", f"series_ticker={s}&status=settled&limit=200")
        for m in r.get("markets", []):
            ct = m.get("close_time", "")
            if not ct:
                continue
            close = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            if not (lo <= close <= hi) or m.get("result") not in ("yes", "no"):
                continue
            close_ts = int(close.timestamp())
            c = get(f"series/{s}/markets/{m['ticker']}/candlesticks",
                    f"start_ts={close_ts-900}&end_ts={close_ts}&period_interval=1")
            time.sleep(0.05)
            cs = sorted(c.get("candlesticks", []), key=lambda x: x.get("end_period_ts", 0))
            for side in ("yes", "no"):
                asks = []
                for k in cs:
                    try:
                        a = (cents(k["yes_ask"]["close_dollars"]) if side == "yes"
                             else (100 - cents(k["yes_bid"]["close_dollars"])
                                   if cents(k["yes_bid"]["close_dollars"]) else None))
                    except (KeyError, TypeError):
                        a = None
                    asks.append((close_ts - k.get("end_period_ts", 0), a))
                for i, (secs, ask) in enumerate(asks):
                    if ask is None or not (MIN_ASK <= ask <= MAX_ASK):
                        continue
                    if not (MIN_SECS <= secs <= MAX_SECS):
                        continue
                    pri = [asks[j][1] for j in (i - 1, i - 2) if j >= 0]
                    if len(pri) < 2 or any(p is None or p < PRIOR_MIN for p in pri):
                        continue
                    if ask <= 91:
                        p3 = asks[i - 3][1] if i >= 3 else None
                        if p3 is None or p3 < 80:
                            continue
                    entries.append((close_ts, m["ticker"], side, ask, m["result"] == side))
                    break

    if not entries:
        print("no qualifying entries in this window")
        return
    # MAX_CONCURRENT: the series settle together, so cap per close cluster
    by_cluster = defaultdict(list)
    for e in sorted(entries):
        by_cluster[e[0]].append(e)
    taken = [e for c in by_cluster.values() for e in c[:MAX_CONCURRENT]]

    tot = wins = 0
    pnl = 0.0
    for _, tk, side, ask, won in taken:
        p = ask / 100
        n = int(BET / p)
        cost = n * p
        fee = math.ceil(0.07 * n * p * (1 - p) * 100) / 100
        pnl += (n - cost - fee) if won else -(cost + fee)
        tot += 1
        wins += won
    print(f"qualifying signals : {len(entries)}")
    print(f"trades after cap   : {tot}  across {len(by_cluster)} close clusters")
    print(f"win rate           : {wins/tot*100:.1f}%  ({wins}W / {tot-wins}L)")
    print(f"\nMISSED P&L         : ${pnl:+,.2f}   (${pnl/tot:+.2f}/trade at ${BET:.0f} bets)")
    print("  quoted-ask fills, no slippage — optimistic by roughly the 0.105c execution gap")


if __name__ == "__main__":
    main()
