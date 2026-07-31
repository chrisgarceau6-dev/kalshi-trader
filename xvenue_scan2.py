#!/usr/bin/env python3
"""Cross-venue divergence scanner v2 — Kalshi vs Polymarket.

SCHEMA FIXES FROM v1 (found by probing, not guessing)
-----------------------------------------------------
* Kalshi price fields are `yes_ask_dollars` / `no_ask_dollars` etc, and
  they are STRINGS already denominated in dollars. v1 looked for
  `yes_ask` and divided by 100 — both wrong, so every pair silently
  dropped.
* `yes_ask_size_fp` / `no_ask_size_fp` give TOP-OF-BOOK DEPTH right in
  the market list. This is the number that killed the Polymarket band
  arb (an 8c edge behind 9.51 contracts), so it is reported up front
  instead of discovered later.
* KXMVE* tickers are multi-leg parlays with comma-jammed titles and zero
  size. They are filtered out or they pollute every title match.
* A quote of 0.0000 with size 0.00 is an empty book, not a free option.

THE TRADE
---------
Same event on both venues. Buy YES on the cheap venue and NO on the
other; the pair pays $1.00 regardless of outcome. If the two asks sum
under $1.00 minus fees, that is locked profit with no forecasting.

Capacity is the whole question, and here it is bounded by the THINNER
book — but at a $2k stack you only need a couple hundred dollars of
depth, which is why small size stops being a handicap on this one.

usage:
    python xvenue_scan2.py
    python xvenue_scan2.py --min-edge -1 --min-overlap 0.4   # see raw matches
    python xvenue_scan2.py --min-edge 0.02 --min-size 50
"""
import argparse, json, re, time
from collections import defaultdict
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

STOP = set("""the a an of in on at to for and or is are will be by with what
who how many much next than more less over under between""".split())


def f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


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


def is_parlay(m):
    tk = str(m.get("ticker") or "")
    if "MVE" in tk:
        return True
    if m.get("mve_collection_ticker") or m.get("mve_selected_legs"):
        return True
    if str(m.get("title") or "").count(",") >= 3:
        return True
    return False


def fetch_kalshi(pages=25):
    out, cursor, seen = [], None, 0
    for _ in range(pages):
        params = {"limit": 1000, "status": "open"}
        if cursor:
            params["cursor"] = cursor
        try:
            r = requests.get(f"{KALSHI}/markets", params=params, timeout=30)
        except Exception as e:
            print(f"  kalshi net error: {type(e).__name__}"); break
        if r.status_code != 200:
            print(f"  kalshi HTTP {r.status_code}: {r.text[:200]}"); break
        j = r.json()
        ms = j.get("markets") or []
        seen += len(ms)
        for m in ms:
            if is_parlay(m):
                continue
            ya = f(m.get("yes_ask_dollars"))
            na = f(m.get("no_ask_dollars"))
            ys = f(m.get("yes_ask_size_fp"), 0.0)
            ns = f(m.get("no_ask_size_fp"), 0.0)
            if ya is None or na is None:
                continue
            if ya <= 0 or ya >= 1 or na <= 0 or na >= 1:
                continue          # empty/degenerate book
            if ys <= 0 and ns <= 0:
                continue
            out.append({
                "ticker": m.get("ticker"),
                "title": m.get("title"),
                "yes_ask": ya, "no_ask": na,
                "yes_size": ys, "no_size": ns,
                "vol": f(m.get("volume_fp"), 0.0),
                "liq": f(m.get("liquidity_dollars"), 0.0),
            })
        cursor = j.get("cursor")
        if not cursor or not ms:
            break
        time.sleep(0.2)
    print(f"  {seen} raw -> {len(out)} quotable non-parlay markets")
    return out


def fetch_poly(pages=25):
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
                ask = f(m.get("bestAsk"))
                bid = f(m.get("bestBid"))
                if ask is None or ask <= 0 or ask >= 1:
                    continue
                out.append({
                    "title": str(m.get("groupItemTitle") or m.get("question")),
                    "event": str(e.get("title")),
                    "yes_ask": ask,
                    "no_ask": (1.0 - bid) if bid is not None else None,
                    "liq": f(e.get("liquidity"), 0.0),
                    "tokens": jparse(m.get("clobTokenIds"), []),
                })
        offset += 100
        time.sleep(0.15)
    print(f"  {len(out)} quotable polymarket markets")
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pages", type=int, default=25)
    p.add_argument("--min-edge", type=float, default=0.0)
    p.add_argument("--min-overlap", type=float, default=0.55)
    p.add_argument("--min-size", type=float, default=0.0,
                   help="require this much kalshi top-of-book size")
    p.add_argument("--out", default="xvenue_candidates.csv")
    a = p.parse_args()

    print("pulling Kalshi...")
    K = fetch_kalshi(a.pages)
    print("pulling Polymarket...")
    P = fetch_poly(a.pages)
    if not K or not P:
        print("a venue returned nothing"); return

    idx = defaultdict(list)
    kt = []
    for m in K:
        t = norm_tokens(m["title"])
        kt.append((m, t))
        for tok in t:
            idx[tok].append(len(kt) - 1)

    rows = []
    for pm in P:
        pt = norm_tokens(pm["title"] + " " + pm["event"])
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
            combos = []
            if pm["yes_ask"] is not None and km["no_ask"] is not None:
                combos.append(("polyYES+kalshiNO",
                               pm["yes_ask"] + km["no_ask"], km["no_size"]))
            if km["yes_ask"] is not None and pm["no_ask"] is not None:
                combos.append(("kalshiYES+polyNO",
                               km["yes_ask"] + pm["no_ask"], km["yes_size"]))
            if not combos:
                continue
            leg, cost, ksize = min(combos, key=lambda t: t[1])
            edge = 1.0 - cost
            if edge <= a.min_edge or ksize < a.min_size:
                continue
            rows.append({
                "edge": round(edge, 4),
                "ovl": round(j, 2),
                "leg": leg,
                "k_size": round(ksize, 1),
                "max_$": round(edge * ksize, 2),
                "kalshi": str(km["title"])[:40],
                "poly": (pm["event"] + " | " + pm["title"])[:52],
                "k_vol": round(km["vol"]),
                "p_liq": round(pm["liq"]),
            })

    if not rows:
        print("\nno pairs cleared the thresholds.")
        print("run:  python xvenue_scan2.py --min-edge -1 --min-overlap 0.4")
        print("  garbage matches  -> the title join needs work")
        print("  good matches, no edge -> the venues are already linked")
        return

    R = pd.DataFrame(rows).sort_values("edge", ascending=False)
    R.to_csv(a.out, index=False)
    with pd.option_context("display.width", 250, "display.max_colwidth", 54):
        print(f"\n=== {len(R)} candidate pairs ===")
        print(R.head(30).to_string(index=False))
    print(f"\nsaved -> {a.out}")
    print("\nmax_$ is edge x kalshi top-of-book size — the ceiling on one shot")
    print("before the quote moves. If that column is all pennies, this is the")
    print("band arb again with extra steps.")
    print("\nBEFORE TRADING ANY ROW:")
    print("  1. read both titles — same date, same source, same tie rules?")
    print("  2. verify the Polymarket side on the live CLOB book; gamma's")
    print("     bestAsk was already proven stale")
    print("  3. fees: kalshi ~0.07*p*(1-p), poly 0.05*p*(1-p), both come out")


if __name__ == "__main__":
    main()
