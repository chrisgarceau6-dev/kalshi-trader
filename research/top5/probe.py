#!/usr/bin/env python3
"""Ground the candidate list in measurements before ranking it.

Four things I have not personally verified and which carry the biggest prizes:

  P1. Entry timing. §7 calibration says 100-150s is -1.26pp and 360-480s is +1.27pp,
      a 2.5pp spread against a ~2pp total edge — yet §7 also says the min_secs sweep
      came out flat. Both cannot be the whole story. Which is right on TRADED entries?
  P2. Edge by entry price inside the traded band, IS and OOS. If real, size by it.
  P3. Edge-weighted sizing at constant average exposure — free P&L if the gradient
      in P2 holds, since it changes no trade count and no risk budget.
  P4. Capacity shape: at a FIXED dollar exposure per cluster, is it better to take
      2 big positions or 4 small ones? Book depth is per-market, so spreading may
      buy capacity the concentrated version cannot reach.
"""
import os, sys
from collections import defaultdict
import numpy as np

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

cfg = B.live_config()
BET, SLIP = cfg["bet"], 0.105
IS_END = "2026-07-31"

rows = B.load()
clusters = defaultdict(list)
for r in rows:
    clusters[r[2]].append(r)

DAY = {}
for r in rows:
    DAY[r[2]] = __import__("datetime").datetime.utcfromtimestamp(r[2]).strftime("%Y-%m-%d")


def qualified(cts):
    best = {}
    for (se, tk, _, side, ask, secs, won, p1, p2, p3) in clusters[cts]:
        if not B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
            continue
        k = (tk, side)
        if k not in best or secs > best[k][5]:
            best[k] = (se, tk, cts, side, ask, secs, won, p1, p2, p3)
    return list(best.values())


def pick(cts, max_conc=None):
    return sorted(qualified(cts), key=lambda r: -r[5])[:(max_conc or cfg["max_conc"])]


ALL = []
for cts in sorted(clusters):
    ALL += pick(cts)
SPAN = (max(clusters) - min(clusters)) / 86400.0
is_ = [r for r in ALL if DAY[r[2]] <= IS_END]
oos = [r for r in ALL if DAY[r[2]] > IS_END]
tot = lambda ts, bet=BET, slip=SLIP: sum(B.pnl(r[6], r[4], bet, slip) for r in ts)
print(f"{len(ALL)} entries over {SPAN:.0f}d | IS {len(is_)} / OOS {len(oos)} | "
      f"${BET:.0f} flat @ {SLIP}c → ${tot(ALL):+,.0f} ({tot(ALL)/SPAN:+.1f}/day)\n")


def boot(a, b, iters=3000, seed=5):
    """cluster bootstrap on mean($/trade) difference"""
    pa, pb = defaultdict(list), defaultdict(list)
    for r in a:
        pa[r[2]].append(B.pnl(r[6], r[4], BET, SLIP))
    for r in b:
        pb[r[2]].append(B.pnl(r[6], r[4], BET, SLIP))
    keys = sorted(set(pa) | set(pb))
    sa = np.array([sum(pa.get(k, [])) for k in keys]); na = np.array([len(pa.get(k, [])) for k in keys])
    sb = np.array([sum(pb.get(k, [])) for k in keys]); nb = np.array([len(pb.get(k, [])) for k in keys])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(keys), size=(iters, len(keys)))
    d = np.sort(sa[idx].sum(1) / np.maximum(na[idx].sum(1), 1)
                - sb[idx].sum(1) / np.maximum(nb[idx].sum(1), 1))
    obs = sa.sum() / max(na.sum(), 1) - sb.sum() / max(nb.sum(), 1)
    return obs, d[int(.0125 * iters)], d[int(.9875 * iters)]


print("=" * 86)
print("P1. ENTRY TIMING on traded entries — is the early window really better?")
print("=" * 86)
print(f"  {'secs_left':>12} {'n':>6} {'WR%':>7} {'$/tr':>8} | {'OOS n':>6} {'OOS $/tr':>9}")
buckets = [(150, 240), (240, 360), (360, 480), (480, 600)]
for lo, hi in buckets:
    a = [r for r in is_ if lo <= r[5] < hi]
    o = [r for r in oos if lo <= r[5] < hi]
    if not a:
        continue
    print(f"  {f'{lo}-{hi}':>12} {len(a):>6} "
          f"{sum(1 for r in a if r[6])/len(a)*100:>7.2f} {tot(a)/len(a):>+8.2f} | "
          f"{len(o):>6} {tot(o)/len(o) if o else 0:>+9.2f}")

