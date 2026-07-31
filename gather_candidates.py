#!/usr/bin/env python3
"""Gather a large, deduped wallet candidate list for wallet_5point.py.

Sources, in order:
  1. Any local smart_wallets*.csv (wallet column, case-insensitive match
     on 'wallet'/'address'/'proxywallet')
  2. A fresh pull from data-api's leaderboard endpoint, if it responds
     (tries a few known param shapes since this API drifts)

Dedupes, lowercases, writes one 0x address per line.

usage:
    python gather_candidates.py --probe        # check the leaderboard endpoint
    python gather_candidates.py --out wallets.txt --max 60
"""
import argparse, glob, sys, time
import requests
import pandas as pd

DATA = "https://data-api.polymarket.com"


def from_local_csvs():
    found = []
    for path in glob.glob("smart_wallets*.csv") + glob.glob("poly_timing.csv"):
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        col = next((c for c in df.columns
                   if c.lower() in ("wallet", "address", "proxywallet", "_wallet")),
                  None)
        if not col:
            continue
        addrs = [str(a).lower() for a in df[col].dropna()
                if str(a).lower().startswith("0x")]
        print(f"  {path}: {len(addrs)} addresses")
        found += addrs
    return found


def probe():
    for params in [
        {"limit": 20, "window": "month"},
        {"limit": 20, "sortBy": "pnl"},
        {"limit": 20},
    ]:
        for path in ("/leaderboard", "/leaderboard/pnl"):
            try:
                r = requests.get(f"{DATA}{path}", params=params, timeout=20)
            except Exception as e:
                print(f"{path} {params} -> net error {type(e).__name__}")
                continue
            print(f"{path} {params} -> HTTP {r.status_code}")
            if r.status_code == 200:
                j = r.json()
                print(f"  shape: {type(j)}, sample: {str(j)[:300]}")


def from_leaderboard(max_n=100):
    out = []
    for path, params in [
        ("/leaderboard", {"limit": max_n, "window": "month"}),
        ("/leaderboard/pnl", {"limit": max_n}),
        ("/leaderboard", {"limit": max_n}),
    ]:
        try:
            r = requests.get(f"{DATA}{path}", params=params, timeout=20)
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            j = r.json()
        except Exception:
            continue
        rows = j if isinstance(j, list) else j.get("data") or j.get("results") or []
        for row in rows:
            if isinstance(row, dict):
                addr = (row.get("proxyWallet") or row.get("wallet")
                        or row.get("address"))
                if addr:
                    out.append(str(addr).lower())
        if out:
            print(f"  leaderboard via {path}: {len(out)} addresses")
            break
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true")
    p.add_argument("--max", type=int, default=60)
    p.add_argument("--out", default="wallets_candidates.txt")
    a = p.parse_args()

    if a.probe:
        probe(); return

    print("pulling from local CSVs...")
    local = from_local_csvs()
    print("pulling fresh leaderboard...")
    lb = from_leaderboard(a.max)

    combined = list(dict.fromkeys(local + lb))     # dedupe, keep order
    combined = combined[:a.max] if a.max else combined

    if not combined:
        print("\nnothing found. run --probe to see why the leaderboard call "
              "failed, and check smart_wallets*.csv actually has a wallet column")
        return

    with open(a.out, "w") as f:
        f.write("\n".join(combined) + "\n")
    print(f"\n{len(combined)} unique candidates -> {a.out}")
    print(f"next: python wallet_5point.py --file {a.out}")


if __name__ == "__main__":
    main()
