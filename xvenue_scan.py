#!/usr/bin/env python3
"""Cross-venue divergence scanner — Kalshi vs Polymarket.

THE TRADE
---------
Same event, two venues, different prices. Buy the cheap side on one,
buy the opposing side on the other, and the pair pays $1.00 no matter
what happens. If the two asks sum to less than $1.00 you have locked a
profit with no forecasting and no directional risk.

WHY THIS ONE IS DIFFERENT FROM EVERYTHING ELSE TRIED
----------------------------------------------------
Capacity is the thing that killed the single-venue band arb: the edge
existed on ~10 contracts. Here the constraint is the THINNER of the two
books, but you only need a couple hundred dollars of depth to matter at
a $2k stack. Small size stops being a handicap.

WHAT THIS SCRIPT DOES / DOESN'T DO
----------------------------------
DOES: pull open markets from both venues, match them by title tokens,
and report every pair whose combined ask is under $1.00.
DOESN'T: trust those numbers. Quoted asks were already proven stale on
Polymarket's gamma feed, so anything this flags is a CANDIDATE to verify
against the live order book, not a signal to trade.

usage:
    python xvenue_scan.py --probe          # check both endpoints first
    python xvenue_scan.py
    python xvenue_scan.py --min-edge 0.01 --pages 30
    python xvenue_scan.py --kalshi-base https://api.elections.kalshi.com/trade-api/v2
"""
import argparse, json, re, time
from collections import defaultdict
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
KALSHI_DEFAULT = "https://api.elections.kalshi.com/trade-api/v2"

STOP = set("""the a an of in on at to for and or is are will be by with what
who how many much next 2025 2026 2027 vs versus"""

.split())


