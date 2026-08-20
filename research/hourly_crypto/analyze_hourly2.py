#!/usr/bin/env python3
"""Follow-ups: why does stacking never produce a simultaneous loss, and does the
hourly edge survive slippage and an out-of-sample split?"""
import os, sys
from collections import defaultdict
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_hourly import load, qualifying, cfg, BET, HERE
import backtest as B

rows = []
for s in ("KXBTCD", "KXETHD"):
    p = os.path.join(HERE, f"hourly_{s}.csv.gz")
    if os.path.exists(p):
        rows += load(p)
q = qualifying(rows)

print("=" * 80)
print("E. WHAT ARE THE STACKED STRIKES? (the August 'leverage' worry)")
print("=" * 80)
per = defaultdict(list)
for t in q:
    per[(t["cts"], t["series"])].append(t)
multi = {k: v for k, v in per.items() if len(v) > 1}
pat = defaultdict(int)
for k, v in multi.items():
    sides = tuple(sorted(x["side"] for x in v))
    pat[sides] += 1
for sides, n in sorted(pat.items(), key=lambda x: -x[1]):
    print(f"  {str(sides):<34} {n:>4} closes")

straddle = 0
for k, v in multi.items():
    if len(v) == 2 and {x["side"] for x in v} == {"yes", "no"}:
        y = [x for x in v if x["side"] == "yes"][0]
        n = [x for x in v if x["side"] == "no"][0]
        if y["strike"] < n["strike"]:
            straddle += 1
print(f"\n  2-strike closes that are a YES below spot + NO above spot: {straddle}")
print("  Those two CANNOT both lose — spot cannot finish below the lower strike")
print("  and above the upper one. Stacking here is self-limiting, not leverage.")

print("\n" + "=" * 80)
print("F. SLIPPAGE AND OUT-OF-SAMPLE")
print("=" * 80)
ts = sorted({t["cts"] for t in q})
cut = ts[len(ts) // 2]
cutd = datetime.fromtimestamp(cut, timezone.utc).strftime("%Y-%m-%d")
print(f"  split at {cutd}\n")
print(f"  {'series':<10} {'window':<8} {'n':>5} {'WR%':>7} {'BE WR%':>7} "
      f"{'$/tr':>7} {'+1 tick':>8} {'total':>8}")
for s in ("KXBTCD", "KXETHD"):
    for tag, sel in (("first", [t for t in q if t["series"] == s and t["cts"] < cut]),
                     ("second", [t for t in q if t["series"] == s and t["cts"] >= cut])):
        if not sel:
            continue
        pl = [B.pnl(t["won"], t["ask"], BET, 0.0) for t in sel]
        p1 = [B.pnl(t["won"], t["ask"], BET, 1.0) for t in sel]
        wr = sum(1 for t in sel if t["won"]) / len(sel) * 100
        ask = sum(t["ask"] for t in sel) / len(sel)
        w = BET / (ask / 100) * (1 - ask / 100) * (1 - B.FEE)
        be = BET / (BET + w) * 100
        print(f"  {s:<10} {tag:<8} {len(sel):>5} {wr:>7.2f} {be:>7.2f} "
              f"{sum(pl)/len(pl):>+7.2f} {sum(p1)/len(p1):>+8.2f} {sum(pl):>+8,.0f}")

print("\n" + "=" * 80)
print("G. POOLED, AND AGAINST THE 15M BOOK")
print("=" * 80)
pl = [B.pnl(t["won"], t["ask"], BET, 0.0) for t in q]
p1 = [B.pnl(t["won"], t["ask"], BET, 1.0) for t in q]
p105 = [B.pnl(t["won"], t["ask"], BET, 0.105) for t in q]
span = (max(t["cts"] for t in q) - min(t["cts"] for t in q)) / 86400
print(f"  hourly, all: n={len(q)}  {sum(pl)/len(pl):+.2f}/tr  ${sum(pl):+,.0f}  "
      f"{sum(pl)/span:+.2f}/day")
print(f"    at measured 0.105c gap: ${sum(p105):+,.0f}  ({sum(p105)/span:+.2f}/day)")
print(f"    at one tick:            ${sum(p1):+,.0f}  ({sum(p1)/span:+.2f}/day)")
print(f"  entry rate: {len(q)/span:.1f} trades/day, vs 15M at "
      f"{5899/45:.0f}/day over the same window")