print("\n  MIN_SECS sweep — TOTAL P&L, which is what a volume business cares about")
print(f"  {'min_secs':>10} {'IS n':>6} {'IS total':>10} {'OOS n':>6} {'OOS total':>10}")
for ms in (150, 200, 240, 300, 360):
    c2 = dict(cfg, min_secs=ms)
    sel = []
    for cts in sorted(clusters):
        best = {}
        for (se, tk, _, side, ask, secs, won, p1, p2, p3) in clusters[cts]:
            if not B.qualifies(c2, se, side, ask, secs, p1, p2, p3):
                continue
            k = (tk, side)
            if k not in best or secs > best[k][5]:
                best[k] = (se, tk, cts, side, ask, secs, won, p1, p2, p3)
        sel += sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]
    i2 = [r for r in sel if DAY[r[2]] <= IS_END]
    o2 = [r for r in sel if DAY[r[2]] > IS_END]
    print(f"  {ms:>10} {len(i2):>6} {tot(i2):>+10,.0f} {len(o2):>6} {tot(o2):>+10,.0f}")

print("\n" + "=" * 86)
print("P2. EDGE BY ENTRY PRICE inside the traded band")
print("=" * 86)
print(f"  {'ask':>5} {'IS n':>6} {'IS $/tr':>9} {'IS edge pp':>11} | "
      f"{'OOS n':>6} {'OOS $/tr':>9} {'OOS edge pp':>12}")
for ask in (90, 91, 92, 93):
    a = [r for r in is_ if r[4] == ask]
    o = [r for r in oos if r[4] == ask]
    if not a:
        continue
    ep = lambda ts: sum((1 if r[6] else 0) - r[4] / 100 for r in ts) / len(ts) * 100
    print(f"  {ask:>5} {len(a):>6} {tot(a)/len(a):>+9.2f} {ep(a):>+11.2f} | "
          f"{len(o):>6} {tot(o)/len(o) if o else 0:>+9.2f} {ep(o) if o else 0:>+12.2f}")

print("\n" + "=" * 86)
print("P3. EDGE-WEIGHTED SIZING at constant average exposure")
print("=" * 86)
print("  Sizing by EDGE is Kelly-correct and is not the martingale that sizing by")
print("  recent RESULTS would be. Weights normalised so mean bet = $50 — same risk")
print("  budget, same trade count, only the allocation changes.\n")


def weighted(ts, wfn):
    w = np.array([wfn(r) for r in ts], float)
    w *= len(w) / w.sum()                      # mean weight = 1
    return sum(B.pnl(r[6], r[4], BET * wi, SLIP) for r, wi in zip(ts, w)), w


print(f"  {'scheme':>28} {'IS total':>10} {'OOS total':>10} {'max bet':>9}")
print(f"  {'flat $50':>28} {tot(is_):>+10,.0f} {tot(oos):>+10,.0f} {BET:>9,.0f}")
schemes = {
    "linear in (93 - ask)": lambda r: (94 - r[4]),
    "steep: (93-ask)^2": lambda r: (94 - r[4]) ** 2,
    "by secs_left": lambda r: max(r[5] - 120, 30),
}
for name, f in schemes.items():
    ti, wi = weighted(is_, f)
    to, wo = weighted(oos, f)
    print(f"  {name:>28} {ti:>+10,.0f} {to:>+10,.0f} {BET*max(wi.max(), wo.max()):>9,.0f}")

print("\n" + "=" * 86)
print("P4. CAPACITY SHAPE — same dollars per cluster, spread wider or concentrated?")
print("=" * 86)
print("  Book depth is per-market (median 172 contracts in band), so N smaller")
print("  positions may fit where 2 large ones would sweep. Exposure held at $300.\n")
print(f"  {'layout':>22} {'n':>6} {'IS total':>10} {'OOS total':>10} {'ct/order':>9}")
for mc, bet in ((2, 150.0), (3, 100.0), (4, 75.0), (6, 50.0)):
    sel = []
    for cts in sorted(clusters):
        sel += sorted(qualified(cts), key=lambda r: -r[5])[:mc]
    i2 = [r for r in sel if DAY[r[2]] <= IS_END]
    o2 = [r for r in sel if DAY[r[2]] > IS_END]
    print(f"  {f'{mc} x ${bet:.0f}':>22} {len(sel):>6} {tot(i2,bet):>+10,.0f} "
          f"{tot(o2,bet):>+10,.0f} {bet/0.91:>9.0f}")
