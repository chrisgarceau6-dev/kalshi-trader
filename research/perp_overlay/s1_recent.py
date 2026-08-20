#!/usr/bin/env python3
"""Would the momentum veto have helped in the Aug 17-19 drawdown?"""
import research as R
from collections import defaultdict

cands = R.build()
cfg = R.B.live_config()
bet = cfg["bet"]
sel = [t for t in R.select(cands, cfg) if t["m3"] is not None]
tot = lambda ts, s=0.105: sum(R.pnl(t, bet, s) for t in ts)

print("Per UTC day, modelled at the measured 0.105c execution gap, $50 flat\n")
print(f"  {'day':<12} {'all n':>6} {'all $':>8} {'blocked':>8} {'blk $/tr':>9} "
      f"{'kept $':>8} {'delta':>7}")
byd = defaultdict(list)
for t in sel:
    byd[t["day"]].append(t)
recent = sorted(byd)[-10:]
tot_d = 0.0
for d in recent:
    ss = byd[d]
    blk = [t for t in ss if t["m3"] > 0.5]
    kept = [t for t in ss if t["m3"] <= 0.5]
    delta = tot(kept) - tot(ss)
    tot_d += delta
    bpt = tot(blk) / len(blk) if blk else 0.0
    print(f"  {d:<12} {len(ss):>6} {tot(ss):>+8,.0f} {len(blk):>8} {bpt:>+9.2f} "
          f"{tot(kept):>+8,.0f} {delta:>+7,.0f}")
print(f"  {'10-day':<12} {'':>6} {tot([t for d in recent for t in byd[d]]):>+8,.0f} "
      f"{'':>8} {'':>9} {'':>8} {tot_d:>+7,.0f}")

print("\nThe three worst modelled days in the archive, and what the veto does to them:")
worst = sorted(byd, key=lambda d: tot(byd[d]))[:5]
for d in worst:
    ss = byd[d]
    kept = [t for t in ss if t["m3"] <= 0.5]
    print(f"  {d}  all {tot(ss):>+8,.0f} -> kept {tot(kept):>+8,.0f} "
          f"(delta {tot(kept)-tot(ss):>+7,.0f})")
