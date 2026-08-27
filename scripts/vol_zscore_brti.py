#!/usr/bin/env python3
"""z-gate rechecked on the TRUE settlement basis (CF Benchmarks RTI), 2026-08-27.

WHAT WAS WRONG WITH THE FIRST VERSION
-------------------------------------
It built the numerator as (Coinbase spot at entry - floor_strike). But `floor_strike`
is an RTI 60-second average, not a Coinbase price, so that expression silently carried
the Coinbase-vs-RTI LEVEL BASIS as an error term. Measured against 38,622 true
settlement prints, that basis has sd 18.4bp — LARGER than the typical denominator
(sigma*sqrt(tau) is ~11bp at sigma=5bp/min, tau=5min). The first z was therefore
mostly measuring index basis noise in its numerator.

THE FIX
-------
`strike[t] == expiration_value[t-15min]` EXACTLY (validated 100% on all six series),
so the strike IS the RTI level at the market's open. Estimate RTI at entry by applying
Coinbase's RETURN over the elapsed window to that exact RTI open level:

    RTI_entry ~= strike * (CB_entry / CB_open)
    numerator  = (RTI_entry - strike)/RTI_entry = 1 - CB_open/CB_entry

which is a pure same-source Coinbase return. The level basis cancels identically; only
tracking error over <=15 minutes survives. Denominator is unchanged: sigma*sqrt(tau).

    z = sign * (1 - CB_open/CB_entry) / (sigma * sqrt(secs_left/60))

Interpretation: how far the underlying has moved since this market opened, in units of
how far it can still move before it closes. These are 15-minute UP/DOWN markets -
"is the RTI at close >= the RTI at open" - so that is exactly the right quantity.

Run:  python3 scripts/fetch_spot.py && python3 scripts/fetch_brti.py
      python3 scripts/vol_zscore_brti.py
"""
import os, sys, glob, gzip, csv
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

PROD = {"KXBTC15M":"BTC-USD","KXETH15M":"ETH-USD","KXSOL15M":"SOL-USD",
        "KXDOGE15M":"DOGE-USD","KXBNB15M":"BNB-USD","KXXRP15M":"XRP-USD"}
SLIP, SPLIT = 0.105, "2026-08-01"
RNG = np.random.default_rng(11)

# ── spot + RTI ────────────────────────────────────────────────────────────────
S = {}
for s, p in PROD.items():
    d = pd.read_csv(os.path.join(ROOT,"data",".spot_cache",p+".csv")).drop_duplicates("ts").sort_values("ts")
    g = np.arange(d.ts.min(), d.ts.max()+60, 60)
    d = d.set_index("ts").reindex(g)
    craw = d["close"].values.astype(float)
    lr = np.diff(np.log(craw), prepend=np.nan)
    S[s] = dict(ts=g, c=pd.Series(craw).ffill(limit=10).values,
                clr=np.nancumsum(np.nan_to_num(lr**2)), nlr=np.cumsum(~np.isnan(lr)))
RTI = pd.concat([pd.read_csv(os.path.join(ROOT,"data",".brti_cache",s+".csv")).assign(series=s)
                 for s in PROD if os.path.exists(os.path.join(ROOT,"data",".brti_cache",s+".csv"))])
RTI = RTI.dropna(subset=["open_ts","close_ts"])
OPEN_TS = dict(zip(RTI.ticker, RTI.open_ts.astype(np.int64)))
STRIKE  = dict(zip(RTI.ticker, RTI.strike.astype(float)))

def at(s, ends):
    d = S[s]; j = np.searchsorted(d["ts"], ends, "right") - 1
    ok = (j >= 0) & (j < len(d["ts"]))
    out = np.full(len(ends), np.nan); out[ok] = d["c"][j[ok]]
    return out

def rv60(s, ends, mins=60):
    d = S[s]; j = np.searchsorted(d["ts"], ends, "right") - 1; i = j - mins
    out = np.full(len(ends), np.nan); ok = (i >= 0) & (j >= 0) & (j < len(d["ts"]))
    ii, jj = i[ok], j[ok]; n = d["nlr"][jj] - d["nlr"][ii]
    v = (d["clr"][jj] - d["clr"][ii]) / np.maximum(n, 1)
    out[ok] = np.where(n > mins*0.6, np.sqrt(v), np.nan)
    return out

# ── signals ───────────────────────────────────────────────────────────────────
cfg = B.live_config()
rows = [(se,tk,cts,side,ask,secs,won) for (se,tk,cts,side,ask,secs,won,p1,p2,p3)
        in B.load(series=cfg["series"]) if B.qualifies(cfg,se,side,ask,secs,p1,p2,p3)]
G = pd.DataFrame(rows, columns=["series","ticker","close_ts","side","ask","secs","won"])
G = G.sort_values("secs", ascending=False).drop_duplicates(["ticker","side"])
G["entry_ts"] = (G.close_ts - G.secs).astype(np.int64)
G["open_ts"] = G.ticker.map(OPEN_TS)
G["strike"]  = G.ticker.map(STRIKE)
G = G.dropna(subset=["open_ts","strike"])          # RTI retention is ~67d, archive is longer
G["open_ts"] = G.open_ts.astype(np.int64)
for c in ("cb_entry","cb_open","sig"): G[c] = np.nan
for s, g in G.groupby("series"):
    G.loc[g.index,"cb_entry"] = at(s, g.entry_ts.values)
    G.loc[g.index,"cb_open"]  = at(s, g.open_ts.values)
    G.loc[g.index,"sig"]      = rv60(s, g.entry_ts.values)
