#!/usr/bin/env python3
"""S1 robustness. If the veto only works at one momentum window or dies at one tick
of slippage, it is a curve fit and gets recorded as refuted."""
import research as R

cands = R.build()
cfg = R.B.live_config()
bet = cfg["bet"]
sel = [t for t in R.select(cands, cfg) if t["m3"] is not None]
days_is, days_oos = 51.0, 19.0


def tot(ts, slip=0.0):
    return sum(R.pnl(t, bet, slip) for t in ts)


print("=" * 88)
print("A. MOMENTUM WINDOW — does the veto survive at 1 and 5 minutes too?")
print("=" * 88)
print(f"  {'window':>8} {'thr':>6} {'IS blocked':>11} {'IS $/tr':>9} {'OOS blocked':>12} "
      f"{'OOS $/tr':>9}  {'both<0?':>8}")
for key in ("m1", "m3", "m5"):
    for thr in (0.5, 1.0):
        i_b = [t for t in R.split(sel)[0] if t[key] is not None and t[key] > thr]
        o_b = [t for t in R.split(sel)[1] if t[key] is not None and t[key] > thr]
        if not i_b or not o_b:
            continue
        a, b = tot(i_b) / len(i_b), tot(o_b) / len(o_b)
        print(f"  {key:>8} {thr:>6.2f} {len(i_b):>11} {a:>+9.2f} {len(o_b):>12} "
              f"{b:>+9.2f}  {'yes' if a < 0 and b < 0 else 'NO':>8}")

print("\n" + "=" * 88)
print("B. SLIPPAGE — the veto must help at realistic fills, not just quoted ones")
print("=" * 88)
for slip in (0.0, 0.105, 1.0):
    print(f"\n  slippage +{slip}c  (0.105c is the measured live execution gap)")
    print(f"    {'variant':<26} {'IS total':>10} {'IS/day':>8} {'OOS total':>10} {'OOS/day':>8}")
    base_i, base_o = tot(R.split(sel)[0], slip), tot(R.split(sel)[1], slip)
    print(f"    {'live config':<26} {base_i:>+10,.0f} {base_i/days_is:>+8.1f} "
          f"{base_o:>+10,.0f} {base_o/days_oos:>+8.1f}")
    for thr in (1.0, 0.75, 0.5, 0.25):
        k_i = [t for t in R.split(sel)[0] if t["m3"] <= thr]
        k_o = [t for t in R.split(sel)[1] if t["m3"] <= thr]
        a, b = tot(k_i, slip), tot(k_o, slip)
        print(f"    {'veto m3 > %+.2f' % thr:<26} {a:>+10,.0f} {a/days_is:>+8.1f} "
              f"{b:>+10,.0f} {b/days_oos:>+8.1f}   "
              f"(delta {a-base_i:>+6,.0f} / {b-base_o:>+6,.0f})")

print("\n" + "=" * 88)
print("C. IS THE EFFECT BROAD-BASED? blocked-trade P&L by series, pooled, m3>+0.50")
print("=" * 88)
blk = [t for t in sel if t["m3"] > 0.5]
for se in sorted({t["series"] for t in blk}):
    ss = [t for t in blk if t["series"] == se]
    print(f"    {se:<11} n={len(ss):>4}  {tot(ss)/len(ss):>+6.2f}/tr  {tot(ss):>+7,.0f}")
print(f"    {'ALL':<11} n={len(blk):>4}  {tot(blk)/len(blk):>+6.2f}/tr  {tot(blk):>+7,.0f}")

print("\n  by side")
for sd in ("yes", "no"):
    ss = [t for t in blk if t["side"] == sd]
    print(f"    {sd.upper():<11} n={len(ss):>4}  {tot(ss)/len(ss):>+6.2f}/tr  {tot(ss):>+7,.0f}")

print("\n" + "=" * 88)
print("D. MONTH BY MONTH — the veto's delta, to check it is not one bad week")
print("=" * 88)
from collections import defaultdict
bym = defaultdict(list)
for t in sel:
    bym[t["day"][:7]].append(t)
for mo in sorted(bym):
    ss = bym[mo]
    b = [t for t in ss if t["m3"] > 0.5]
    if not b:
        continue
    print(f"    {mo}  all {tot(ss):>+8,.0f}  blocked n={len(b):>4} "
          f"{tot(b)/len(b):>+6.2f}/tr  delta {-tot(b):>+7,.0f}")
