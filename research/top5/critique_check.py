#!/usr/bin/env python3
"""Verify the external critique's quantitative claims against the actual data.

Claims to check:
  C1. Inverse-variance combination: +$0.104/tr, 95% CI [-0.030, +0.239], p~0.13
  C2. Serial dependence — cluster-IID bootstrap overstates precision; 3-day blocks
      give OOS ~[-0.185, +0.344], P(>0)~0.78
  C3. The seven weight functions are ~1.26 effective independent directions
  C4. Normalising on the OOS window's own secs_left distribution is procedurally
      impure; using IS normalisation moves OOS gain +290 -> +306
  C5. The cap clips weights without renormalising, so capped variants no longer
      hold average exposure constant
  C6. Power: a true $0.10/tr effect needs ~5,500 fresh clusters for the expected
      one-sided 95% lower bound to reach zero, ~12,000 for 80% power
"""
import os, sys
from collections import defaultdict
from datetime import datetime
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
W = lambda r: max(r[5] - 120, 30)

print(f"IS {len(IS)} trades / {len({r[2] for r in IS})} clusters / "
      f"{len({DAY[r[2]] for r in IS})} days")
print(f"OOS {len(OOS)} trades / {len({r[2] for r in OOS})} clusters / "
      f"{len({DAY[r[2]] for r in OOS})} days\n")


def diffs(ts, norm_from=None):
    """per-trade (weighted - flat) using a normaliser fitted on norm_from."""
    g = np.array([float(W(r)) for r in ts])
    src = np.array([float(W(r)) for r in (norm_from or ts)])
    w = g / src.mean()
    return np.array([B.pnl(r[6], r[4], BET * wi, SLIP) - B.pnl(r[6], r[4], BET, SLIP)
                     for r, wi in zip(ts, w)])


def by_cluster(ts, d):
    s, n = defaultdict(float), defaultdict(int)
    for r, x in zip(ts, d):
        s[r[2]] += x
        n[r[2]] += 1
    k = sorted(s)
    return np.array(k), np.array([s[i] for i in k]), np.array([n[i] for i in k], float)


def boot(ts, d, block_days=0, iters=6000, seed=17):
    k, s, n = by_cluster(ts, d)
    rng = np.random.default_rng(seed)
    if block_days <= 0:
        idx = rng.integers(0, len(k), size=(iters, len(k)))
    else:
        L = max(1, int(block_days * 75))          # ~75 clusters/day
        nb = int(np.ceil(len(k) / L))
        st = rng.integers(0, max(len(k) - L, 1), size=(iters, nb))
        idx = (st[:, :, None] + np.arange(L)[None, None, :]).reshape(iters, -1)[:, :len(k)]
        idx = np.clip(idx, 0, len(k) - 1)
    tot = s[idx].sum(1) / np.maximum(n[idx].sum(1), 1)
    tot.sort()
    return (s.sum() / n.sum(), tot[int(.025 * iters)], tot[int(.975 * iters)],
            float((tot > 0).mean()), tot.std())


print("=" * 86)
print("C2. SERIAL DEPENDENCE — does block resampling widen the interval?")
print("=" * 86)
print(f"  {'window':>6} {'block':>10} {'mean':>8} {'95% CI':>22} {'P(>0)':>7} {'SE':>7}")
res = {}
for tag, ts in (("IS", IS), ("OOS", OOS)):
    d = diffs(ts)
    for bd, lbl in ((0, "cluster-IID"), (1, "1-day"), (3, "3-day"), (5, "5-day")):
        m, lo, hi, p, se = boot(ts, d, bd)
        res[(tag, bd)] = (m, se)
        print(f"  {tag:>6} {lbl:>10} {m:>+8.3f}   [{lo:>+6.3f}, {hi:>+6.3f}] "
              f"{p:>7.3f} {se:>7.3f}")

print("\n" + "=" * 86)
print("C1. INVERSE-VARIANCE COMBINATION (3-day blocks, the honest SE)")
print("=" * 86)
for bd in (0, 3):
    (mi, si), (mo, so) = res[("IS", bd)], res[("OOS", bd)]
    wi, wo = 1 / si ** 2, 1 / so ** 2
    m = (mi * wi + mo * wo) / (wi + wo)
    se = (1 / (wi + wo)) ** 0.5
    z = m / se
    from math import erfc, sqrt
    p2 = erfc(abs(z) / sqrt(2))
    print(f"  block={'IID' if bd==0 else str(bd)+'-day':>6}  combined {m:+.3f}/tr  "
          f"SE {se:.3f}  95% CI [{m-1.96*se:+.3f}, {m+1.96*se:+.3f}]  "
          f"z={z:.2f}  two-sided p={p2:.3f}")
