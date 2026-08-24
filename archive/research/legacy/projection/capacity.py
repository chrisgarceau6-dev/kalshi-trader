#!/usr/bin/env python3
"""How many contracts can actually be bought inside the 90-93c band right now?

This is the binding constraint on any growth projection. The strategy risks ~$50 to
win ~$4.50, so one cent of slippage removes most of the edge (CLAUDE.md invariant 2).
A bet is only safe while it fits inside the book at or under MAX_ASK_CENTS.

Kalshi book convention (CLAUDE.md §5): orderbook_fp.no_dollars holds NO bids and
yes_dollars holds YES bids. A YES buyer LIFTS NO BIDS, so YES ask = 100 - best NO bid,
and the depth available to a YES buyer at ask <= A is the sum of NO bid size at
prices >= 100 - A.

Public endpoints only.
"""
import json, statistics, sys, time, urllib.parse, urllib.request
from collections import defaultdict

API = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"]
MAX_ASK = 93


def get(path, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    r = urllib.request.Request(url, headers={"User-Agent": "capacity/1.0"})
    try:
        return json.loads(urllib.request.urlopen(r, timeout=20).read())
    except Exception:
        return None


def depth_for(book, side, max_ask):
    """Contracts buyable on `side` at an ask <= max_ask."""
    levels = book.get("no_dollars" if side == "yes" else "yes_dollars") or []
    floor = 100 - max_ask
    tot = 0.0
    for px, qty in levels:
        cents = float(px) * 100 if float(px) < 2 else float(px)
        if cents >= floor:
            tot += float(qty)
    return tot


rows = []
for s in SERIES:
    r = get("/markets", {"series_ticker": s, "status": "open", "limit": 20})
    if not r:
        continue
    for m in r.get("markets", []):
        t = m.get("ticker", "")
        ob = get(f"/markets/{t}/orderbook")
        time.sleep(0.05)
        if not ob:
            continue
        book = ob.get("orderbook_fp") or {}
        for side in ("yes", "no"):
            d = depth_for(book, side, MAX_ASK)
            if d > 0:
                rows.append((s, t, side, d))

print(f"sampled {len(rows)} open (market, side) books across {len(SERIES)} series\n")
if not rows:
    sys.exit("no open books returned — markets may be between closes")

depths = sorted(r[3] for r in rows)


def pct(p):
    return depths[min(int(p * (len(depths) - 1)), len(depths) - 1)]


print("CONTRACTS AVAILABLE AT ASK <= 93c (both sides pooled)")
print(f"  p10 {pct(.10):>7.0f} | p25 {pct(.25):>7.0f} | median {pct(.50):>7.0f} | "
      f"p75 {pct(.75):>7.0f} | p90 {pct(.90):>7.0f}")
print(f"  mean {statistics.mean(depths):.0f}   MIN_BOOK_DEPTH gate is 60\n")

print("WHAT THAT MEANS FOR BET SIZE (at a ~91c entry, contracts = bet / 0.91)")
print(f"  {'bet':>7} {'contracts':>10} {'% of books that fit':>21}")
for bet in (50, 75, 100, 150, 200, 400, 1000):
    ct = bet / 0.91
    fit = sum(1 for d in depths if d >= ct) / len(depths) * 100
    print(f"  ${bet:>6,} {ct:>10.0f} {fit:>20.0f}%")

print("\nBY SERIES (median contracts at <=93c)")
by = defaultdict(list)
for s, t, side, d in rows:
    by[s].append(d)
for s in SERIES:
    if by[s]:
        v = sorted(by[s])
        print(f"  {s:<11} n={len(v):>3}  median {v[len(v)//2]:>7.0f}  "
              f"min {v[0]:>7.0f}  max {v[-1]:>8.0f}")
