#!/usr/bin/env python3
"""z-gate over EVERY day of data that exists — 2026-06-11..08-25, 76 days.

THIS IS THE MAXIMUM WINDOW, and it is not "since the series launched".
KXBTC15M and friends predate our data by months (their Kalshi volume programs ran
until 2026-05-12), but Kalshi retains settled markets only ~67 days and today's
floor has moved past Jun 20 — the API now returns ZERO markets for any date on or
before 2026-06-20. The only reason Jun 11-19 exists at all is that
scripts/archive_candles.py backfilled to the retention floor as it stood on Aug 17.
That data exists nowhere else. research/search2/data_ohlc/ starts Jun 19, so the
narrow archive is the deepest dataset in the project.

The strike comes from the archive's own floor_strike column (present since Jun 11),
which the RTI chain validated as the exact CF Benchmarks index level at each market's
open — see scripts/vol_zscore_brti.py.

WALK-FORWARD is the headline here, not the pooled number: for each week, the cut is
refitted on every prior week only, then scored on that week. No day is ever scored by
a threshold that saw it.
"""
import os, sys, glob, gzip, csv
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

PROD = {"KXBTC15M":"BTC-USD","KXETH15M":"ETH-USD","KXSOL15M":"SOL-USD",
        "KXDOGE15M":"DOGE-USD","KXBNB15M":"BNB-USD","KXXRP15M":"XRP-USD"}
SLIP, PCT = 0.105, 0.20
RNG = np.random.default_rng(11)

S = {}
for s, p in PROD.items():
    d = pd.read_csv(os.path.join(ROOT,"data",".spot_cache",p+".csv")).drop_duplicates("ts").sort_values("ts")
    g = np.arange(d.ts.min(), d.ts.max()+60, 60); d = d.set_index("ts").reindex(g)
    craw = d["close"].values.astype(float); lr = np.diff(np.log(craw), prepend=np.nan)
    S[s] = dict(ts=g, c=pd.Series(craw).ffill(limit=10).values,
                clr=np.nancumsum(np.nan_to_num(lr**2)), nlr=np.cumsum(~np.isnan(lr)))

def spot_sig(s, ends, mins=60):
    d=S[s]; j=np.searchsorted(d["ts"],ends,"right")-1; i=j-mins
    sp=np.full(len(ends),np.nan); sg=sp.copy(); ok=(i>=0)&(j>=0)&(j<len(d["ts"]))
    ii,jj=i[ok],j[ok]; n=d["nlr"][jj]-d["nlr"][ii]
    v=(d["clr"][jj]-d["clr"][ii])/np.maximum(n,1)
    sg[ok]=np.where(n>mins*0.6,np.sqrt(v),np.nan); sp[ok]=d["c"][jj]
    return sp,sg

strikes={}
for f in sorted(glob.glob(os.path.join(ROOT,"data","candles","*.csv.gz"))):
    with gzip.open(f,"rt") as fh:
        for r in csv.DictReader(fh):
            try: strikes[r["ticker"]]=float(r["floor_strike"])
            except (TypeError,ValueError): pass

cfg = B.live_config()
rows=[(se,tk,cts,side,ask,secs,won) for (se,tk,cts,side,ask,secs,won,p1,p2,p3)
      in B.load(series=cfg["series"]) if B.qualifies(cfg,se,side,ask,secs,p1,p2,p3)]
G=pd.DataFrame(rows,columns=["series","ticker","close_ts","side","ask","secs","won"])
G=G.sort_values("secs",ascending=False).drop_duplicates(["ticker","side"])
G["entry_ts"]=(G.close_ts-G.secs).astype(np.int64)
G["strike"]=G.ticker.map(strikes)
G["spot"]=np.nan; G["sig"]=np.nan
for s,g in G.groupby("series"):
    sp,sg=spot_sig(s,g.entry_ts.values); G.loc[g.index,["spot","sig"]]=np.column_stack([sp,sg])
sgn=np.where(G.side.values=="no",-1.0,1.0)
G["z"]=sgn*(G.spot-G.strike)/G.spot/(G.sig*np.sqrt(G.secs/60.0))
G["day"]=pd.to_datetime(G.entry_ts,unit="s",utc=True).dt.strftime("%Y-%m-%d")
G["week"]=pd.to_datetime(G.entry_ts,unit="s",utc=True).dt.strftime("%G-W%V")
G["pnl"]=[B.pnl(bool(w),a,cfg["bet"],SLIP) for w,a in zip(G.won,G.ask)]
G=G[G.day>="2026-06-11"]

def alloc(d):
    return pd.concat([g.sort_values("secs",ascending=False).head(cfg["max_conc"])
                      for _,g in d.groupby("close_ts")]) if len(d) else d

print(f"config {cfg['version']}  slip={SLIP}c  bet=${cfg['bet']:.0f}")
print(f"window {G.day.min()} .. {G.day.max()}  ({G.day.nunique()} days)  "
      f"signals={len(G)}  z known={G.z.notna().sum()} ({G.z.notna().mean()*100:.1f}%)")
base=alloc(G)
print(f"BASELINE v5.17 over everything: {len(base)}tr  {base.won.mean()*100:.2f}%WR  "
      f"{base.pnl.sum():+.0f}  ({base.pnl.mean():+.3f}/tr, {base.pnl.sum()/G.day.nunique():+.2f}/day)")