sgn = np.where(G.side.values == "no", -1.0, 1.0)
den = G.sig * np.sqrt(G.secs/60.0)
G["z"]     = sgn * (1 - G.cb_open/G.cb_entry) / den            # RTI-consistent
G["z_old"] = sgn * (G.cb_entry - G.strike)/G.cb_entry / den    # the mixed-basis version
G["day"] = pd.to_datetime(G.entry_ts, unit="s", utc=True).dt.strftime("%Y-%m-%d")
G["pnl"] = [B.pnl(bool(w), a, cfg["bet"], SLIP) for w, a in zip(G.won, G.ask)]
G = G[np.isfinite(G.z) & np.isfinite(G.z_old)]
print(f"config {cfg['version']}  signals with RTI+spot={len(G)}  "
      f"{G.day.min()}..{G.day.max()}  slip={SLIP}c")

def alloc(d):
    return pd.concat([g.sort_values("secs",ascending=False).head(cfg["max_conc"])
                      for _, g in d.groupby("close_ts")])
def _cl(df,col):
    g=df.groupby("close_ts")[col].agg(["sum","size"]); return g["sum"].values*1.0,g["size"].values*1.0
def boot(hi,lo,col="pnl",scale=1.0,B_=4000,chunk=400):
    sh,nh=_cl(hi,col); sl,nl=_cl(lo,col); Kh,Kl=len(sh),len(sl); o=np.empty(B_)
    for a in range(0,B_,chunk):
        b=min(chunk,B_-a)
        ih=RNG.integers(0,Kh,(b,Kh)); il=RNG.integers(0,Kl,(b,Kl))
        o[a:a+b]=(sh[ih].sum(1)/nh[ih].sum(1)-sl[il].sum(1)/nl[il].sum(1))*scale
    o.sort(); return o.mean(),o[int(.025*B_)],o[int(.975*B_)],float((o>0).mean())

T = alloc(G)
print(f"modelled trades={len(T)}  WR={T.won.mean()*100:.2f}%  total={T.pnl.sum():+.0f}  $/tr={T.pnl.mean():+.3f}")

print("\n"+"="*76+"\nQUINTILES — mixed-basis z (OLD) vs RTI-consistent z (NEW)\n"+"="*76)
for col,lab in (("z_old","OLD  z = (Coinbase level - RTI strike)/sigma*sqrt(tau)"),
                ("z",    "NEW  z = Coinbase return since open / sigma*sqrt(tau)")):
    d=T.copy(); d["q"]=pd.qcut(d[col],5,labels=False,duplicates="drop")
    print(f"\n  {lab}")
    print(f"    {'q':<3}{'n':>7}{'WR%':>8}{'$/tr':>9}{'total$':>10}{'ask':>7}")
    for q,g in d.groupby("q"):
        print(f"    {int(q)+1:<3}{len(g):>7}{g.won.mean()*100:>8.2f}{g.pnl.mean():>9.3f}{g.pnl.sum():>10.0f}{g.ask.mean():>7.2f}")
    m,l,h,p=boot(d[d.q==d.q.max()],d[d.q==d.q.min()])
    print(f"    top-bottom $/tr {m:+.3f}  95%CI [{l:+.3f},{h:+.3f}]  P(>0)={p:.3f}")

print("\n"+"="*76+f"\nHOLDOUT — cut fitted on <{SPLIT}, scored on >={SPLIT}, slots reused\n"+"="*76)
for col,lab in (("z_old","OLD (mixed basis)"),("z","NEW (RTI-consistent)")):
    cut=G.loc[G.day<SPLIT,col].quantile(0.20)
    print(f"\n  {lab}   cut {col} >= {cut:.3f}")
    for nm,sub in (("FULL",G),("IS",G[G.day<SPLIT]),("HOLDOUT",G[G.day>=SPLIT])):
        days=sub.day.nunique(); b=alloc(sub); g=alloc(sub[sub[col]>=cut])
        print(f"    {nm:<9}{days:>3}d  base {len(b):>5}tr {b.won.mean()*100:>6.2f}% "
              f"{b.pnl.sum()/days:>+7.2f}/day  ->  gated {len(g):>5}tr {g.won.mean()*100:>6.2f}% "
              f"{g.pnl.sum()/days:>+7.2f}/day   delta {g.pnl.sum()-b.pnl.sum():>+6.0f}")
    ho=G[G.day>=SPLIT]; b,g=alloc(ho),alloc(ho[ho[col]>=cut])
    bm,gm=b.groupby("close_ts").pnl.sum(),g.groupby("close_ts").pnl.sum()
    keys=sorted(set(bm.index)|set(gm.index))
    diff=np.array([gm.get(k,0.0)-bm.get(k,0.0) for k in keys]); K=len(diff)
    o=np.sort(diff[RNG.integers(0,K,(4000,K))].sum(1))
    keep=b[b[col]>=cut]; drop=b[b[col]<cut]
    print(f"    HOLDOUT delta {diff.sum():+.0f}  95%CI [{o[100]:+.0f},{o[3900]:+.0f}]  P(>0)={(o>0).mean():.3f}  ({K} clusters)")
    print(f"    dropped trades: n={len(drop)}  WR {drop.won.mean()*100:.2f}%  vs break-even {drop.ask.mean():.2f}c "
          f"= {drop.won.mean()*100-drop.ask.mean():+.2f}pp")
