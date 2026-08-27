#!/usr/bin/env python3
"""Corrected inference for the z-gate, after an external review (2026-08-27).

THREE THINGS THE EARLIER ANALYSIS GOT WRONG, and what replaces them.

1. SIGMA CLAIMS OFF 60 PERMUTATIONS. An empirical one-sided p-value from N draws
   cannot go below 1/(N+1). 60 draws floor at 0.016; quoting "11.4 sd" implied a tail
   probability those draws cannot support. Now 500 draws, and the p-value is reported
   as the exceedance count, never as a sigma multiple.

2. THE GLOBAL PERMUTATION WAS THE WRONG NULL. Shuffling z across all rows destroys its
   relationship with asset, time-left, volatility, ask, week and missingness. A z that
   fails FORWARD would keep every one of those and lose only its link to the outcome.
   Replaced with a STRATIFIED permutation: z is shuffled only within
   week x series x side x ask-bucket x time-left-bucket, so everything except the
   outcome association is held fixed. The full pipeline — weekly threshold refit AND
   slot allocation — is rerun inside every permutation.

3. IID CLUSTER BOOTSTRAP IGNORES TIME. rv60 overlaps mechanically across consecutive
   clusters, and volatility, basis and signal density persist for days. Replaced with a
   MOVING-BLOCK bootstrap over daily deltas, reported across several block lengths so
   the sensitivity to the dependence assumption is visible rather than assumed away.
"""
import os, sys
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B
exec(open(os.path.join(ROOT,"scripts","vol_zscore_full.py")).read().split('def alloc(')[0])
RNG = np.random.default_rng(17)
MAXC, BET, FEE = cfg["max_conc"], cfg["bet"], 0.07

W = G[G.week.isin([w for w in sorted(G.week.unique()) if G[G.week<w].z.notna().sum()>=300])].copy()
W = W.reset_index(drop=True)
price = np.minimum(W.ask.values + SLIP, 99.0)
contracts = BET / (price/100.0)
W["pnl_take"] = np.where(W.won.values, contracts*(1-price/100.0)*(1-FEE), -BET)

clu, cluv = pd.factorize(W.close_ts.values)
wk, wkv = pd.factorize(W.week.values)
order = np.lexsort((-W.secs.values, clu))          # within cluster: most time left first
cl_rows = [[] for _ in range(len(cluv))]
for i in order: cl_rows[clu[i]].append(i)
cl_rows = [np.array(r) for r in cl_rows]
cl_week = np.array([wk[r[0]] for r in cl_rows])
cl_day  = np.array([W.day.values[r[0]] for r in cl_rows])
zv = W.z.values; pv = W.pnl_take.values
days = pd.unique(cl_day); nday = len(days)

def total(zcol, cuts):
    """Run the whole pipeline: gate by that week's cut, then fill 2 slots earliest-first."""
    out = 0.0
    for c, rows in enumerate(cl_rows):
        cut = cuts[cl_week[c]]
        z = zcol[rows]
        ok = np.isnan(z) | (z >= cut) if cut == cut else np.ones(len(rows), bool)
        sel = rows[ok][:MAXC]
        out += pv[sel].sum()
    return out

def daily(zcol, cuts):
    d = {}
    for c, rows in enumerate(cl_rows):
        cut = cuts[cl_week[c]]
        z = zcol[rows]
        ok = np.isnan(z) | (z >= cut) if cut == cut else np.ones(len(rows), bool)
        d[cl_day[c]] = d.get(cl_day[c], 0.0) + pv[rows[ok][:MAXC]].sum()
    return np.array([d.get(x, 0.0) for x in days])

def weekly_cuts(zcol):
    cuts = np.full(len(wkv), np.nan)
    for i, w in enumerate(wkv):
        prior = zcol[W.week.values < w]
        prior = prior[np.isfinite(prior)]
        if len(prior) >= 300: cuts[i] = np.quantile(prior, PCT)
    return cuts

nogate = np.full(len(wkv), -np.inf)
base_d = daily(zv, nogate); real_d = daily(zv, weekly_cuts(zv))
delta_d = real_d - base_d
print(f"walk-forward, {nday} days, {len(cl_rows)} clusters")
print(f"  baseline {base_d.sum():+.0f} ({base_d.sum()/nday:+.2f}/day)   "
      f"gated {real_d.sum():+.0f} ({real_d.sum()/nday:+.2f}/day)   delta {delta_d.sum():+.0f}")

# ── 1+2. stratified permutation, full pipeline inside each draw ───────────────
W["ab"] = pd.cut(W.ask, [87.9,90.5,91.5,92.5,93.01], labels=False)
W["tb"] = pd.cut(W.secs, [149,240,360,480,601], labels=False)
strat = pd.factorize(list(zip(W.week, W.series, W.side, W.ab, W.tb)))[0]
sidx = {}
for i, s in enumerate(strat): sidx.setdefault(s, []).append(i)
sidx = [np.array(v) for v in sidx.values() if len(v) > 1]
NP = 500
print(f"\n  STRATIFIED permutation — z shuffled within week x series x side x ask x time-left")
print(f"  ({len(sidx)} strata, {NP} draws, weekly refit + slot allocation rerun each time)")
exc = 0; tot = np.empty(NP)
for k in range(NP):
    zp = zv.copy()
    for ix in sidx: zp[ix] = zp[RNG.permutation(ix)]
    tot[k] = total(zp, weekly_cuts(zp)) - base_d.sum()
    if tot[k] >= delta_d.sum(): exc += 1
p = (exc + 1) / (NP + 1)
print(f"    null delta: mean {tot.mean():+.0f}  sd {tot.std():.0f}  "
      f"p95 {np.percentile(tot,95):+.0f}  max {tot.max():+.0f}")
print(f"    real {delta_d.sum():+.0f}   exceeded by {exc}/{NP}   one-sided p = {p:.4f}"
      f"   (floor for {NP} draws is {1/(NP+1):.4f})")

# ── 3. moving-block bootstrap over daily deltas ──────────────────────────────
print(f"\n  MOVING-BLOCK bootstrap over {nday} daily deltas (accounts for time dependence)")
ac = pd.Series(delta_d).autocorr(1)
print(f"    lag-1 autocorrelation of the daily delta: {ac:+.3f}")
print(f"    {'block':<8}{'mean':>9}{'2.5%':>9}{'97.5%':>9}{'P(>0)':>8}{'eff n':>8}")
for L in (1, 3, 5, 7, 10):
    nb = int(np.ceil(nday / L)); out = np.empty(4000)
    starts = np.arange(0, nday - L + 1)
    for b in range(4000):
        s = RNG.choice(starts, nb)
        out[b] = np.concatenate([delta_d[i:i+L] for i in s])[:nday].sum()
    out.sort()
    eff = nday / max(1.0, (1 + 2*sum(max(pd.Series(delta_d).autocorr(l) or 0, 0) for l in range(1, L+1))))
    print(f"    {L:<8}{out.mean():>9.0f}{out[100]:>9.0f}{out[3900]:>9.0f}"
          f"{(out>0).mean():>8.3f}{eff:>8.0f}")
