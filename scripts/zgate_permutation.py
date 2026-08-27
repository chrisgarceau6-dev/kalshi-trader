#!/usr/bin/env python3
"""Two nulls, because the gate operates at two levels and they must be priced apart.

  GLOBAL shuffle  — z permuted across ALL rows. Destroys cluster-level and within-cluster
                    structure alike. The true "z is meaningless" case.
  WITHIN-CLUSTER  — z permuted inside each settlement cluster only. KEEPS cluster-level z
                    (whole settlements where every coin sits on its strike) and destroys
                    only the choice of which contract inside it to skip.

Difference between the two nulls = the value of cluster-level z.
Real minus within-cluster null = the value of picking the right contract.
"""
import os,sys
import numpy as np, pandas as pd
ROOT=os.path.expanduser("~/pm"); sys.path.insert(0,os.path.join(ROOT,"scripts"))
import backtest as B
exec(open(os.path.join(ROOT,"scripts","vol_zscore_full.py")).read().split('def alloc(')[0]
     .replace('ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))','ROOT = os.path.expanduser("~/pm")'))
RNG=np.random.default_rng(5); MAXC=cfg["max_conc"]; BET=cfg["bet"]; FEE=0.07
def pnl_at(w,a):
    p=min(a+SLIP,99.0); c=BET/(p/100.0)
    return c*(1-p/100.0)*(1-FEE) if w else -BET
def run(df,zcol,gate):
    out=[]
    for cts,g in df.groupby("close_ts"):
        d=g[(g[zcol]>=gate)|g[zcol].isna()] if gate is not None else g
        if not len(d): continue
        for r in d.sort_values("secs",ascending=False).head(MAXC).itertuples():
            out.append(pnl_at(bool(r.won),r.ask))
    return np.array(out)
wks=[w for w in sorted(G.week.unique()) if G[G.week<w].z.notna().sum()>=300]
days=G[G.week.isin(wks)].day.nunique()
base=np.concatenate([run(G[G.week==w],"z",None) for w in wks]).sum()
real=np.concatenate([run(G[G.week==w],"z",G[G.week<w].z.quantile(PCT)) for w in wks]).sum()
print(f"walk-forward, {days} days")
print(f"  baseline      {base:>+8.0f} ({base/days:+.2f}/day)")
print(f"  REAL z gate   {real:>+8.0f} ({real/days:+.2f}/day)   delta {real-base:+.0f} ({(real-base)/days:+.2f}/day)\n")
res={}
for name,mode,n in (("GLOBAL shuffle — z is meaningless","g",60),
                    ("WITHIN-CLUSTER shuffle — keeps cluster-level z","w",60)):
    tot=[]
    for it in range(n):
        Gp=G.copy()
        if mode=="g":
            v=Gp.z.values.copy(); RNG.shuffle(v); Gp["zp"]=v
        else:
            Gp["zp"]=Gp.groupby("close_ts")["z"].transform(lambda s: RNG.permutation(s.values))
        tot.append(np.concatenate([run(Gp[Gp.week==w],"zp",Gp[Gp.week<w].zp.quantile(PCT)) for w in wks]).sum())
    t=np.array(tot); d=t-base; res[mode]=d
    print(f"  {name}")
    print(f"    delta mean {d.mean():>+7.0f} ({d.mean()/days:+.2f}/day)  sd {d.std():.0f}  range [{d.min():+.0f},{d.max():+.0f}]")
    print(f"    real is {(real-base-d.mean())/d.std():.1f} sd above it;  beaten by {(t>=real).sum()}/{n} permutations\n")
g,w=res["g"].mean(),res["w"].mean(); tot_d=real-base
print("  ATTRIBUTION of the +%.2f/day"%(tot_d/days))
print(f"    mechanical (cutting ~20% of a slot-bound book) {g/days:>+7.2f}/day  {g/tot_d*100:>5.1f}%")
print(f"    cluster-level z (sit out whipsaw settlements)  {(w-g)/days:>+7.2f}/day  {(w-g)/tot_d*100:>5.1f}%")
print(f"    contract-level z (pick the right one to skip)  {(tot_d-w)/days:>+7.2f}/day  {(tot_d-w)/tot_d*100:>5.1f}%")
