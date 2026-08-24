#!/usr/bin/env python3
"""Are losses actually clustered, and does halting after one pay?

Pre-registered before looking at results:

  D1. Losses are positively autocorrelated across close clusters — P(loss in the next
      cluster | loss now) exceeds the base rate.
  D2. Edge in the window AFTER a loss is worse than the unconditional edge, and
      decays back toward it as the window lengthens.
  P1. A cooldown of D minutes after any losing trade raises TOTAL P&L, in both
      windows, at the measured 0.105c execution gap.

Kill criteria (same as research/perp_overlay/PREREG.md):
  - opposite sign IS vs OOS = refuted
  - per-trade improvement with lower total P&L is NOT a win; this is a volume business
  - must survive the measured execution gap, not just quoted fills

Careful about one thing: the seven series settle simultaneously, so two positions in
one cluster losing together is MECHANICAL, not evidence of a regime. All clustering
here is measured BETWEEN clusters, never within one.
"""
import os, sys
from collections import defaultdict

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

cfg = B.live_config()
BET = cfg["bet"]
SLIP = 0.105          # measured live execution gap
IS_END = 1785196800   # 2026-07-26 ~ midpoint of the archive; recomputed below


def candidates(rows):
    """One row per (ticker, side) per cluster — earliest qualifying signal."""
    out = defaultdict(dict)
    for (se, tk, cts, side, ask, secs, won, p1, p2, p3) in rows:
        if not B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
            continue
        k = (tk, side)
        cur = out[cts].get(k)
        if cur is None or secs > cur[5]:
            out[cts][k] = (se, tk, cts, side, ask, secs, won, p1, p2, p3)
    return {cts: list(v.values()) for cts, v in out.items()}


def simulate(cand, cooldown_s, net_rule=False, slip=SLIP):
    """Sequential walk. A cooldown started at a losing cluster's close blocks any
    entry whose ENTRY time falls inside it. Causal: a trade that is never taken
    cannot trigger a cooldown."""
    last_loss = -1e18
    taken, blocked = [], []
    for cts in sorted(cand):
        pool = cand[cts]
        allowed = [r for r in pool if (cts - r[5]) >= last_loss + cooldown_s]
        stopped = [r for r in pool if (cts - r[5]) < last_loss + cooldown_s]
        picked = sorted(allowed, key=lambda r: -r[5])[:cfg["max_conc"]]
        # what the blocked ones would have done, for pricing the cost of the halt
        would = sorted(stopped, key=lambda r: -r[5])[:cfg["max_conc"]]
        taken += picked
        blocked += would
        if picked:
            pnls = [B.pnl(r[6], r[4], BET, slip) for r in picked]
            hit = (sum(pnls) < 0) if net_rule else any(not r[6] for r in picked)
            if hit:
                last_loss = max(last_loss, cts)
    return taken, blocked


def stats(trades, slip=SLIP):
    if not trades:
        return dict(n=0, wr=0.0, tot=0.0, per=0.0)
    p = [B.pnl(r[6], r[4], BET, slip) for r in trades]
    return dict(n=len(trades), wr=sum(1 for r in trades if r[6]) / len(trades) * 100,
                tot=sum(p), per=sum(p) / len(p))


