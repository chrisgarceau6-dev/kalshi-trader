#!/usr/bin/env python3
"""Harvest wallets from real trade activity on high-volume markets, then
run the whole batch through wallet_5point2.py's 5-point screen.

WHY THIS SOURCE
---------------
The leaderboard endpoint 404'd. But data-api's /trades endpoint (same
family kalshi_weather_edge.py already uses per-wallet) also supports
querying by market/asset. Pull the highest-volume events from gamma,
grab a market from each, pull its trade tape, and take the wallets who
show up with real size. That's a genuine cross-section of active
traders, not a hand-picked list.

usage:
    python harvest_and_screen.py --probe            # check /trades shape
    python harvest_and_screen.py --n-markets 40 --min-size 200 --max-wallets 150
"""
import argparse, subprocess, sys, time
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def probe():
    print("finding one high-volume market...")
    r = requests.get(f"{GAMMA}/events", params={
        "closed": "false", "limit": 5, "order": "volume24hr",
        "ascending": "false"}, timeout=30)
    ev = r.json()
    if not ev:
        print("no events"); return
    m = (ev[0].get("markets") or [{}])[0]
    cond = m.get("conditionId")
    print(f"market: {m.get('question')}  conditionId={cond}")

    for params in [{"market": cond, "limit": 20},
                  {"conditionId": cond, "limit": 20},
                  {"asset": cond, "limit": 20}]:
        try:
            r = requests.get(f"{DATA}/trades", params=params, timeout=20)
        except Exception as e:
            print(f"{params} -> net error {type(e).__name__}"); continue
        print(f"{params} -> HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            print(f"  {len(j) if isinstance(j, list) else '?'} rows, "
                  f"sample: {str(j)[:300]}")


def harvest(n_markets, min_size, max_wallets):
    print(f"pulling top {n_markets} events by volume...")
    r = requests.get(f"{GAMMA}/events", params={
        "closed": "false", "limit": n_markets, "order": "volume24hr",
        "ascending": "false"}, timeout=30)
    events = r.json() if r.status_code == 200 else []
    print(f"  {len(events)} events")

    conds = []
    for e in events:
        for m in (e.get("markets") or [])[:1]:
            c = m.get("conditionId")
            if c:
                conds.append(c)

    wallets = {}
    for i, cond in enumerate(conds):
        try:
            r = requests.get(f"{DATA}/trades",
                             params={"market": cond, "limit": 200}, timeout=20)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            rows = r.json()
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            w = row.get("proxyWallet") or row.get("wallet") or row.get("maker")
            sz = f(row.get("size"))
            if not w or sz is None or sz < min_size:
                continue
            wallets[w.lower()] = wallets.get(w.lower(), 0) + sz
        if (i + 1) % 10 == 0:
            print(f"  ...{i+1}/{len(conds)} markets, {len(wallets)} wallets so far")
        time.sleep(0.15)
        if len(wallets) >= max_wallets * 3:   # enough raw pool, stop early
            break

    ranked = sorted(wallets.items(), key=lambda t: -t[1])[:max_wallets]
    return [w for w, _ in ranked]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true")
    p.add_argument("--n-markets", type=int, default=40)
    p.add_argument("--min-size", type=float, default=200.0,
                   help="minimum single-trade size to count (filters out noise wallets)")
    p.add_argument("--max-wallets", type=int, default=150)
    p.add_argument("--out", default="harvested_wallets.txt")
    p.add_argument("--min-hold-hrs", type=float, default=24.0)
    p.add_argument("--max-concurrent", type=int, default=20)
    p.add_argument("--bet", type=float, default=100.0)
    p.add_argument("--skip-screen", action="store_true",
                   help="only harvest, don't run wallet_5point2.py")
    a = p.parse_args()

    if a.probe:
        probe(); return

    wallets = harvest(a.n_markets, a.min_size, a.max_wallets)
    if not wallets:
        print("\nharvested nothing. run --probe to check the /trades param shape")
        return
    with open(a.out, "w") as f2:
        f2.write("\n".join(wallets) + "\n")
    print(f"\n{len(wallets)} candidates -> {a.out}")

    if a.skip_screen:
        print(f"next: python wallet_5point2.py --file {a.out} "
              f"--min-hold-hrs {a.min_hold_hrs} --max-concurrent {a.max_concurrent}")
        return

    print(f"\nscreening all {len(wallets)} through wallet_5point2.py "
          f"(this will take a while)...")
    cmd = ["python", "wallet_5point2.py", "--file", a.out,
           "--min-hold-hrs", str(a.min_hold_hrs),
           "--max-concurrent", str(a.max_concurrent),
           "--bet", str(a.bet)]
    subprocess.run(cmd)


if __name__ == "__main__":
    main()
