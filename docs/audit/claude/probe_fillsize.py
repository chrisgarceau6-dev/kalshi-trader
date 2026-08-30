#!/usr/bin/env python3
"""Probe: does a crash fill "silently UPSIZE" the position?

CLAUDE.md (z-gate row, Aug 29) records: "it filled 37 contracts for $31.45 against a
$25 flat bet - 26% over ... a cheaper fill buys more contracts, so a crash fill
silently UPSIZES exactly when the book is disorderly. Pre-existing, unrelated to the
z-gate, and not currently controlled."

This scores that claim against every settlement, using the settlements API as the
source of truth for what was actually paid.

The trap this probe exists to avoid: FLAT_BET_DOLLARS moved SIX times in this window
(~$36 -> $45.6 -> $73.5 -> $48.8 -> $24 -> $34), and a deploy lands MID-DAY, so a day
spans two sizes. Normalising against a remembered bet size manufactures a large fake
"oversized" cohort - which is exactly how this claim was first mis-scored. Normalise
against the day's own median settlement cost, then check whether the outliers are
bimodal on deploy days.

  python3 docs/audit/claude/probe_fillsize.py
"""
import os, re, sys, json, datetime as D, statistics as S
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from kalshi_auth import get as kget

ET = D.timezone(D.timedelta(hours=-4))
LC = {"KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M", "KXXRP15M"}


def _ts(t):
    # Kalshi returns variable-width fractional seconds; fromisoformat wants exactly 6.
    t = t.replace("Z", "+00:00")
    t = re.sub(r"\.(\d{6})\d*", r".\1", t)
    t = re.sub(r"\.(\d{1,5})\+", lambda m: "." + m.group(1).ljust(6, "0") + "+", t)
    return D.datetime.fromisoformat(t)


def settlements():
    rows, cursor, pages = [], None, 0
    while pages < 60:
        p = {"limit": 200}
        if cursor:
            p["cursor"] = cursor
        code, r = kget("/portfolio/settlements", p)
        if code != 200:
            sys.exit(f"settlements HTTP {code}")
        batch = r.get("settlements", [])
        if not batch:
            break
        rows += batch
        pages += 1
        cursor = r.get("cursor")
        if not cursor:
            break
    out = []
    for s in rows:
        if s.get("ticker", "").split("-")[0] not in LC:
            continue
        cost = (float(s.get("yes_total_cost_dollars", 0) or 0)
                + float(s.get("no_total_cost_dollars", 0) or 0))
        if cost <= 0.001:
            continue
        con = (float(s.get("yes_count_fp", 0) or 0)
               + float(s.get("no_count_fp", 0) or 0))
        rev = int(s.get("revenue", 0)) / 100.0
        fee = float(s.get("fee_cost", 0) or 0)
        out.append(dict(day=_ts(s["settled_time"]).astimezone(ET).date().isoformat(),
                        cost=cost, con=con, fee=fee, pnl=rev - cost - fee,
                        won=rev > 0.01))
    return out


def main():
    T = settlements()
    byday = defaultdict(list)
    for r in T:
        byday[r["day"]].append(r)
    med = {d: S.median([x["cost"] for x in byday[d]]) for d in byday}

    print(f"{len(T)} settlements, {min(byday)}..{max(byday)}\n")
    print("Effective bet size per day (median settlement cost) — NOT a constant:")
    for d in sorted(byday):
        print(f"  {d}  ${med[d]:6.2f}  n={len(byday[d])}")

    # Post-broken-build only: Jul 27-Aug 3 was the broken build at erratic sizes.
    A = [r for r in T if r["day"] >= "2026-08-04"]
    for r in A:
        r["ratio"] = r["cost"] / med[r["day"]]

    print("\nSettlement cost / that day's median cost (Aug 4-30):")
    print(f"  {'bucket':<22}{'n':>6}{'share':>8}{'P&L':>10}{'WR':>8}{'ROC':>8}")
    for lo, hi, lbl in [(0, .6, "<0.6x partial"), (.6, 1.15, "0.6-1.15x normal"),
                        (1.15, 1.6, "1.15-1.6x"), (1.6, 2.4, "1.6-2.4x ~double"),
                        (2.4, 99, ">2.4x")]:
        R = [r for r in A if lo <= r["ratio"] < hi]
        if not R:
            print(f"  {lbl:<22}{0:>6}")
            continue
        print(f"  {lbl:<22}{len(R):>6}{100*len(R)/len(A):>7.1f}%"
              f"{sum(x['pnl'] for x in R):>10.2f}"
              f"{100*sum(x['won'] for x in R)/len(R):>7.1f}%"
              f"{100*sum(x['pnl'] for x in R)/sum(x['cost'] for x in R):>7.2f}%")

    print("\nThe >=1.6x cohort, by day — if these are deploy days, they are a "
          "size CHANGE, not an overshoot:")
    d = defaultdict(list)
    for r in [x for x in A if x["ratio"] >= 1.6]:
        d[r["day"]].append(r)
    for k in sorted(d):
        c = sorted(x["cost"] for x in byday[k])
        cut = med[k] * 1.35
        lo = [x for x in c if x < cut]
        hi = [x for x in c if x >= cut]
        print(f"  {k}  n={len(d[k]):<3} P&L {sum(x['pnl'] for x in d[k]):+8.2f}   "
              f"day splits {len(lo)} @ ~${S.median(lo):.2f} + {len(hi)} @ ~${S.median(hi):.2f}"
              f"  {'<- BIMODAL: mid-day deploy' if len(lo) >= 3 and len(hi) >= 3 else ''}")

    print("\nThe specific fill CLAUDE.md flags (Aug 29, 37 contracts, $31.45):")
    for x in T:
        if x["day"] == "2026-08-29" and abs(x["cost"] - 31.45) < 0.01:
            print(f"  cost ${x['cost']:.2f}  {x['con']:.0f} con @ "
                  f"{100*x['cost']/x['con']:.2f}c  pnl {x['pnl']:+.2f}")
            print(f"  that day's median cost: ${med['2026-08-29']:.2f}  -> ratio "
                  f"{x['cost']/med['2026-08-29']:.2f}x  (BELOW the day's typical size)")


if __name__ == "__main__":
    main()