def norm_tokens(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    return {t for t in s.split() if t and t not in STOP and len(t) > 2}


def jparse(v, default=None):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def fetch_kalshi(base, pages=20):
    out, cursor = [], None
    for _ in range(pages):
        params = {"limit": 1000, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{base}/markets", params=params, timeout=30)
        except Exception as e:
            print(f"  kalshi net error: {type(e).__name__}"); break
        if r.status_code != 200:
            print(f"  kalshi HTTP {r.status_code}: {r.text[:200]}")
            print("  --> fix with --kalshi-base (grep your main script for the URL)")
            break
        j = r.json()
        ms = j.get("markets") or []
        out.extend(ms)
        cursor = j.get("cursor")
        if not cursor or not ms:
            break
        time.sleep(0.2)
    return out


def fetch_poly(pages=20):
    out, offset = [], 0
    for _ in range(pages):
        try:
            r = requests.get(f"{GAMMA}/events",
                             params={"closed": "false", "limit": 100,
                                     "offset": offset, "order": "volume24hr",
                                     "ascending": "false"}, timeout=30)
        except Exception as e:
            print(f"  poly net error: {type(e).__name__}"); break
        if r.status_code != 200:
            print(f"  poly HTTP {r.status_code}"); break
        batch = r.json()
        if not batch:
            break
        for e in batch:
            for m in (e.get("markets") or []):
                if m.get("closed") or m.get("acceptingOrders") is False:
                    continue
                out.append({
                    "title": f"{e.get('title')} | {m.get('groupItemTitle') or m.get('question')}",
                    "question": m.get("question"),
                    "yes_ask": m.get("bestAsk"),
                    "yes_bid": m.get("bestBid"),
                    "liq": e.get("liquidity"),
                    "vol24": e.get("volume24hr"),
                    "tokens": jparse(m.get("clobTokenIds"), []),
                })
        offset += 100
        time.sleep(0.15)
    return out


def probe(base):
    print("=== KALSHI PROBE ===")
    ms = fetch_kalshi(base, pages=1)
    print(f"  markets returned: {len(ms)}")
    if ms:
        m = ms[0]
        print(f"  keys: {sorted(m.keys())[:18]}")
        for k in ("ticker", "title", "yes_bid", "yes_ask", "no_bid", "no_ask",
                  "volume", "liquidity", "status"):
            print(f"    {k}: {m.get(k)}")
    print("\n=== POLYMARKET PROBE ===")
    ps = fetch_poly(pages=1)
    print(f"  live markets returned: {len(ps)}")
    if ps:
        print(f"    {ps[0]}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--kalshi-base", default=KALSHI_DEFAULT)
    p.add_argument("--pages", type=int, default=20)
    p.add_argument("--min-edge", type=float, default=0.0,
                   help="report pairs where 1.00 - (askA + askB) exceeds this")
    p.add_argument("--min-overlap", type=float, default=0.55)
    p.add_argument("--probe", action="store_true")
    p.add_argument("--out", default="xvenue_candidates.csv")
    a = p.parse_args()

    if a.probe:
        probe(a.kalshi_base); return

    print("pulling Kalshi open markets...")
    K = fetch_kalshi(a.kalshi_base, a.pages)
    print(f"  {len(K)} kalshi markets")
    print("pulling Polymarket live markets...")
    P = fetch_poly(a.pages)
    print(f"  {len(P)} polymarket markets")
    if not K or not P:
        print("one venue returned nothing — run --probe"); return

    # index kalshi by token for a cheap candidate join
    idx = defaultdict(list)
    kt = []
    for m in K:
        t = norm_tokens(m.get("title"))
        kt.append((m, t))
        for tok in t:
            idx[tok].append(len(kt) - 1)

    rows = []
    for pm in P:
        pt = norm_tokens(pm["title"])
        if not pt:
            continue
        counts = defaultdict(int)
        for tok in pt:
            for i in idx.get(tok, ()):
                counts[i] += 1
        for i, c in counts.items():
            km, ktok = kt[i]
            j = c / len(pt | ktok)
            if j < a.min_overlap:
                continue
            k_yes_ask = km.get("yes_ask")
            k_no_ask = km.get("no_ask")
            p_yes = pm.get("yes_ask")
            if k_yes_ask is None or k_no_ask is None or p_yes is None:
                continue
            # kalshi quotes in cents
            k_yes = float(k_yes_ask) / 100.0
            k_no = float(k_no_ask) / 100.0
            p_yes = float(p_yes)
            p_no = 1.0 - float(pm.get("yes_bid") or 0)  # rough: buying NO on poly
            # two ways to lock the pair
            combo_a = p_yes + k_no      # YES on poly, NO on kalshi
            combo_b = k_yes + p_no      # YES on kalshi, NO on poly
            best = min(combo_a, combo_b)
            edge = 1.0 - best
            if edge <= a.min_edge:
                continue
            rows.append({
                "overlap": round(j, 2),
                "edge": round(edge, 4),
                "leg": "polyYES+kalshiNO" if combo_a <= combo_b else "kalshiYES+polyNO",
                "kalshi": str(km.get("title"))[:44],
                "poly": pm["title"][:52],
                "k_yes": k_yes, "k_no": k_no,
                "p_ask": p_yes,
                "k_vol": km.get("volume"), "p_liq": pm.get("liq"),
            })

    if not rows:
        print("\nno pairs matched above the overlap+edge thresholds.")
        print("try --min-overlap 0.4 --min-edge -1 to see the raw matches first —")
        print("if the MATCHES are garbage, the title join needs work; if the")
        print("matches are good but no edge exists, the venues are linked.")
        return

    R = pd.DataFrame(rows).sort_values("edge", ascending=False)
    R.to_csv(a.out, index=False)
    with pd.option_context("display.width", 240, "display.max_colwidth", 52):
        print(f"\n=== {len(R)} candidate pairs, combined ask under $1.00 ===")
        print(R.head(25).to_string(index=False))
    print(f"\nsaved -> {a.out}")
    print("\nEVERY ROW IS A CANDIDATE, NOT A TRADE:")
    print("  1. read the two titles — do they truly resolve identically?")
    print("     (same date, same source, same tie handling)")
    print("  2. quoted asks were already proven stale on gamma; verify both")
    print("     books live before sizing")
    print("  3. Kalshi fee is ~0.07*p*(1-p), Polymarket 0.05*p*(1-p) — both")
    print("     come out of the edge")


if __name__ == "__main__":
    main()
