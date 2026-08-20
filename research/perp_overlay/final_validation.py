#!/usr/bin/env python3
"""Rigorous validation of the two survivors, against this repo's evidence bar.

  S1  veto entries with strongly adverse 3-min spot momentum
  S2  extend the ask band down to 88-89c, but only when momentum is not adverse

Both get: cluster-bootstrap CIs, one tick of slippage, series concentration, and an
honest trade count against the 500-per-bucket bar in CLAUDE.md rule 5.
"""
import statistics
from collections import defaultdict
import research as R

cands = R.build()
cfg = R.B.live_config()
bet = cfg["bet"]


def pnl_of(ts, slip=0.0):
    return [R.pnl(t, bet, slip) for t in ts]


def show(tag, ts, slip=0.0):
    if not ts:
        print(f"  {tag:<30} —")
        return
    p = pnl_of(ts, slip)
    wr = sum(1 for t in ts if t["won"]) / len(ts) * 100
    print(f"  {tag:<30} n={len(ts):>5}  WR {wr:>6.2f}%  {sum(p)/len(p):>+6.2f}/tr  "
          f"total {sum(p):>+8,.0f}")


print("=" * 84)
print("S1 — VETO STRONGLY ADVERSE MOMENTUM.  Are the blocked trades genuinely bad?")
print("=" * 84)
sel = [t for t in R.select(cands, cfg) if t["m3"] is not None]
for thr in (1.5, 1.0, 0.75, 0.5):
    print(f"\n  trigger: block m3 > {thr:+.2f}")
    for tag, ss in (("IS ", R.split(sel)[0]), ("OOS", R.split(sel)[1])):
        blocked = [t for t in ss if t["m3"] > thr]
        kept = [t for t in ss if t["m3"] <= thr]
        if not blocked:
            continue
        pb = pnl_of(blocked)
        m, lo, hi = R.cluster_boot(blocked, lambda t: R.pnl(t, bet))
        print(f"    {tag} blocked n={len(blocked):>4} "
              f"{sum(pb)/len(pb):>+6.2f}/tr  CI [{lo:+.2f}, {hi:+.2f}]   "
              f"kept {sum(pnl_of(kept)):>+8,.0f} vs all {sum(pnl_of(ss)):>+8,.0f} "
              f"(delta {sum(pnl_of(kept))-sum(pnl_of(ss)):>+7,.0f})")

print("\n  POOLED (both windows) — the blocked bucket's own P&L, the cleanest statement")
for thr in (1.5, 1.0, 0.75, 0.5):
    blocked = [t for t in sel if t["m3"] > thr]
    m, lo, hi = R.cluster_boot(blocked, lambda t: R.pnl(t, bet))
    obs, dlo, dhi, p = R.boot_diff(blocked, [t for t in sel if t["m3"] <= thr],
                                   lambda t: R.pnl(t, bet))
    print(f"    m3>{thr:+.2f}  n={len(blocked):>4}  {m:>+6.2f}/tr CI [{lo:+.2f},{hi:+.2f}]"
          f"   vs kept: {obs:+.2f}/tr CI [{dlo:+.2f},{dhi:+.2f}]  P(worse)={1-p:.3f}")

print("\n" + "=" * 84)
print("S2 — EXTEND THE BAND TO 88-89c WHEN MOMENTUM IS NOT ADVERSE")
print("=" * 84)
wide = dict(cfg, min_ask=88, max_ask=89)
ws = [t for t in R.select(cands, wide) if t["m3"] is not None]
print("\n  break-even WR at 88.5c is ~88.6%, vs ~91.5% at 91.5c — a cheaper contract")
print("  needs a lower win rate, so a lower WR here is not automatically worse.\n")
for tag, i in (("IS ", 0), ("OOS", 1)):
    ss = R.split(ws)[i]
    print(f"  {tag}")
    show("    all 88-89c", ss)
    show("    m3 <= 0", [t for t in ss if t["m3"] <= 0])
    show("    m3 <= 0, +1 tick slip", [t for t in ss if t["m3"] <= 0], slip=1.0)

print("\n  POOLED")
sub = [t for t in ws if t["m3"] <= 0]
m, lo, hi = R.cluster_boot(sub, lambda t: R.pnl(t, bet))
print(f"    88-89c & m3<=0   n={len(sub)}  {m:+.2f}/tr  CI [{lo:+.2f}, {hi:+.2f}]")
m1, lo1, hi1 = R.cluster_boot(sub, lambda t: R.pnl(t, bet, 1.0))
print(f"    same, +1 tick    n={len(sub)}  {m1:+.2f}/tr  CI [{lo1:+.2f}, {hi1:+.2f}]")
m2, lo2, hi2 = R.cluster_boot([t for t in ws if t["m3"] > 0], lambda t: R.pnl(t, bet))
print(f"    88-89c & m3>0    n={len([t for t in ws if t['m3']>0])}  {m2:+.2f}/tr  "
      f"CI [{lo2:+.2f}, {hi2:+.2f}]   <- the half the filter throws away")

print("\n  BY SERIES (pooled, m3<=0) — is it one series carrying it?")
for se in sorted({t["series"] for t in sub}):
    ss = [t for t in sub if t["series"] == se]
    p = pnl_of(ss)
    print(f"    {se:<11} n={len(ss):>4}  {sum(p)/len(p):>+6.2f}/tr  {sum(p):>+7,.0f}")

print("\n" + "=" * 84)
print("COMBINED PROPOSAL vs LIVE — what the whole change is worth")
print("=" * 84)
full = dict(cfg, min_ask=88)
prop = [t for t in R.select(cands, full)
        if t["m3"] is not None and not (t["ask"] <= 89 and t["m3"] > 0)
        and t["m3"] <= 1.0]
live = [t for t in R.select(cands, cfg) if t["m3"] is not None]
for tag, i in (("IS ", 0), ("OOS", 1)):
    print(f"\n  {tag}")
    show("    live config", R.split(live)[i])
    show("    proposal", R.split(prop)[i])
    show("    proposal, +1 tick slip", R.split(prop)[i], slip=1.0)
    a, b = R.split(live)[i], R.split(prop)[i]
    print(f"    delta: {sum(pnl_of(b))-sum(pnl_of(a)):+,.0f} over "
          f"{len({t['cts'] for t in b})} clusters, "
          f"{len(b)-len(a):+d} trades")