rows = B.load()
cand = candidates(rows)
cts_sorted = sorted(cand)
mid = cts_sorted[len(cts_sorted) // 2]
days = (cts_sorted[-1] - cts_sorted[0]) / 86400.0
base_taken, _ = simulate(cand, 0)
b = stats(base_taken)
print(f"archive {len(rows):,} rows | {len(cand)} clusters | {days:.0f} days | "
      f"${BET:.0f}/trade | slip {SLIP}c")
print(f"baseline: {b['n']} trades  {b['wr']:.2f}%WR  ${b['tot']:+,.0f}  "
      f"({b['per']:+.2f}/tr, {b['tot']/days:+.1f}/day)\n")

print("=" * 78)
print("D1. IS A LOSS FOLLOWED BY MORE LOSSES? (between clusters, never within)")
print("=" * 78)
by_cluster = defaultdict(list)
for r in base_taken:
    by_cluster[r[2]].append(r)
seq = [(cts, by_cluster[cts]) for cts in sorted(by_cluster)]
lost = [any(not r[6] for r in ts) for _, ts in seq]
base_rate = sum(lost) / len(lost) * 100
print(f"  clusters with >=1 loss: {sum(lost)}/{len(lost)} = {base_rate:.2f}%")
for lag in (1, 2, 3, 4, 8):
    pairs = [(lost[i], lost[i + lag]) for i in range(len(lost) - lag)]
    after_loss = [b_ for a, b_ in pairs if a]
    after_win = [b_ for a, b_ in pairs if not a]
    pl = sum(after_loss) / len(after_loss) * 100 if after_loss else 0
    pw = sum(after_win) / len(after_win) * 100 if after_win else 0
    print(f"  lag {lag} ({lag*15:>3}min): P(loss | loss) {pl:>6.2f}%   "
          f"P(loss | no loss) {pw:>6.2f}%   lift {pl-pw:>+6.2f}pp")

print("\n" + "=" * 78)
print("D2. EDGE IN THE WINDOW AFTER A LOSS")
print("=" * 78)
loss_ts = sorted({cts for cts, ts in seq if any(not r[6] for r in ts)})
import bisect
print(f"  {'window':>10} {'n':>6} {'WR%':>7} {'$/tr':>8} {'vs baseline':>12}")
for win_m in (15, 30, 60, 120, 240):
    inw, outw = [], []
    for r in base_taken:
        entry = r[2] - r[5]
        i = bisect.bisect_right(loss_ts, entry)
        recent = loss_ts[i - 1] if i else None
        (inw if recent is not None and entry - recent <= win_m * 60 else outw).append(r)
    s = stats(inw)
    print(f"  {win_m:>7}min {s['n']:>6} {s['wr']:>7.2f} {s['per']:>+8.2f} "
          f"{s['per']-b['per']:>+12.2f}")

print("\n" + "=" * 78)
print("P1. THE POLICY — halt for D minutes after a loss")
print("=" * 78)
print(f"  {'rule':>22} {'n':>6} {'WR%':>7} {'$/tr':>8} {'total':>9} {'/day':>8} "
      f"{'blocked':>8} {'blk $/tr':>9}")
print(f"  {'no cooldown':>22} {b['n']:>6} {b['wr']:>7.2f} {b['per']:>+8.2f} "
      f"{b['tot']:>+9,.0f} {b['tot']/days:>+8.1f} {'—':>8} {'—':>9}")
for net_rule in (False, True):
    tag = "net-negative cluster" if net_rule else "any losing trade"
    for D in (15, 30, 45, 60, 120):
        t, blk = simulate(cand, D * 60, net_rule)
        s, sb = stats(t), stats(blk)
        print(f"  {tag[:12]+' '+str(D)+'min':>22} {s['n']:>6} {s['wr']:>7.2f} "
              f"{s['per']:>+8.2f} {s['tot']:>+9,.0f} {s['tot']/days:>+8.1f} "
              f"{sb['n']:>8} {sb['per']:>+9.2f}")

print("\n" + "=" * 78)
print("IN-SAMPLE / HOLDOUT — the only test that matters")
print("=" * 78)
import datetime
print(f"  split at {datetime.datetime.fromtimestamp(mid, datetime.timezone.utc):%Y-%m-%d}\n")
print(f"  {'rule':>22} {'IS total':>10} {'IS/day':>8} {'OOS total':>10} {'OOS/day':>8}")
for D in (0, 15, 30, 60, 120):
    t, _ = simulate(cand, D * 60)
    i = [r for r in t if r[2] < mid]
    o = [r for r in t if r[2] >= mid]
    di = (mid - cts_sorted[0]) / 86400.0
    do = (cts_sorted[-1] - mid) / 86400.0
    si, so = stats(i), stats(o)
    lbl = "no cooldown" if D == 0 else f"halt {D}min after loss"
    print(f"  {lbl:>22} {si['tot']:>+10,.0f} {si['tot']/di:>+8.1f} "
          f"{so['tot']:>+10,.0f} {so['tot']/do:>+8.1f}")
