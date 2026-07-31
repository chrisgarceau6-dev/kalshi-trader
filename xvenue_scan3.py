#!/usr/bin/env python3
"""Cross-venue divergence scanner v3.

v2 problems fixed:
  * Kalshi: 25000 raw -> 109 kept. The filter required BOTH yes_ask and
    no_ask strictly inside (0,1); a no_ask of exactly 1.0000 is normal
    and was killing most of the book. Now keeps a market if EITHER side
    has a usable ask.
  * Polymarket: HTTP 422 partway through pagination (offset ceiling).
    Now stops cleanly on 422 instead of burning pages.
  * Matching: Jaccard >= 0.4 across differently-phrased titles matched
    nothing. Now scores by containment (intersection / min set size) and
    always prints the best matches found, so you can see whether the join
    works before any edge filter is applied.

usage:
    python xvenue_scan3.py                 # top matches + any edge
    python xvenue_scan3.py --show 40
    python xvenue_scan3.py --min-edge 0.01 --min-size 50
"""
import argparse, json, re, time
from collections import defaultdict
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

STOP = set("""the a an of in on at to for and or is are will be by with what
who how many much next than more less over under between be up down""".split())


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def toks(s):
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    return {t for t in s.split() if t and t not in STOP and len(t) > 2}


def jparse(v, d=None):
    if v is None:
        return d
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return d


def usable(x):
    return x is not None and 0.0 < x < 1.0


def fetch_kalshi(pages=30):
    out, cursor, raw, parlay = [], None, 0, 0
    for _ in range(pages):
        params = {"limit": 1000, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{KALSHI}/markets", params=params, timeout=30)
        except Exception as e:
            print(f"  kalshi net error: {type(e).__name__}"); break
        if r.status_code != 200:
            print(f"  kalshi HTTP {r.status_code}"); break
        j = r.json()
        ms = j.get("markets") or []
        raw += len(ms)
        for m in ms:
            if "MVE" in str(m.get("ticker") or ""):
                parlay += 1
                continue
            ya = f(m.get("yes_ask_dollars"))
            na = f(m.get("no_ask_dollars"))
            if not (usable(ya) or usable(na)):
                continue
            out.append({
                "ticker": m.get("ticker"),
                "title": f"{m.get('title')} {m.get('subtitle') or ''}".strip(),
                "yes_ask": ya, "no_ask": na,
                "yes_size": f(m.get("yes_ask_size_fp"), 0.0),
                "no_size": f(m.get("no_ask_size_fp"), 0.0),
                "vol": f(m.get("volume_fp"), 0.0),
            })
        cursor = j.get("cursor")
        if not cursor or not ms:
            break
        time.sleep(0.2)
    print(f"  kalshi: {raw} raw, {parlay} parlays dropped, {len(out)} usable")
    return out


def fetch_poly(pages=30):
    out, offset = [], 0
    for _ in range(pages):
        try:
            r = requests.get(f"{GAMMA}/events",
                             params={"closed": "false", "limit": 100,
                                     "offset": offset, "order": "volume24hr",
                                     "ascending": "false"}, timeout=30)
        except Exception as e:
            print(f"  poly net error: {type(e).__name__}"); break
        if r.status_code == 422:
            break
        if r.status_code != 200:
            print(f"  poly HTTP {r.status_code}"); break
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
                    "title": str(m.get("groupItemTitle") or m.get("question")),
                    "event": str(e.get("title")),
                    "yes_ask": ask,
                    "no_ask": (1.0 - bid) if usable(bid) else None,
                    "liq": f(e.get("liquidity"), 0.0),
                })
        offset += 100
        time.sleep(0.15)
    print(f"  poly: {len(out)} usable")
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=30)
    p.add_argument("--min-edge", type=float, default=-99.0)
    p.add_argument("--min-score", type=float, default=0.5)
    p.add_argument("--min-size", type=float, default=0.0)
    p.add_argument("--show", type=int, default=30)
    p.add_argument("--out", default="xvenue_candidates.csv")
    a = p.parse_args()

    print("pulling Kalshi...")
    K = fetch_kalshi(a.pages)
    print("pulling Polymarket...")
    P = fetch_poly(a.pages)
    if not K or not P:
        print("a venue returned nothing"); return

    idx = defaultdict(set)
    kt = []
    for m in K:
        t = toks(m["title"])
        kt.append((m, t))
        for tk in t:
            idx[tk].add(len(kt) - 1)

    rows = []
    for pm in P:
        pt = toks(pm["event"] + " " + pm["title"])
        if len(pt) < 2:
            continue
        cand = defaultdict(int)
        for tk in pt:
            for i in idx.get(tk, ()):
                cand[i] += 1
        for i, c in cand.items():
            km, ktok = kt[i]
            if not ktok:
                continue
            score = c / min(len(pt), len(ktok))
            if score < a.min_score:
                continue
            combos = []
            if usable(pm["yes_ask"]) and usable(km["no_ask"]):
                combos.append(("polyYES+kalNO", pm["yes_ask"] + km["no_ask"],
                               km["no_size"]))
            if usable(km["yes_ask"]) and usable(pm["no_ask"]):
                combos.append(("kalYES+polyNO", km["yes_ask"] + pm["no_ask"],
                               km["yes_size"]))
            if not combos:
                continue
            leg, cost, ksz = min(combos, key=lambda t: t[1])
            edge = 1.0 - cost
            if edge < a.min_edge or ksz < a.min_size:
                continue
            rows.append({
                "score": round(score, 2),
                "edge": round(edge, 4),
                "leg": leg,
                "k_size": round(ksz, 1),
                "max_$": round(max(edge, 0) * ksz, 2),
                "kalshi": str(km["title"])[:46],
                "poly": (pm["event"] + " | " + pm["title"])[:56],
            })

    if not rows:
        print("\nzero title matches at all. lower --min-score 0.3")
        return

    R = pd.DataFrame(rows).sort_values(["edge", "score"], ascending=False)
    R.to_csv(a.out, index=False)
    with pd.option_context("display.width", 260, "display.max_colwidth", 58):
        print(f"\n=== {len(R)} matched pairs (sorted by edge) ===")
        print(R.head(a.show).to_string(index=False))
        print(f"\n--- best title matches regardless of edge ---")
        print(R.sort_values("score", ascending=False)
              .head(15).to_string(index=False))
    pos = R[R.edge > 0]
    print(f"\npairs with positive edge: {len(pos)} of {len(R)}")
    print(f"saved -> {a.out}")


if __name__ == "__main__":
    main()
