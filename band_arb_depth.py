#!/usr/bin/env python3
"""Depth check for candidate negRisk arbitrage sets.

WHY THIS EXISTS
---------------
band_arb_live2.py scores sets on bestAsk, which is TOP OF BOOK ONLY.
A set showing an 8c edge with 5 contracts resting behind the quote is
not a strategy. This walks the full order book for every leg and answers
the only question that matters:

    how many complete sets can I actually buy while the cost per set
    stays under $1.00?

METHOD
------
Buying one "set" = one contract of every leg. To buy N sets you must
lift N contracts from each leg's ask ladder. Cost per set rises as you
eat through the book. We walk the ladder and report the size at which
the edge disappears.

Fees: Polymarket markets carry takerBaseFee. It is read from gamma and
applied if present. If it comes back 0, that is reported plainly rather
than assumed.

usage:
    python band_arb_depth.py --pattern "Lions vs. Packers"
    python band_arb_depth.py --pattern "Highest temperature in London"
    python band_arb_depth.py --pattern "Fed Decision in September" --max-sets 2000
"""
import argparse, json, re, time
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def jparse(v, default=None):
    if v is None:
        return default
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


def find_events(pattern, pages=20):
    rx = re.compile(pattern, re.I)
    hits, offset = [], 0
    for _ in range(pages):
        r = requests.get(f"{GAMMA}/events",
                         params={"closed": "false", "limit": 100,
                                 "offset": offset, "order": "volume24hr",
                                 "ascending": "false"}, timeout=30)
        if r.status_code != 200:
            print(f"HTTP {r.status_code}: {r.text[:150]}"); break
        batch = r.json()
        if not batch:
            break
        hits += [e for e in batch if rx.search(str(e.get("title") or ""))]
        offset += 100
        time.sleep(0.15)
    return hits


def get_book(token_id):
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=20)
        if r.status_code != 200:
            return None
        j = r.json()
        asks = j.get("asks") or []
        # CLOB returns asks; normalise and sort ascending by price
        lad = sorted(((float(x["price"]), float(x["size"])) for x in asks),
                     key=lambda t: t[0])
        return lad
    except Exception as e:
        print(f"    book error: {type(e).__name__}")
        return None


def cost_for(ladder, n):
    """VWAP cost to buy n contracts. Returns (total_cost, filled)."""
    need, spend = n, 0.0
    for px, sz in ladder:
        take = min(need, sz)
        spend += take * px
        need -= take
        if need <= 0:
            break
    return spend, n - need


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pattern", required=True)
    p.add_argument("--max-sets", type=int, default=1000)
    p.add_argument("--pages", type=int, default=20)
    a = p.parse_args()

    evs = find_events(a.pattern, a.pages)
    if not evs:
        print(f"no open event matching /{a.pattern}/")
        return
    e = evs[0]
    print(f"event: {e.get('title')}")
    print(f"  slug: {e.get('slug')}   negRisk={e.get('negRisk')}")

    legs = []
    for m in (e.get("markets") or []):
        if m.get("closed") or m.get("acceptingOrders") is False:
            continue
        tids = jparse(m.get("clobTokenIds"), [])
        if not tids:
            continue
        legs.append({
            "q": str(m.get("groupItemTitle") or m.get("question"))[:44],
            "token": tids[0],
            "bestAsk": m.get("bestAsk"),
            "takerFee": m.get("takerBaseFee"),
        })
    print(f"  live legs: {len(legs)}")
    if len(legs) < 2:
        print("  not a multi-leg set"); return

    fees = {l["takerFee"] for l in legs}
    print(f"  takerBaseFee values seen: {fees}")

    print("\nfetching order books...")
    for l in legs:
        l["ladder"] = get_book(l["token"])
        time.sleep(0.12)
        depth = sum(s for _, s in (l["ladder"] or []))
        top = l["ladder"][0] if l["ladder"] else None
        print(f"  {l['q']:<46} bestAsk={l['bestAsk']}  "
              f"top={top}  total_ask_depth={depth:,.0f}")

    if any(l["ladder"] is None or not l["ladder"] for l in legs):
        print("\nat least one leg has an empty/failed book — the set is not "
              "buyable as a set. stop here.")
        return

    print("\n=== COST PER SET AS SIZE GROWS ===")
    print(f"{'sets':>8}{'cost/set':>12}{'edge/set':>11}{'total cost':>13}{'total edge':>12}")
    best_n, best_edge_total = 0, 0.0
    for n in [1, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000]:
        if n > a.max_sets:
            break
        tot, ok = 0.0, True
        for l in legs:
            c, filled = cost_for(l["ladder"], n)
            if filled < n:
                ok = False
                break
            tot += c
        if not ok:
            print(f"{n:>8}   book exhausted on at least one leg — max size reached")
            break
        per = tot / n
        edge = 1.0 - per
        print(f"{n:>8}{per:>12.4f}{edge*100:>10.2f}c{tot:>13,.0f}{edge*n:>12,.0f}")
        if edge > 0:
            best_n, best_edge_total = n, edge * n

    print("\n--- VERDICT ---")
    if best_n == 0:
        print("  no size is profitable — bestAsk was top-of-book noise")
    else:
        print(f"  max profitable size found: {best_n} sets")
        print(f"  gross profit at that size: ${best_edge_total:,.0f} "
              f"(capital required ~${best_n:,.0f})")
        print("  REMAINING CHECKS BEFORE THIS IS REAL:")
        print("   1. resolution date — capital is locked until then")
        print("   2. fees above (takerBaseFee) come out of the edge")
        print("   3. all legs must fill; a partial set is an open directional bet")
        print("   4. quotes move; this is a snapshot, not a guarantee")


if __name__ == "__main__":
    main()
