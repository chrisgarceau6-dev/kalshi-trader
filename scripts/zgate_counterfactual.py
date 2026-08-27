#!/usr/bin/env python3
"""What would the z-gate have done to the REAL ACCOUNT?

Everything else about the z-gate is measured on MODELLED trades from the archive.
This scores it on actual fills and actual settlements — the money that actually
moved — using the real fill price, the real fill timestamp, and the real outcome.

P&L formula is daily_summary.py's: revenue - cost - fee, per settlement.

The counterfactual is DROP-ONLY: a skipped trade's concurrency slot is NOT refilled,
because nothing in the fill record says what the bot would have taken instead. On the
archive that refill was worth +$182 of +$891, so this understates the gate by roughly
a fifth and is the conservative direction.
"""
import os, sys, math, json, time
from datetime import datetime, timezone
import numpy as np, pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from kalshi_auth import get as kget

SERIES = ["KXBTC15M","KXETH15M","KXSOL15M","KXDOGE15M","KXBNB15M","KXXRP15M"]
PROD = {"KXBTC15M":"BTC-USD","KXETH15M":"ETH-USD","KXSOL15M":"SOL-USD",
        "KXDOGE15M":"DOGE-USD","KXBNB15M":"BNB-USD","KXXRP15M":"XRP-USD"}
Z_CUT = 0.761          # fitted in-sample on the archive, NOT on this data

def page(path, key, params=None, maxp=40):
    out, cur, params = [], None, dict(params or {})
    for _ in range(maxp):
        p = dict(params, limit=200)
        if cur: p["cursor"] = cur
        c, r = kget(path, p)
        if c != 200: break
        b = r.get(key, [])
        out += b
        cur = r.get("cursor")
        if not cur or not b: break
        time.sleep(0.05)
    return out

print("fetching settlements + fills ...", file=sys.stderr)
S = pd.DataFrame(page("/portfolio/settlements", "settlements"))
F = pd.DataFrame(page("/portfolio/fills", "fills"))
S["series"] = S.ticker.str.split("-").str[0]
F["series"] = F.ticker.str.split("-").str[0]
S = S[S.series.isin(SERIES)].copy()
F = F[F.series.isin(SERIES)].copy()

S["revenue"] = S.revenue.astype(float)/100.0
S["cost"] = S.yes_total_cost_dollars.astype(float) + S.no_total_cost_dollars.astype(float)
S["fee"] = S.fee_cost.astype(float)
S["pnl"] = S.revenue - S.cost - S.fee
S["won"] = S.revenue > 0.01
S["settled_ts"] = pd.to_datetime(S.settled_time, format="mixed", utc=True).astype("int64")//10**9
S = S[S.cost > 0.01]

# entry = EARLIEST fill on the ticker; that is the decision the gate would have seen
F["ts"] = F.ts.astype(np.int64)
F["px"] = np.where(F.outcome_side.eq("yes"), F.yes_price_dollars.astype(float),
                                             F.no_price_dollars.astype(float))*100
ent = F.sort_values("ts").groupby("ticker").agg(
    entry_ts=("ts","first"), side=("outcome_side","first"), ask=("px","first"),
    nfills=("ts","size")).reset_index()
D = S.merge(ent, on="ticker", how="left")
print(f"settlements={len(S)}  matched to a fill={D.entry_ts.notna().sum()}", file=sys.stderr)

# strike from the RTI cache; close_ts from it too
RTI = pd.concat([pd.read_csv(os.path.join(ROOT,"data",".brti_cache",s+".csv")) for s in SERIES])
D = D.merge(RTI[["ticker","strike","close_ts"]], on="ticker", how="left")

