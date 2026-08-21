#!/usr/bin/env python3
"""Validate edge-weighted sizing by time-to-close before recommending it.

The finding: filtering on secs_left FAILS out-of-sample (raising MIN_SECS improves
IS and hurts OOS — the volume-business trap again), but WEIGHTING by secs_left at
constant average exposure improves BOTH windows. The timing edge is real; it just
has to be expressed through allocation rather than exclusion.

Before that becomes a recommendation it has to survive: arbitrary weight-function
choice, slippage, a cluster bootstrap, and a per-cluster risk cap.
"""
import os, sys
from collections import defaultdict
from datetime import datetime
import numpy as np

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

cfg = B.live_config()
BET = cfg["bet"]
IS_END = "2026-07-31"

rows = B.load()
clusters = defaultdict(list)
for r in rows:
    clusters[r[2]].append(r)
DAY = {c: datetime.utcfromtimestamp(c).strftime("%Y-%m-%d") for c in clusters}

SEL = []
for cts in sorted(clusters):
    best = {}
    for (se, tk, _, side, ask, secs, won, p1, p2, p3) in clusters[cts]:
        if not B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
            continue
        k = (tk, side)
        if k not in best or secs > best[k][5]:
            best[k] = (se, tk, cts, side, ask, secs, won, p1, p2, p3)
    SEL += sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]

IS = [r for r in SEL if DAY[r[2]] <= IS_END]
OOS = [r for r in SEL if DAY[r[2]] > IS_END]


def weights(ts, wfn, cap=None):
    w = np.array([float(wfn(r)) for r in ts])
    w *= len(w) / w.sum()
    if cap:
        w = np.minimum(w, cap / BET)
    return w


def total(ts, wfn=None, slip=0.105, cap=None):
    if wfn is None:
        return sum(B.pnl(r[6], r[4], BET, slip) for r in ts)
    w = weights(ts, wfn, cap)
    return sum(B.pnl(r[6], r[4], BET * wi, slip) for r, wi in zip(ts, w))


print("=" * 88)
print("A. WEIGHT-FUNCTION ROBUSTNESS — is the result an artifact of one curve?")
print("=" * 88)
print(f"  {'weight w(secs)':>34} {'IS':>10} {'OOS':>10} {'IS %':>7} {'OOS %':>7} {'max bet':>8}")
bi, bo = total(IS), total(OOS)
print(f"  {'flat (baseline)':>34} {bi:>+10,.0f} {bo:>+10,.0f} {'—':>7} {'—':>7} {BET:>8,.0f}")
fns = {
    "secs - 120":            lambda r: max(r[5] - 120, 30),
    "secs - 100":            lambda r: max(r[5] - 100, 30),
    "secs (raw)":            lambda r: r[5],
    "sqrt(secs)":            lambda r: r[5] ** 0.5,
    "(secs/600)^2":          lambda r: (r[5] / 600.0) ** 2,
    "step: 1.0 <360, 1.5 >=360": lambda r: 1.5 if r[5] >= 360 else 1.0,
    "step: 0.5 <300, 1.0 >=300": lambda r: 1.0 if r[5] >= 300 else 0.5,
}
for name, f in fns.items():
    ti, to = total(IS, f), total(OOS, f)
    mx = BET * max(weights(IS, f).max(), weights(OOS, f).max())
    print(f"  {name:>34} {ti:>+10,.0f} {to:>+10,.0f} "
          f"{(ti/bi-1)*100:>+6.1f}% {(to/bo-1)*100:>+6.1f}% {mx:>8,.0f}")

print("\n" + "=" * 88)
print("B. SLIPPAGE — does the gain survive worse fills?")
print("=" * 88)
W = lambda r: max(r[5] - 120, 30)
print(f"  {'slip':>8} {'flat IS':>10} {'wtd IS':>10} {'flat OOS':>10} {'wtd OOS':>10} {'OOS gain':>9}")
for slip in (0.0, 0.105, 0.3, 0.5, 1.0):
    fi, wi = total(IS, slip=slip), total(IS, W, slip=slip)
    fo, wo = total(OOS, slip=slip), total(OOS, W, slip=slip)
    print(f"  {slip:>7.3f}c {fi:>+10,.0f} {wi:>+10,.0f} {fo:>+10,.0f} {wo:>+10,.0f} "
          f"{(wo-fo):>+9,.0f}")

print("\n" + "=" * 88)
print("C. CLUSTER BOOTSTRAP on the per-trade difference (weighted - flat)")
print("=" * 88)
for tag, ts in (("IS ", IS), ("OOS", OOS)):
    w = weights(ts, W)
    per = defaultdict(float)
    cnt = defaultdict(int)
    for r, wi in zip(ts, w):
        per[r[2]] += B.pnl(r[6], r[4], BET * wi, 0.105) - B.pnl(r[6], r[4], BET, 0.105)
        cnt[r[2]] += 1
    keys = sorted(per)
    s = np.array([per[k] for k in keys]); n = np.array([cnt[k] for k in keys], float)
    rng = np.random.default_rng(11)
    idx = rng.integers(0, len(keys), size=(4000, len(keys)))
    d = np.sort(s[idx].sum(1) / n[idx].sum(1))
    print(f"  {tag} mean diff {s.sum()/n.sum():+.3f}/tr  "
          f"CI [{d[50]:+.3f}, {d[3949]:+.3f}]  P(>0)={float((d>0).mean()):.3f}  "
          f"total {s.sum():+,.0f}")

print("\n" + "=" * 88)
print("D. RISK — per-cluster exposure, and what a bet cap costs")
print("=" * 88)
w_all = weights(SEL, W)
per_cl = defaultdict(float)
for r, wi in zip(SEL, w_all):
    per_cl[r[2]] += BET * wi
ex = np.array(sorted(per_cl.values()))
flat_max = BET * cfg["max_conc"]
print(f"  flat exposure per cluster: ${flat_max:,.0f} max")
print(f"  weighted: median ${np.median(ex):,.0f}  p90 ${np.percentile(ex,90):,.0f}  "
      f"max ${ex.max():,.0f}")
print(f"\n  {'bet cap':>10} {'IS':>10} {'OOS':>10} {'OOS vs flat':>12}")
for cap in (75, 85, 100, None):
    ti, to = total(IS, W, cap=cap), total(OOS, W, cap=cap)
    print(f"  {str(cap or 'none'):>10} {ti:>+10,.0f} {to:>+10,.0f} {(to-bo):>+12,.0f}")

print("\n" + "=" * 88)
print("E. DOES IT STACK WITH THE ASK GRADIENT?")
print("=" * 88)
combo = lambda r: max(r[5] - 120, 30) * (94 - r[4])
for name, f in (("secs only", W), ("ask only", lambda r: 94 - r[4]),
                ("secs x ask", combo)):
    ti, to = total(IS, f), total(OOS, f)
    print(f"  {name:>12} IS {ti:>+9,.0f} ({(ti/bi-1)*100:>+5.1f}%)  "
          f"OOS {to:>+9,.0f} ({(to/bo-1)*100:>+5.1f}%)")
