#!/usr/bin/env python3
"""Side-by-side Kalshi vs Polymarket on a search term. Manual pairing.

Auto title-matching failed: containment scoring matched
"Dynamo Kyiv score over 3.5 goals" to "Dynamo Kyiv Exact Score 0".
Same game, different question. No cheap text metric separates those.

So: you pick the term, this prints both venues' live markets with asks
and depth, and you eyeball the true pairs. Then --pair to price one.

usage:
    python xvenue_compare.py --term fed
    python xvenue_compare.py --term "temperature london"
    python xvenue_compare.py --term bitcoin --min-vol 1000
    python xvenue_compare.py --list-categories
"""
import argparse, json, re, time
from collections import Counter
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


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


def kalshi_markets(pages=30):
    out, cursor = [], None
    for _ in range(pages):
        params = {"limit": 1000, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{KALSHI}/markets", params=params, timeout=30)
        if r.status_code != 200:
            break
        j = r.json()
        ms = j.get("markets") or []
        for m in ms:
            if "MVE" in str(m.get("ticker") or ""):
                continue
            ya = f(m.get("yes_ask_dollars"))
            na = f(m.get("no_ask_dollars"))
            if not (usable(ya) or usable(na)):
                continue
            out.append({
                "venue": "KALSHI",
                "ticker": str(m.get("ticker") or ""),
                "title": str(m.get("title") or ""),
                "yes_ask": ya, "no_ask": na,
                "yes_sz": f(m.get("yes_ask_size_fp"), 0.0),
                "no_sz": f(m.get("no_ask_size_fp"), 0.0),
                "vol": f(m.get("volume_fp"), 0.0),
            })
        cursor = j.get("cursor")
        if not cursor or not ms:
            break
        time.sleep(0.2)
    return out


def poly_markets(pages=30):
    out, offset = [], 0
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
        for e in batch:
            for m in (e.get("markets") or []):
                if m.get("closed") or m.get("acceptingOrders") is False:
                    continue
                ask = f(m.get("bestAsk"))
                bid = f(m.get("bestBid"))
                if not usable(ask):
                    continue
                out.append({
                    "venue": "POLY",
                    "ticker": str(e.get("slug") or "")[:34],
                    "title": f"{e.get('title')} | {m.get('groupItemTitle') or m.get('question')}",
                    "yes_ask": ask,
                    "no_ask": (1.0 - bid) if usable(bid) else None,
                    "yes_sz": None, "no_sz": None,
                    "vol": f(e.get("volume24hr"), 0.0),
                })
        offset += 100
        time.sleep(0.15)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--term", default="")
    p.add_argument("--pages", type=int, default=30)
    p.add_argument("--min-vol", type=float, default=0.0)
    p.add_argument("--show", type=int, default=40)
    p.add_argument("--list-categories", action="store_true")
    p.add_argument("--out", default="xvenue_side_by_side.csv")
    a = p.parse_args()

    print("pulling both venues...")
    K = kalshi_markets(a.pages)
    P = poly_markets(a.pages)
    print(f"  kalshi {len(K)} | poly {len(P)}")

    if a.list_categories:
        def head_tokens(rows, n=30):
            c = Counter()
            for r in rows:
                for t in re.sub(r"[^a-z ]", " ", r["title"].lower()).split():
                    if len(t) > 3:
                        c[t] += 1
            return c.most_common(n)
        print("\ntop KALSHI title words:")
        print(head_tokens(K))
        print("\ntop POLY title words:")
        print(head_tokens(P))
        print("\npick a --term from words that appear on BOTH lists")
        return

    if not a.term:
        print("pass --term, or --list-categories to find one")
        return

    words = [w.lower() for w in a.term.split()]

    def hit(r):
        t = r["title"].lower()
        return all(w in t for w in words) and r["vol"] >= a.min_vol

    rows = [r for r in K + P if hit(r)]
    if not rows:
        print(f"\nno live markets on either venue match all of {words}")
        return

    R = pd.DataFrame(rows).sort_values(["venue", "vol"], ascending=[True, False])
    R.to_csv(a.out, index=False)
    with pd.option_context("display.width", 260, "display.max_colwidth", 74):
        for v in ("KALSHI", "POLY"):
            s = R[R.venue == v]
            print(f"\n=== {v}: {len(s)} matches ===")
            if len(s):
                print(s[["title", "yes_ask", "no_ask", "yes_sz", "no_sz", "vol"]]
                      .head(a.show).to_string(index=False))
    print(f"\nsaved -> {a.out}")
    print("\nfind a row on each side that resolves IDENTICALLY, then:")
    print("  cost = poly.yes_ask + kalshi.no_ask   (or kalshi.yes_ask + poly.no_ask)")
    print("  profit exists if cost < 1.00 minus fees")
    print("  size is capped by kalshi yes_sz/no_sz")


if __name__ == "__main__":
    main()