print("\n"+"="*74+"\nWALK-FORWARD — each week scored by a cut fitted ONLY on prior weeks\n"+"="*74)
wks=sorted(G.week.unique())
print(f"  {'week':<10}{'days':>5}{'cut':>7}{'base $':>9}{'gated $':>9}{'delta':>8}{'base WR':>9}{'gated WR':>9}")
tot_b=tot_g=0.0; deltas=[]; rowsout=[]
for i,w in enumerate(wks):
    prior=G[G.week<w]
    if len(prior)<400 or prior.z.notna().sum()<300:
        continue                                   # need history to fit a cut
    cut=prior.z.quantile(PCT)
    cur=G[G.week==w]
    b=alloc(cur); g=alloc(cur[(cur.z>=cut)|cur.z.isna()])
    if not len(b): continue
    tot_b+=b.pnl.sum(); tot_g+=g.pnl.sum(); deltas.append(g.pnl.sum()-b.pnl.sum())
    rowsout.append((w,cur.day.nunique(),b.pnl.sum(),g.pnl.sum()))
    print(f"  {w:<10}{cur.day.nunique():>5}{cut:>7.3f}{b.pnl.sum():>+9.0f}{g.pnl.sum():>+9.0f}"
          f"{g.pnl.sum()-b.pnl.sum():>+8.0f}{b.won.mean()*100:>8.2f}%{g.won.mean()*100:>8.2f}%")
nd=sum(r[1] for r in rowsout)
print(f"\n  TOTAL walk-forward ({len(rowsout)} weeks, {nd} days)")
print(f"    baseline {tot_b:+.0f}  ({tot_b/nd:+.2f}/day)")
print(f"    z-gated  {tot_g:+.0f}  ({tot_g/nd:+.2f}/day)")
print(f"    delta    {tot_g-tot_b:+.0f}  ({(tot_g-tot_b)/nd:+.2f}/day)   "
      f"weeks positive: {sum(1 for d in deltas if d>0)}/{len(deltas)}")

print("\n"+"="*74+"\nPOOLED, whole window, single cut fitted on the whole window (in-sample)\n"+"="*74)
cut=G.z.quantile(PCT); g=alloc(G[(G.z>=cut)|G.z.isna()])
print(f"  cut z>={cut:.3f}   baseline {base.pnl.sum():+.0f} -> gated {g.pnl.sum():+.0f}  "
      f"delta {g.pnl.sum()-base.pnl.sum():+.0f}   WR {base.won.mean()*100:.2f}% -> {g.won.mean()*100:.2f}%")

print("\n"+"="*74+"\nBY MONTH (walk-forward cut, so each month is out-of-sample)\n"+"="*74)
G["month"]=G.day.str[:7]
print(f"  {'month':<9}{'days':>5}{'base tr':>9}{'base WR':>9}{'base $/day':>12}{'gated WR':>10}{'gated $/day':>13}{'delta':>9}")
for m,cur in G.groupby("month"):
    prior=G[G.month<m]
    cut=prior.z.quantile(PCT) if prior.z.notna().sum()>300 else G[G.day<"2026-08-01"].z.quantile(PCT)
    b=alloc(cur); gg=alloc(cur[(cur.z>=cut)|cur.z.isna()]); d=cur.day.nunique()
    print(f"  {m:<9}{d:>5}{len(b):>9}{b.won.mean()*100:>8.2f}%{b.pnl.sum()/d:>12.2f}"
          f"{gg.won.mean()*100:>9.2f}%{gg.pnl.sum()/d:>13.2f}{gg.pnl.sum()-b.pnl.sum():>+9.0f}")

print("\n"+"="*74+"\nCLUSTER BOOTSTRAP on the walk-forward result\n"+"="*74)
cutmap={}
for w in wks:
    prior=G[G.week<w]
    cutmap[w]=prior.z.quantile(PCT) if prior.z.notna().sum()>=300 else np.nan
G["_cut"]=G.week.map(cutmap)
WF=G[G._cut.notna()]
b=alloc(WF); g=alloc(WF[(WF.z>=WF._cut)|WF.z.isna()])
bm=b.groupby("close_ts").pnl.sum(); gm=g.groupby("close_ts").pnl.sum()
keys=sorted(set(bm.index)|set(gm.index))
diff=np.array([gm.get(k,0.0)-bm.get(k,0.0) for k in keys]); K=len(diff)
o=np.sort(diff[RNG.integers(0,K,(4000,K))].sum(1))
print(f"  delta {diff.sum():+.0f}  95%CI [{o[100]:+.0f},{o[3900]:+.0f}]  P(>0)={(o>0).mean():.3f}  ({K} clusters)")

print("\n  per-series, walk-forward")
print(f"    {'series':<12}{'base $/tr':>11}{'gated $/tr':>12}{'delta $':>10}")
for s in sorted(cfg["series"]):
    sb=b[b.series==s]; sg=g[g.series==s]
    if len(sb): print(f"    {s:<12}{sb.pnl.mean():>11.3f}{sg.pnl.mean():>12.3f}{sg.pnl.sum()-sb.pnl.sum():>10.0f}")