print("  (treats the windows as independent, which overstates the evidence)")

print("\n" + "=" * 86)
print("C3. ARE THE SEVEN WEIGHT FUNCTIONS SEVEN TESTS?")
print("=" * 86)
fns = {
    "secs-120": lambda r: max(r[5] - 120, 30),
    "secs-100": lambda r: max(r[5] - 100, 30),
    "secs": lambda r: r[5],
    "sqrt": lambda r: r[5] ** 0.5,
    "(s/600)^2": lambda r: (r[5] / 600.0) ** 2,
    "step360": lambda r: 1.5 if r[5] >= 360 else 1.0,
    "step300": lambda r: 1.0 if r[5] >= 300 else 0.5,
}
M = np.array([[float(f(r)) for r in SEL] for f in fns.values()])
M = (M - M.mean(1, keepdims=True)) / M.std(1, keepdims=True)
C = np.corrcoef(M)
print("  pairwise correlation of the weight vectors:")
names = list(fns)
print("           " + " ".join(f"{n[:8]:>9}" for n in names))
for i, n in enumerate(names):
    print(f"  {n:>9} " + " ".join(f"{C[i,j]:>9.2f}" for j in range(len(names))))
ev = np.linalg.eigvalsh(C)
ev = np.clip(ev, 0, None)
eff = ev.sum() ** 2 / (ev ** 2).sum()
print(f"\n  min corr {C[np.triu_indices(len(names),1)].min():.2f}, "
      f"max off-diagonal {C[np.triu_indices(len(names),1)].max():.2f}")
print(f"  effective dimensionality (sum(ev)^2 / sum(ev^2)) = {eff:.2f} of 7")

print("\n" + "=" * 86)
print("C4 / C5. TWO BUGS IN MY IMPLEMENTATION")
print("=" * 86)
d_self = diffs(OOS)
d_is = diffs(OOS, norm_from=IS)
print(f"  normaliser fitted on OOS itself : OOS gain {d_self.sum():+,.0f}  "
      f"(what I reported)")
print(f"  normaliser fitted on IS only    : OOS gain {d_is.sum():+,.0f}  "
      f"(what a prospective rule gets)")

g = np.array([float(W(r)) for r in OOS])
w = g / g.mean()
for cap in (75, 85, 100):
    c = cap / BET
    wc = np.minimum(w, c)
    wr = wc * (len(wc) / wc.sum())
    ex_c = BET * wc.mean()
    ex_r = BET * wr.mean()
    pl_c = sum(B.pnl(r[6], r[4], BET * x, SLIP) - B.pnl(r[6], r[4], BET, SLIP)
               for r, x in zip(OOS, wc))
    pl_r = sum(B.pnl(r[6], r[4], BET * x, SLIP) - B.pnl(r[6], r[4], BET, SLIP)
               for r, x in zip(OOS, wr))
    print(f"  cap ${cap}: clipped avg bet ${ex_c:.2f} gain {pl_c:>+7,.0f}  |  "
          f"renormalised avg bet ${ex_r:.2f} gain {pl_r:>+7,.0f}")

print("\n" + "=" * 86)
print("C6. HOW MUCH FRESH DATA WOULD SETTLE IT?")
print("=" * 86)
_, se_oos = res[("OOS", 3)]
n_oos = len({r[2] for r in OOS})
per_cl = se_oos * np.sqrt(n_oos)          # SE scales as 1/sqrt(clusters)
print(f"  OOS SE {se_oos:.4f}/tr on {n_oos} clusters (3-day blocks)")
for target, label in ((1.645, "one-sided 95% LB reaches 0 in expectation"),
                      (1.645 + 0.84, "80% power")):
    need = (per_cl * target / 0.10) ** 2
    print(f"  for a true +$0.10/tr effect, {label}: "
          f"~{need:,.0f} clusters = ~{need/75:,.0f} days")