# spot + sigma at the real fill instant
SP = {}
for s,p in PROD.items():
    d = pd.read_csv(os.path.join(ROOT,"data",".spot_cache",p+".csv")).drop_duplicates("ts").sort_values("ts")
    g = np.arange(d.ts.min(), d.ts.max()+60, 60); d = d.set_index("ts").reindex(g)
    craw = d["close"].values.astype(float); lr = np.diff(np.log(craw), prepend=np.nan)
    SP[s] = dict(ts=g, c=pd.Series(craw).ffill(limit=10).values,
                 clr=np.nancumsum(np.nan_to_num(lr**2)), nlr=np.cumsum(~np.isnan(lr)))
D["spot"]=np.nan; D["sigma"]=np.nan
for s,g in D.dropna(subset=["entry_ts"]).groupby("series"):
    d=SP[s]; e=g.entry_ts.values.astype(np.int64)
    j=np.searchsorted(d["ts"],e,"right")-1; i=j-60
    ok=(i>=0)&(j>=0)&(j<len(d["ts"]))
    sp=np.full(len(g),np.nan); sg=sp.copy()
    ii,jj=i[ok],j[ok]; n=d["nlr"][jj]-d["nlr"][ii]
    v=(d["clr"][jj]-d["clr"][ii])/np.maximum(n,1)
    sg[ok]=np.where(n>36,np.sqrt(v),np.nan); sp[ok]=d["c"][jj]
    D.loc[g.index,["spot","sigma"]]=np.column_stack([sp,sg])

D["secs_left"] = D.close_ts - D.entry_ts
sgn = np.where(D.side.values=="no",-1.0,1.0)
D["z"] = sgn*(D.spot-D.strike)/D.spot/(D.sigma*np.sqrt(D.secs_left/60.0))
D["day"] = pd.to_datetime(D.settled_ts, unit="s", utc=True).dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
D["in_band"] = D.ask.between(88,93)
D.to_csv(os.path.join(ROOT,"data",".zgate_counterfactual.csv"), index=False)

known = D[D.z.notna() & np.isfinite(D.z)]
print(f"z computable on {len(known)}/{len(D)} ({len(known)/len(D)*100:.1f}%)  "
      f"window {D.day.min()} .. {D.day.max()} ET", file=sys.stderr)

def block(name, df):
    if not len(df): return
    drop = df[(df.z < Z_CUT) & df.z.notna()]
    keep = df.drop(drop.index)
    print(f"\n{name}   n={len(df)}  {df.day.min()}..{df.day.max()}")
    print(f"  {'':<26}{'trades':>8}{'WR%':>8}{'P&L':>11}{'$/trade':>10}{'fees':>9}")
    for lab, r in (("ACTUAL (what you ran)", df), ("WITH z-gate (drop only)", keep),
                   ("  -> what it skips", drop)):
        if not len(r): continue
        print(f"  {lab:<26}{len(r):>8}{r.won.mean()*100:>8.2f}{r.pnl.sum():>+11.2f}"
              f"{r.pnl.mean():>+10.3f}{r.fee.sum():>9.2f}")
    print(f"  {'DELTA':<26}{-len(drop):>+8}{keep.won.mean()*100-df.won.mean()*100:>+8.2f}"
          f"{keep.pnl.sum()-df.pnl.sum():>+11.2f}{keep.pnl.mean()-df.pnl.mean():>+10.3f}")

print("\n" + "="*72)
print("REAL ACCOUNT — actual fills, actual settlements, actual outcomes")
print("="*72)
block("ALL six live series", known)
block("Entries inside the 88-93c band only", known[known.in_band])
# sizing regimes: bet ran $75 -> $50 -> $25, so dollars are not comparable across them
print("\n" + "="*72 + "\nBY SIZING REGIME (bet $75 -> $50 -> $25; dollars are NOT comparable across)\n" + "="*72)
for lab, lo, hi in (("$75  (..Aug 18)","2000-01-01","2026-08-18"),
                    ("$50  (Aug 19-21)","2026-08-19","2026-08-21"),
                    ("$25  (Aug 23..)","2026-08-23","2099-01-01")):
    block(lab, known[(known.day>=lo)&(known.day<=hi)])
