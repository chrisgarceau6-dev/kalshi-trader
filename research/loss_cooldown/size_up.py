#!/usr/bin/env python3
"""If the 15 min after a loss is the BEST window, should we size UP into it?

Two separate questions, and they must not be conflated:
  Q1. Is the post-loss edge real, or the same noise that produced the fake
      lag-1 clustering? Cluster bootstrap on the difference.
  Q2. Even if real, what does size-up-after-a-loss do to risk of ruin? Sizing is a
      risk decision (CLAUDE.md §4), so the test is P(touch the $650 stop), the same
      block bootstrap §4 used to reject the $100 bet.
"""
import os, sys, bisect
from collections import defaultdict
import numpy as np

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as B
from measure import candidates, cfg, BET, SLIP

STOP = 650.0
START = 1411.73        # live balance 2026-08-20
cand = candidates(B.load())
cts_sorted = sorted(cand)
days = (cts_sorted[-1] - cts_sorted[0]) / 86400.0


def baseline():
    out = []
    for cts in sorted(cand):
        out += sorted(cand[cts], key=lambda r: -r[5])[:cfg["max_conc"]]
    return out


base = baseline()
by = defaultdict(list)
for r in base:
    by[r[2]].append(r)
loss_ts = sorted(c for c in by if any(not r[6] for r in by[c]))

print("=" * 80)
print("Q1. IS THE POST-LOSS EDGE REAL?")
print("=" * 80)
print("  A loss is known at the losing cluster's CLOSE. 'In window' means the trade's")
print("  ENTRY fell within D minutes after that close.\n")
print(f"  {'window':>8} {'in n':>6} {'in $/tr':>8} {'out $/tr':>9} {'diff':>7} "
      f"{'bootstrap CI':>20} {'P(>0)':>7}")
for win_m in (15, 30, 60):
    inw, outw = [], []
    for r in base:
        entry = r[2] - r[5]
        i = bisect.bisect_right(loss_ts, entry)
        rec = loss_ts[i - 1] if i else None
        (inw if rec is not None and entry - rec <= win_m * 60 else outw).append(r)

    def boot(a, bb, iters=4000, seed=7):
        pa, pb = defaultdict(list), defaultdict(list)
        for r in a:
            pa[r[2]].append(B.pnl(r[6], r[4], BET, SLIP))
        for r in bb:
            pb[r[2]].append(B.pnl(r[6], r[4], BET, SLIP))
        keys = sorted(set(pa) | set(pb))
        sa = np.array([sum(pa.get(k, [])) for k in keys]);  na = np.array([len(pa.get(k, [])) for k in keys])
        sb = np.array([sum(pb.get(k, [])) for k in keys]);  nb = np.array([len(pb.get(k, [])) for k in keys])
        rng = np.random.default_rng(seed)
        idx = rng.integers(0, len(keys), size=(iters, len(keys)))
        d = np.sort(sa[idx].sum(1) / np.maximum(na[idx].sum(1), 1)
                    - sb[idx].sum(1) / np.maximum(nb[idx].sum(1), 1))
        obs = sa.sum() / na.sum() - sb.sum() / nb.sum()
        return obs, d[int(.0125 * iters)], d[int(.9875 * iters)], (d > 0).mean()

    o, lo, hi, p = boot(inw, outw)
    pi = np.mean([B.pnl(r[6], r[4], BET, SLIP) for r in inw])
    po = np.mean([B.pnl(r[6], r[4], BET, SLIP) for r in outw])
    print(f"  {win_m:>6}min {len(inw):>6} {pi:>+8.2f} {po:>+9.2f} {o:>+7.2f} "
          f"  [{lo:+.2f}, {hi:+.2f}] {p:>7.3f}")

print("\n" + "=" * 80)
print("Q2. WHAT DOES SIZING UP DO TO RISK OF RUIN?")
print("=" * 80)


def cluster_series(mult, win_m):
    """Per-cluster P&L with bet = BET*mult for entries within win_m of a loss."""
    per = []
    for cts in sorted(cand):
        picked = sorted(cand[cts], key=lambda r: -r[5])[:cfg["max_conc"]]
        tot = 0.0
        for r in picked:
            entry = r[2] - r[5]
            i = bisect.bisect_right(loss_ts, entry)
            rec = loss_ts[i - 1] if i else None
            hot = rec is not None and entry - rec <= win_m * 60
            tot += B.pnl(r[6], r[4], BET * (mult if hot else 1.0), SLIP)
        per.append(tot)
    return np.array(per)


def ruin(series, iters=4000, block=48, seed=11):
    """Block bootstrap: P(equity touches the $650 stop) starting from live balance."""
    rng = np.random.default_rng(seed)
    n = len(series)
    nb = int(np.ceil(n / block))
    hits = 0
    for _ in range(iters):
        starts = rng.integers(0, max(n - block, 1), size=nb)
        path = np.concatenate([series[s:s + block] for s in starts])[:n]
        eq = START + np.cumsum(path)
        if eq.min() <= STOP:
            hits += 1
    return hits / iters


print(f"  start ${START:,.2f}, stop ${STOP:,.0f}, block bootstrap over "
      f"{len(cts_sorted)} clusters ({days:.0f}d)\n")
print(f"  {'rule':>28} {'total':>9} {'/day':>8} {'maxDD':>9} {'P(hit $650)':>12}")
for mult, win_m, lbl in ((1.0, 0, "flat $50 (live)"),
                         (1.5, 15, "x1.5 for 15min after loss"),
                         (2.0, 15, "x2.0 for 15min after loss"),
                         (1.5, 30, "x1.5 for 30min after loss"),
                         (2.0, 30, "x2.0 for 30min after loss"),
                         (2.0, 60, "x2.0 for 60min after loss")):
    s = cluster_series(mult, win_m)
    eq = np.cumsum(s)
    dd = float((eq - np.maximum.accumulate(eq)).min())
    print(f"  {lbl:>28} {s.sum():>+9,.0f} {s.sum()/days:>+8.1f} {dd:>+9,.0f} "
          f"{ruin(s)*100:>11.1f}%")

print("\n  for scale, the same ruin metric on a flat size increase:")
for mult, lbl in ((1.0, "flat $50"), (1.5, "flat $75"), (2.0, "flat $100")):
    s = cluster_series(1.0, 0) * mult
    print(f"  {lbl:>28} {s.sum():>+9,.0f} {s.sum()/days:>+8.1f} "
          f"{float((np.cumsum(s)-np.maximum.accumulate(np.cumsum(s))).min()):>+9,.0f} "
          f"{ruin(s)*100:>11.1f}%")
