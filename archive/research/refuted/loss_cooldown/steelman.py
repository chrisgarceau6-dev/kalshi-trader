#!/usr/bin/env python3
"""Follow-ups to measure.py, so the idea is refuted in its strongest form.

  S1. Is the lag-1 clustering significant at all, or is it noise?
  S2. Maybe only a DOUBLE loss (both concurrent slots lose, ~-$100 in one print)
      signals a regime — that is the finest-grained version of the daily-loss-limit
      mechanism that actually works.
  S3. Maybe the right trigger is a rolling drawdown smaller than the validated $300.
"""
import bisect, os, sys
from collections import defaultdict
import numpy as np

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as B
from measure import candidates, stats, cfg, BET, SLIP

cand = candidates(B.load())
cts_sorted = sorted(cand)
days = (cts_sorted[-1] - cts_sorted[0]) / 86400.0
mid = cts_sorted[len(cts_sorted) // 2]


def run(cooldown_s, trigger):
    """trigger(picked) -> bool. Causal sequential walk."""
    last, taken, blocked = -1e18, [], []
    for cts in sorted(cand):
        pool = cand[cts]
        ok = [r for r in pool if (cts - r[5]) >= last + cooldown_s]
        no = [r for r in pool if (cts - r[5]) < last + cooldown_s]
        picked = sorted(ok, key=lambda r: -r[5])[:cfg["max_conc"]]
        taken += picked
        blocked += sorted(no, key=lambda r: -r[5])[:cfg["max_conc"]]
        if picked and trigger(picked):
            last = max(last, cts)
    return taken, blocked


base, _ = run(0, lambda p: False)
b = stats(base)
print(f"baseline {b['n']} trades  ${b['tot']:+,.0f}  ({b['per']:+.2f}/tr)\n")

print("=" * 78)
print("S1. IS THE LAG-1 CLUSTERING SIGNIFICANT?")
print("=" * 78)
by = defaultdict(list)
for r in base:
    by[r[2]].append(r)
lost = np.array([any(not r[6] for r in by[c]) for c in sorted(by)])
n = len(lost)
a, bb = lost[:-1], lost[1:]
p_ll = bb[a].mean() * 100
p_lw = bb[~a].mean() * 100
obs = p_ll - p_lw
rng = np.random.default_rng(7)
# permutation null: shuffle the sequence, destroying any time structure
null = np.empty(5000)
for i in range(5000):
    s = rng.permutation(lost)
    x, y = s[:-1], s[1:]
    null[i] = y[x].mean() * 100 - y[~x].mean() * 100
p = (null >= obs).mean()
print(f"  observed lag-1 lift {obs:+.2f}pp   permutation P(>= observed) = {p:.3f}")
print(f"  null spread: sd {null.std():.2f}pp, 97.5th pct {np.percentile(null,97.5):+.2f}pp")
print(f"  -> {'significant' if p < 0.025 else 'NOT significant — consistent with noise'}")
print(f"  n = {int(a.sum())} clusters followed a losing cluster")

print("\n" + "=" * 78)
print("S2. DOUBLE LOSSES ONLY — both slots lose the same print (~-$100)")
print("=" * 78)
dbl = sum(1 for c in by if sum(1 for r in by[c] if not r[6]) >= 2)
print(f"  clusters where both slots lost: {dbl} of {len(by)}\n")
print(f"  {'rule':>26} {'n':>6} {'$/tr':>8} {'total':>9} {'blocked':>8} {'blk $/tr':>9}")
print(f"  {'no cooldown':>26} {b['n']:>6} {b['per']:>+8.2f} {b['tot']:>+9,.0f} "
      f"{'—':>8} {'—':>9}")
for D in (15, 30, 60, 120, 240):
    t, blk = run(D * 60, lambda p: sum(1 for r in p if not r[6]) >= 2)
    s, sb = stats(t), stats(blk)
    print(f"  {'double-loss ' + str(D) + 'min':>26} {s['n']:>6} {s['per']:>+8.2f} "
          f"{s['tot']:>+9,.0f} {sb['n']:>8} {sb['per']:>+9.2f}")

print("\n" + "=" * 78)
print("S3. ROLLING DRAWDOWN TRIGGERS BELOW THE VALIDATED $300")
print("=" * 78)
print("  CLAUDE.md warns $200 is a level nothing has tested and it halted the trader")
print("  for most of a day. Here is what each threshold would actually have done.\n")


def run_dd(threshold, window_s=86400):
    """Halt while realised P&L over the trailing window is below -threshold."""
    hist, taken = [], []
    for cts in sorted(cand):
        cut = cts - window_s
        while hist and hist[0][0] < cut:
            hist.pop(0)
        rolling = sum(x[1] for x in hist)
        pool = cand[cts]
        if rolling <= -threshold:
            continue
        picked = sorted(pool, key=lambda r: -r[5])[:cfg["max_conc"]]
        taken += picked
        for r in picked:
            hist.append((cts, B.pnl(r[6], r[4], BET, SLIP)))
    return taken


print(f"  {'threshold':>12} {'n':>6} {'WR%':>7} {'$/tr':>8} {'total':>9} {'/day':>8}")
for thr in (100, 150, 200, 300, 400, 600, 10 ** 9):
    t = run_dd(thr)
    s = stats(t)
    lbl = "none" if thr > 10 ** 8 else f"${thr}"
    print(f"  {lbl:>12} {s['n']:>6} {s['wr']:>7.2f} {s['per']:>+8.2f} "
          f"{s['tot']:>+9,.0f} {s['tot']/days:>+8.1f}")

print("\n  same, split IS / OOS")
print(f"  {'threshold':>12} {'IS total':>10} {'OOS total':>10}")
for thr in (150, 200, 300, 600, 10 ** 9):
    t = run_dd(thr)
    i = stats([r for r in t if r[2] < mid])
    o = stats([r for r in t if r[2] >= mid])
    lbl = "none" if thr > 10 ** 8 else f"${thr}"
    print(f"  {lbl:>12} {i['tot']:>+10,.0f} {o['tot']:>+10,.0f}")
