#!/usr/bin/env python3
"""Is a hard z-gate the BEST way to spend this finding? Four ways, same walk-forward.

The strategy is SLOT-bound (MAX_CONCURRENT=2 blocks ~51% of qualifying entries), not
capital-bound. That makes 'skip the bad ones' only one of several uses for a signal:

  A  GATE        skip z<cut; slots still go to the earliest signal   (what is proposed)
  B  RANK        skip nothing; give the 2 slots to the HIGHEST z in the cluster
                 instead of the most-time-left  -> free, cuts zero volume
  C  GATE+RANK   both
  D  TILT        skip nothing, size by z, normalised to the same average bet
"""
import os, sys, glob, gzip, csv
import numpy as np, pandas as pd
ROOT=os.path.expanduser("~/pm"); sys.path.insert(0,os.path.join(ROOT,"scripts"))
import backtest as B
exec(open(os.path.join(ROOT,"scripts","vol_zscore_full.py")).read().split('def alloc(')[0]
     .replace('ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))','ROOT = os.path.expanduser("~/pm")'))
RNG=np.random.default_rng(11)
MAXC=cfg["max_conc"]; BET=cfg["bet"]; FEE=0.07

def pnl_at(won, ask, bet):
    price=min(ask+SLIP,99.0); c=bet/(price/100.0)
    return c*(1-price/100.0)*(1-FEE) if won else -bet

def run(df, gate=None, rank="secs", tilt=None):
    out=[]
    for cts,g in df.groupby("close_ts"):
        d=g if gate is None else g[(g.z>=gate)|g.z.isna()]
        if not len(d): continue
        if rank=="secs":  d=d.sort_values("secs",ascending=False)
        elif rank=="z":   d=d.sort_values("z",ascending=False,na_position="last")
        d=d.head(MAXC)
        for r in d.itertuples():
            bet=BET
            if tilt is not None and r.z==r.z:
                bet=BET*tilt(r.z)
            out.append((cts,r.series,r.won,r.ask,bet,pnl_at(bool(r.won),r.ask,bet)))
    return pd.DataFrame(out,columns=["close_ts","series","won","ask","bet","pnl"])

def tilt_fn(cut):
    # 2x above the cut, 0.5x below — then renormalised so mean bet is unchanged
    return lambda z: 2.0 if z>=cut else 0.5

wks=sorted(G.week.unique())
res={k:[] for k in ("BASE","A","B","C","D")}
for w in wks:
    prior=G[G.week<w]
    if prior.z.notna().sum()<300: continue
    cut=prior.z.quantile(PCT); cur=G[G.week==w]
    res["BASE"].append(run(cur))
    res["A"].append(run(cur,gate=cut))
    res["B"].append(run(cur,rank="z"))
    res["C"].append(run(cur,gate=cut,rank="z"))
    d=run(cur,tilt=tilt_fn(cut))
    d["pnl"]*= BET/d.bet.mean(); d["bet"]*= BET/d.bet.mean()   # same average capital
    res["D"].append(d)
R={k:pd.concat(v) for k,v in res.items()}
days=G[G.week.isin([w for w in wks if G[G.week<w].z.notna().sum()>=300])].day.nunique()

print(f"walk-forward, {days} days, MAX_CONCURRENT={MAXC}, bet=${BET:.0f}, slip={SLIP}c\n")
print(f"  {'variant':<32}{'trades':>8}{'WR%':>8}{'$/tr':>9}{'total$':>9}{'$/day':>9}{'vs base':>9}")
b0=R["BASE"].pnl.sum()
lab={"BASE":"baseline v5.17 (earliest-first)","A":"A  hard gate, earliest-first",
     "B":"B  z-RANKED slots, no gate","C":"C  gate + z-ranked slots",
     "D":"D  z sizing tilt 2x/0.5x, no gate"}
for k in ("BASE","A","B","C","D"):
    r=R[k]
    print(f"  {lab[k]:<32}{len(r):>8}{r.won.mean()*100:>8.2f}{r.pnl.mean():>9.3f}"
          f"{r.pnl.sum():>9.0f}{r.pnl.sum()/days:>9.2f}{r.pnl.sum()-b0:>+9.0f}")

def boot(x,y):
    xm=x.groupby("close_ts").pnl.sum(); ym=y.groupby("close_ts").pnl.sum()
    keys=sorted(set(xm.index)|set(ym.index))
    d=np.array([xm.get(k,0.0)-ym.get(k,0.0) for k in keys]); K=len(d)
    o=np.sort(d[RNG.integers(0,K,(4000,K))].sum(1))
    return d.sum(),o[100],o[3900],(o>0).mean()
print("\n  cluster bootstrap vs baseline")
for k in ("A","B","C","D"):
    m,l,h,p=boot(R[k],R["BASE"])
    print(f"    {lab[k]:<32}{m:>+7.0f}  95%CI [{l:+.0f},{h:+.0f}]  P(>0)={p:.3f}")
print("\n  C vs A — does ranking add on top of the gate?")
m,l,h,p=boot(R["C"],R["A"]); print(f"    {m:+.0f}  95%CI [{l:+.0f},{h:+.0f}]  P(>0)={p:.3f}")
print("  B vs A — ranking alone vs gating alone")
m,l,h,p=boot(R["B"],R["A"]); print(f"    {m:+.0f}  95%CI [{l:+.0f},{h:+.0f}]  P(>0)={p:.3f}")
print(f"\n  volume kept: base {len(R['BASE'])}  A {len(R['A'])} ({len(R['A'])/len(R['BASE'])*100:.0f}%)  "
      f"B {len(R['B'])} ({len(R['B'])/len(R['BASE'])*100:.0f}%)  C {len(R['C'])} ({len(R['C'])/len(R['BASE'])*100:.0f}%)")
