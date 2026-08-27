#!/usr/bin/env python3
"""A (gate) vs D (sizing tilt) deliver the same edge. They do NOT carry the same risk.
CLAUDE.md: bet size is a risk decision, the daily limit is denominated in BETS, and
MAX_CONCURRENT exists to cap per-cluster exposure. Price that."""
import os,sys
import numpy as np, pandas as pd
ROOT=os.path.expanduser("~/pm"); sys.path.insert(0,os.path.join(ROOT,"scripts"))
import backtest as B
exec(open(os.path.join(ROOT,"scripts","vol_zscore_full.py")).read().split('def alloc(')[0]
     .replace('ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))','ROOT = os.path.expanduser("~/pm")'))
RNG=np.random.default_rng(11); MAXC=cfg["max_conc"]; BET=cfg["bet"]; FEE=0.07
def pnl_at(won,ask,bet):
    p=min(ask+SLIP,99.0); c=bet/(p/100.0)
    return c*(1-p/100.0)*(1-FEE) if won else -bet
def run(df,gate=None,tilt=None):
    out=[]
    for cts,g in df.groupby("close_ts"):
        d=g if gate is None else g[(g.z>=gate)|g.z.isna()]
        if not len(d):continue
        for r in d.sort_values("secs",ascending=False).head(MAXC).itertuples():
            bet=BET*(tilt(r.z) if (tilt and r.z==r.z) else 1.0)
            out.append((cts,bool(r.won),r.ask,bet,pnl_at(bool(r.won),r.ask,bet)))
    return pd.DataFrame(out,columns=["close_ts","won","ask","bet","pnl"])
def norm(d):
    k=BET/d.bet.mean(); d=d.copy(); d["pnl"]*=k; d["bet"]*=k; return d
def stats(d,days):
    cl=d.groupby("close_ts").agg(pnl=("pnl","sum"),risk=("bet","sum"))
    eq=cl.pnl.cumsum(); dd=(eq-eq.cummax()).min()
    r24=cl.pnl.rolling(96,min_periods=1).sum().min()   # ~24h of 15-min clusters
    return dict(n=len(d),wr=d.won.mean()*100,tot=d.pnl.sum(),perday=d.pnl.sum()/days,
                maxdd=dd,worst_cluster=cl.pnl.min(),max_exposure=cl.risk.max(),
                worst24=r24,avg_bet=d.bet.mean(),cap_at_risk=cl.risk.mean())
wks=sorted(G.week.unique()); parts={k:[] for k in ("BASE","A","D","H")}
for w in wks:
    pr=G[G.week<w]
    if pr.z.notna().sum()<300: continue
    cut=pr.z.quantile(PCT); lo=pr.z.quantile(0.10); cur=G[G.week==w]
    parts["BASE"].append(run(cur))
    parts["A"].append(run(cur,gate=cut))
    parts["D"].append(norm(run(cur,tilt=lambda z:2.0 if z>=cut else 0.5)))
    # H: gate the worst decile only, then a MILD tilt above it — less volume cut, less leverage
    parts["H"].append(norm(run(cur,gate=lo,tilt=lambda z:1.35 if z>=cut else 0.8)))
R={k:pd.concat(v) for k,v in parts.items()}
days=G[G.week.isin([w for w in wks if G[G.week<w].z.notna().sum()>=300])].day.nunique()
lab={"BASE":"baseline v5.17","A":"A  hard gate (20th pct)","D":"D  sizing tilt 2x/0.5x",
     "H":"H  gate 10th pct + mild tilt"}
print(f"walk-forward, {days} days, bet=${BET:.0f}\n")
print(f"  {'variant':<30}{'n':>7}{'WR%':>7}{'$/day':>8}{'maxDD':>9}{'worst clu':>11}{'worst 24h':>11}{'max exp':>9}{'avg cap':>9}")
for k in ("BASE","A","D","H"):
    s=stats(R[k],days)
    print(f"  {lab[k]:<30}{s['n']:>7}{s['wr']:>7.2f}{s['perday']:>8.2f}{s['maxdd']:>9.0f}"
          f"{s['worst_cluster']:>11.0f}{s['worst24']:>11.0f}{s['max_exposure']:>9.0f}{s['cap_at_risk']:>9.2f}")
def boot(x,y):
    xm=x.groupby("close_ts").pnl.sum(); ym=y.groupby("close_ts").pnl.sum()
    ks=sorted(set(xm.index)|set(ym.index))
    d=np.array([xm.get(k,0.)-ym.get(k,0.) for k in ks]); K=len(d)
    o=np.sort(d[RNG.integers(0,K,(4000,K))].sum(1)); return d.sum(),o[100],o[3900],(o>0).mean()
print("\n  head-to-head")
for a,b in (("D","A"),("H","A")):
    m,l,h,p=boot(R[a],R[b]); print(f"    {lab[a]:<30} minus {lab[b]:<26}{m:>+7.0f} CI[{l:+.0f},{h:+.0f}] P={p:.3f}")
print(f"\n  STOP_BALANCE=$400 headroom (worst 24h / bet): "
      f"base {abs(stats(R['BASE'],days)['worst24'])/BET:.1f} losses, "
      f"A {abs(stats(R['A'],days)['worst24'])/BET:.1f}, D {abs(stats(R['D'],days)['worst24'])/BET:.1f}")
