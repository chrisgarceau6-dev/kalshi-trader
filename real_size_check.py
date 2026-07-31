#!/usr/bin/env python3
"""Real-dollar position-sizing check for a single wallet.

The 5-point screener normalizes every wallet to $100/trade so wallets
can be compared apples-to-apples. That's useless for answering "can I
actually copy this" -- for that you need the wallet's REAL position
sizes, in real dollars, so you can see what fraction of them your own
bankroll could actually replicate.

usage:
    python real_size_check.py 0xWALLET
"""
import sys, json, time
import requests
import pandas as pd

DATA = "https://data-api.polymarket.com"


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def fetch_all(endpoint, wallet, max_pages=20):
    out, offset = [], 0
    for _ in range(max_pages):
        try:
            r = requests.get(f"{DATA}/{endpoint}",
                             params={"user": wallet, "limit": 500, "offset": offset},
                             timeout=25)
        except Exception:
            break
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 500:
            break
        offset += 500
        time.sleep(0.2)
    return out


def main():
    if len(sys.argv) < 2:
        print("usage: python real_size_check.py 0xWALLET"); return
    wallet = sys.argv[1]

    print(f"pulling real positions for {wallet}...")
    positions = fetch_all("positions", wallet)
    print(f"  {len(positions)} positions (open + resolved-unredeemed)")

    if positions:
        P = pd.DataFrame(positions)
        for c in ("size", "avgPrice", "curPrice", "initialValue", "currentValue"):
            if c in P.columns:
                P[c] = pd.to_numeric(P[c], errors="coerce")
        # real dollar cost basis per position
        if "size" in P.columns and "avgPrice" in P.columns:
            P["dollar_cost"] = P["size"] * P["avgPrice"]
            costs = P["dollar_cost"].dropna()
            costs = costs[costs > 0]
            print(f"\n=== REAL position sizing (dollar cost basis) ===")
            print(f"  n={len(costs)}")
            print(f"  median: ${costs.median():,.2f}")
            print(f"  mean:   ${costs.mean():,.2f}")
            print(f"  min:    ${costs.min():,.2f}")
            print(f"  max:    ${costs.max():,.2f}")
            print(f"  25th pct: ${costs.quantile(0.25):,.2f}")
            print(f"  75th pct: ${costs.quantile(0.75):,.2f}")

            print(f"\n=== what a $2,000 bankroll can actually mirror ===")
            bankroll = 2000
            for frac_name, frac in [("full-size copy (1:1)", 1.0),
                                     ("half-size copy", 0.5),
                                     ("quarter-size copy", 0.25)]:
                n_affordable = (costs <= bankroll * frac).sum()
                pct = 100 * n_affordable / len(costs)
                print(f"  {frac_name}: can afford {n_affordable}/{len(costs)} "
                      f"positions ({pct:.0f}%) if capping each bet at "
                      f"${bankroll*frac:,.0f}")

            n_concurrent = len(P[P.get("size", 0).fillna(0) > 0])
            print(f"\n  currently open/unredeemed positions: {n_concurrent}")
            if n_concurrent > 0:
                implied_concurrent_capital = costs.median() * n_concurrent
                print(f"  if all open positions were median-sized, wallet has "
                      f"~${implied_concurrent_capital:,.0f} deployed at once")
                print(f"  your $2,000 / that = "
                      f"{2000/implied_concurrent_capital*100:.1f}% of their scale")

    print(f"\n--- READ THIS ---")
    print("If most real positions cost far more than what a fraction of your")
    print("$2k could cover, copying this wallet means either (a) skipping")
    print("most of its trades entirely, which breaks the diversification that")
    print("produced its Sharpe, or (b) sizing so small your absolute weekly $")
    print("target becomes unreachable even if the % return transfers cleanly.")


if __name__ == "__main__":
    main()
