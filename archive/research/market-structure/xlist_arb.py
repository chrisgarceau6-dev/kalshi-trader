#!/usr/bin/env python3
"""Cross-listing parity scanner: range bands vs threshold ladder.

Identity, for a band [A, B):      Band(A,B) = Above(A) - Above(B)

Portfolio 1 (band cheap) -- pays exactly $1 in every state:
    BUY Band YES  +  BUY Above(B) YES  +  BUY Above(A) NO
Portfolio 2 (band rich) -- pays exactly $2 in every state:
    BUY Band NO   +  BUY Above(A) YES  +  BUY Above(B) NO

Arb if total cost + exact taker fees + execution reserve < guaranteed payout.
Read-only. Places nothing.
"""
import sys
from decimal import Decimal, ROUND_CEILING

import kalshi_auth as ka

CONTRACTS = 10
RESERVE_PER_LEG = Decimal("0.01")   # execution reserve, per contract per leg
EPS = Decimal("0.005")


def fee(price, n=CONTRACTS):
    raw = Decimal("0.07") * Decimal(n) * price * (Decimal("1") - price)
    return raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def D(v):
    try:
        d = Decimal(str(v))
        return d if 0 < d < 1 else None
    except Exception:
        return None


def depth(ticker, side, limit_price):
    """Contracts available for `side` at or below limit_price."""
    code, r = ka.get(f"/markets/{ticker}/orderbook", {})
    if code != 200 or not r:
        return None
    ob = r.get("orderbook_fp") or {}
    opp = ob.get("no_dollars" if side == "yes" else "yes_dollars", []) or []
    need = Decimal("1") - limit_price
    tot = Decimal("0")
    for lvl in opp:
        try:
            p, q = Decimal(str(lvl[0])), Decimal(str(lvl[1]))
            if p >= need:
                tot += q
        except Exception:
            continue
    return tot


def markets(series, event):
    out, cur = [], None
    while True:
        p = {"series_ticker": series, "event_ticker": event, "limit": 200}
        if cur:
            p["cursor"] = cur
        code, r = ka.get("/markets", p)
        if code != 200:
            break
        out += r.get("markets", [])
        cur = r.get("cursor")
        if not cur:
            break
    return out


def scan(asset="BTC", max_events=4, verbose=False):
    rng_s, thr_s = f"KX{asset}", f"KX{asset}D"
    code, r = ka.get("/markets", {"series_ticker": rng_s, "status": "open", "limit": 200})
    events = sorted({m["event_ticker"] for m in r.get("markets", [])})[:max_events]
    if not events:
        print(f"  {asset}: no open range events")
        return []

    found = []
    for ev in events:
        bands = markets(rng_s, ev)
        thr_ev = ev.replace(rng_s, thr_s, 1)
        thr = markets(thr_s, thr_ev)
        # threshold ladder keyed by floor strike; prefer KXxxxD, fall back to range series' own T-markets
        above = {}
        for m in thr + bands:
            if m.get("strike_type") == "greater" and m.get("floor_strike") is not None:
                above.setdefault(round(float(m["floor_strike"]), 2), m)
        betweens = [m for m in bands if m.get("strike_type") == "between"]
        if verbose:
            print(f"  {ev}: {len(betweens)} bands, {len(above)} thresholds")

        for b in betweens:
            lo, hi = b.get("floor_strike"), b.get("cap_strike")
            if lo is None or hi is None:
                continue
            aA = above.get(round(float(lo) - 0.01, 2))   # Above(A)
            aB = above.get(round(float(hi), 2))          # Above(B)
            if not aA or not aB:
                continue

            legs1 = [(b, "yes"), (aB, "yes"), (aA, "no")]     # pays $1
            legs2 = [(b, "no"), (aA, "yes"), (aB, "no")]      # pays $2
            for legs, payout, name in ((legs1, 1, "band-cheap"), (legs2, 2, "band-rich")):
                px = [D(m.get(f"{s}_ask_dollars")) for m, s in legs]
                if any(p is None for p in px):
                    continue
                cost = sum(px)
                fees = sum(fee(p) for p in px) / Decimal(CONTRACTS)
                reserve = RESERVE_PER_LEG * len(legs)
                edge = Decimal(payout) - cost - fees - reserve
                if edge > EPS:
                    dep = [depth(m["ticker"], s, p) for (m, s), p in zip(legs, px)]
                    ok = all(d is not None and d >= CONTRACTS for d in dep)
                    found.append({
                        "event": ev, "band": b["ticker"], "type": name,
                        "edge_per_contract": float(edge),
                        "total": float(edge) * CONTRACTS,
                        "cost": float(cost), "fees": float(fees),
                        "depth_ok": ok, "depth": [float(d) if d is not None else None for d in dep],
                        "legs": [f"{m['ticker']}:{s}@{p}" for (m, s), p in zip(legs, px)],
                    })
    return found


if __name__ == "__main__":
    assets = sys.argv[1:] or ["BTC", "ETH"]
    allf = []
    for a in assets:
        print(f"=== {a} ===")
        f = scan(a, verbose=True)
        allf += f
        print(f"  candidates: {len(f)}")
    print()
    if not allf:
        print("NO ARBITRAGE FOUND (after fees + 1c/leg reserve, 10 contracts)")
    else:
        for x in sorted(allf, key=lambda z: -z["total"]):
            flag = "EXECUTABLE" if x["depth_ok"] else "thin book"
            print(f"[{flag}] {x['type']:11} {x['band']}  +${x['total']:.2f}/10ct  "
                  f"cost={x['cost']:.4f} fees={x['fees']:.4f} depth={x['depth']}")
            for l in x["legs"]:
                print(f"      {l}")
