#!/usr/bin/env python3
"""Forward projection with the documented sizing ladder.

Method: block-bootstrap the ACTUAL per-cluster P&L sequence from the archive (so
autocorrelation and fat tails survive), scale each cluster linearly with the bet in
force at that moment, and re-size after every cluster using the §4 rule
(bet <= 4.6% of balance, symmetric — it cuts as readily as it raises). Stop at
STOP_BALANCE.

Deliberately NOT compounding of an average: that hides both the left tail, which
truncates the account, and the liquidity ceiling on the right.
"""
import os, sys
from collections import defaultdict
import numpy as np

ROOT = "/Users/chrisgarceau/pm"
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

cfg = B.live_config()
BASE_BET, SLIP = 50.0, 0.105
START, STOP, RATIO = 1370.37, 650.0, 0.046
ITERS, BLOCK, CHUNK = 3000, 48, 500

_rows = B.load()
_clusters = defaultdict(list)
for r in _rows:
    _clusters[r[2]].append(r)


def per_cluster(slip):
    out = {}
    for cts, crows in _clusters.items():
        best = {}
        for (se, tk, _, side, ask, secs, won, p1, p2, p3) in crows:
            if not B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
                continue
            k = (tk, side)
            if k not in best or secs > best[k][5]:
                best[k] = (se, tk, cts, side, ask, secs, won, p1, p2, p3)
        picked = sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]
        if picked:
            out[cts] = sum(B.pnl(v[6], v[4], BASE_BET, slip) for v in picked)
    return out


base = per_cluster(SLIP)
KEYS = sorted(base)
SPAN = (KEYS[-1] - KEYS[0]) / 86400.0
CPD = len(KEYS) / SPAN
ARR = np.array([base[k] for k in KEYS])


def simulate(series, days, cap=None, seed=3):
    """Vectorised across paths; sequential only in time (sizing needs the balance)."""
    n = int(round(days * CPD))
    N = len(series)
    nb = int(np.ceil(n / BLOCK))
    rng = np.random.default_rng(seed)
    finals, peaks = [], []
    for c0 in range(0, ITERS, CHUNK):
        m = min(CHUNK, ITERS - c0)
        starts = rng.integers(0, max(N - BLOCK, 1), size=(m, nb))
        idx = (starts[:, :, None] + np.arange(BLOCK)[None, None, :]).reshape(m, -1)[:, :n]
        paths = series[np.clip(idx, 0, N - 1)]
        bal = np.full(m, START)
        alive = np.ones(m, bool)
        peak = np.full(m, BASE_BET)
        for t in range(n):
            bet = np.maximum(np.floor(bal * RATIO / 25.0) * 25.0, 25.0)
            if cap:
                bet = np.minimum(bet, cap)
            peak = np.maximum(peak, np.where(alive, bet, 0))
            bal = np.where(alive, bal + paths[:, t] * (bet / BASE_BET), bal)
            alive &= bal > STOP
        finals.append(bal)
        peaks.append(peak)
    f = np.concatenate(finals)
    return f, float((f <= STOP).mean()), np.concatenate(peaks)


print(f"archive: {len(ARR)} clusters, {CPD:.1f}/day, ${ARR.sum():+,.0f} over {SPAN:.0f}d "
      f"at ${BASE_BET:.0f} flat / {SLIP}c slip  (= ${ARR.sum()/SPAN:+.1f}/day)")
print(f"start ${START:,.2f}   sizing: 4.6% of balance, rounded down to $25, "
      f"stop ${STOP:,.0f}\n")


def show(title, series, cap=None):
    print("=" * 90)
    print(title)
    print("=" * 90)
    print(f"  {'horizon':>9} {'p10':>10} {'p25':>10} {'median':>10} {'p75':>10} "
          f"{'p90':>10} {'P(ruin)':>8} {'peak bet':>9}")
    for label, d in (("1 month", 30), ("3 months", 90), ("6 months", 180)):
        f, ruin, pb = simulate(series, d, cap=cap)
        q = np.percentile(f, [10, 25, 50, 75, 90])
        print(f"  {label:>9} {q[0]:>10,.0f} {q[1]:>10,.0f} {q[2]:>10,.0f} "
              f"{q[3]:>10,.0f} {q[4]:>10,.0f} {ruin*100:>7.1f}% {np.median(pb):>9,.0f}")
    print()


show("A. MODELLED EDGE, measured slippage, NO capacity limit  [OPTIMISTIC]", ARR)
show("B. 70% OF THE MODELLED EDGE CAPTURED LIVE", ARR * 0.7)
show("C. 50% OF THE MODELLED EDGE CAPTURED LIVE", ARR * 0.5)

print("=" * 90)
print("D. SLIPPAGE IS THE WHOLE BALLGAME — 6-month median by fill quality")
print("=" * 90)
print(f"  {'slip':>8} {'$/day @ $50':>12} {'median 6mo':>12} {'p10 6mo':>10} {'P(ruin)':>9}")
for slip in (0.0, 0.105, 0.3, 0.5, 1.0):
    s = per_cluster(slip)
    a = np.array([s[k] for k in sorted(s)])
    f, ruin, _ = simulate(a, 180)
    print(f"  {slip:>7.3f}c {a.sum()/SPAN:>+12.1f} {np.percentile(f,50):>12,.0f} "
          f"{np.percentile(f,10):>10,.0f} {ruin*100:>8.1f}%")

print("\n" + "=" * 90)
print("E. A BET CEILING — what capping costs and what it buys, at 6 months")
print("=" * 90)
print(f"  {'cap':>8} {'median':>12} {'p10':>10} {'p90':>12} {'P(ruin)':>9}")
for cap in (100, 200, 400, None):
    f, ruin, _ = simulate(ARR, 180, cap=cap)
    print(f"  {str(cap or 'none'):>8} {np.percentile(f,50):>12,.0f} "
          f"{np.percentile(f,10):>10,.0f} {np.percentile(f,90):>12,.0f} {ruin*100:>8.1f}%")
