#!/usr/bin/env python3
"""Do volatility measures correlate with late-certainty performance? (2026-08-27)

TWO ANSWERS, AND THEY POINT OPPOSITE WAYS.

1. Volatility as a LEVEL correlates with nothing. Realized vol (1m/15m/60m/24h),
   Deribit DVOL implied vol and VIX are all flat against daily P&L, daily $/trade and
   daily win rate (every |r| < 0.09, every p > 0.45, n=75 days). Entry-time vol
   quintiles taken WITHIN (series x ask cent) are non-monotonic and their top-bottom
   CI includes zero. This reproduces scripts/vol_bucket_test.py (2026-08-18) on a
   different, EXTERNAL vol measure, so the refutation is not an artifact of using the
   Kalshi price as its own volatility proxy. The one real level effect is on VOLUME,
   not edge: high vol days generate MORE signals (r=+0.30, p=0.008; +0.36 after
   detrending) because more markets transit 88-93c inside the 150-600s window.

2. Volatility as a DENOMINATOR is the largest single predictor in the dataset.
       z = (spot - floor_strike) / (rv60 * sqrt(secs_left))       signed toward the position
   Every input is known at order time. Bottom z quintile: 85.1% WR, -$1.85/trade,
   -$2,929 — i.e. the whole strategy's losses live in it. Top quintile: 97.2% WR,
   +$1.25/trade. Top-bottom +$3.09/tr, cluster-bootstrap CI [+2.54,+3.64].
   Volatility is doing real work in that denominator: the same numerator normalised
   only by time (dist/sqrt(tau)) spreads +2.56, and adding vol widens it to +3.09;
   high-z vs low-z WITHIN a dt quintile is +$1.79/tr, CI [+1.40,+2.20].
   And z is NOT already in the price — inside every single ask cent, high-z minus
   low-z is +$2.22/tr, CI [+1.83,+2.62].

SETTLEMENT BASIS. Kalshi settles on BRTI, this uses Coinbase 1-min closes. Validated:
the rule "YES wins iff spot_close > strike" agrees with actual settlement 100% of the
time when |spot-strike| > 20bp at close, degrading only inside +/-5bp. So z is sound
in the bulk and noisiest exactly where it matters most — see the caveats in the
write-up before trading it.

NOT A DEPLOYMENT RECOMMENDATION. This is a backtest on modelled trades. It needs its
own pre-registration and a live spot feed the trader does not currently have.

Run:  python3 scripts/fetch_spot.py        # ~20 min, caches to data/.spot_cache/
      python3 scripts/vol_zscore_test.py
"""
import os, sys, json, gzip, glob, csv
import numpy as np, pandas as pd
from collections import defaultdict
from scipy import stats

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

CACHE = os.path.join(ROOT, "data", ".spot_cache")
PROD = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD",
        "KXDOGE15M": "DOGE-USD", "KXBNB15M": "BNB-USD", "KXXRP15M": "XRP-USD"}
SLIP = 0.105          # measured live fill gap
SPLIT = "2026-08-01"  # IS / holdout boundary
RNG = np.random.default_rng(11)

# ── spot ──────────────────────────────────────────────────────────────────────
def load_spot():
    S = {}
    for s, p in PROD.items():
        f = os.path.join(CACHE, p + ".csv")
        if not os.path.exists(f):
            sys.exit(f"missing {f} — run python3 scripts/fetch_spot.py first")
        d = pd.read_csv(f).drop_duplicates("ts").sort_values("ts")
        grid = np.arange(d.ts.min(), d.ts.max() + 60, 60)
        d = d.set_index("ts").reindex(grid)
        # RAW close: NaN on a minute with no trade, so a gap yields NaN returns and is
        # EXCLUDED from realized vol instead of counted as a zero return. Without this
        # the thin books (BNB 71% minute coverage, DOGE 91%) measure as artificially calm.
        craw = d["close"].values.astype(float)
        lr = np.diff(np.log(craw), prepend=np.nan)
        S[s] = dict(ts=grid, c=pd.Series(craw).ffill(limit=10).values,
                    clr=np.nancumsum(np.nan_to_num(lr ** 2)), nlr=np.cumsum(~np.isnan(lr)))
    return S

def vol_px(S, s, ends, mins=60):
    """Per-minute realized vol over `mins` ending at each ts, and the spot there."""
    d = S[s]; g = d["ts"]
    j = np.searchsorted(g, ends, "right") - 1
    i = j - mins
    rv = np.full(len(ends), np.nan); px = rv.copy()
    ok = (i >= 0) & (j >= 0) & (j < len(g))
    ii, jj = i[ok], j[ok]
    n = d["nlr"][jj] - d["nlr"][ii]
    v = (d["clr"][jj] - d["clr"][ii]) / np.maximum(n, 1)
    rv[ok] = np.where(n > mins * 0.6, np.sqrt(v), np.nan)
    px[ok] = d["c"][jj]
    return rv, px

# ── every qualifying signal, before the concurrency cap ───────────────────────
def signals(S, cfg):
    strikes = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "data", "candles", "*.csv.gz"))):
        with gzip.open(f, "rt") as fh:
            for r in csv.DictReader(fh):
                try: strikes[r["ticker"]] = float(r["floor_strike"])
                except (ValueError, TypeError): pass
    rows = [(se, tk, cts, side, ask, secs, won)
            for (se, tk, cts, side, ask, secs, won, p1, p2, p3) in B.load(series=cfg["series"])
            if B.qualifies(cfg, se, side, ask, secs, p1, p2, p3)]
    G = pd.DataFrame(rows, columns=["series", "ticker", "close_ts", "side", "ask", "secs", "won"])
    G = G.sort_values("secs", ascending=False).drop_duplicates(["ticker", "side"])
    G["entry_ts"] = (G.close_ts - G.secs).astype(np.int64)
    G["strike"] = G.ticker.map(strikes)
    G["rv60"] = np.nan; G["px"] = np.nan
    for s, g in G.groupby("series"):
        rv, px = vol_px(S, s, g.entry_ts.values)
        G.loc[g.index, ["rv60", "px"]] = np.column_stack([rv, px])
    sgn = np.where(G.side.values == "no", -1.0, 1.0)          # signed TOWARD the position
    G["dist"] = sgn * (G.px - G.strike) / G.px
    G["dt"] = G.dist / np.sqrt(G.secs / 60.0)                  # time-normalised only
    G["z"] = G.dt / G.rv60                                     # time + VOL normalised
    G["day"] = pd.to_datetime(G.entry_ts, unit="s", utc=True).dt.strftime("%Y-%m-%d")
    G["pnl"] = [B.pnl(bool(w), a, cfg["bet"], SLIP) for w, a in zip(G.won, G.ask)]
    return G

# ── cluster bootstrap (close_ts = one settlement = one risk event) ────────────
def _cl(df, col):
    g = df.groupby("close_ts")[col].agg(["sum", "size"])
    return g["sum"].values * 1.0, g["size"].values * 1.0

def boot(hi, lo, col="pnl", scale=1.0, B_=4000, chunk=400):
    sh, nh = _cl(hi, col); sl, nl = _cl(lo, col)
    Kh, Kl = len(sh), len(sl); o = np.empty(B_)
    for a in range(0, B_, chunk):
        b = min(chunk, B_ - a)
        ih = RNG.integers(0, Kh, (b, Kh)); il = RNG.integers(0, Kl, (b, Kl))
        o[a:a+b] = (sh[ih].sum(1)/nh[ih].sum(1) - sl[il].sum(1)/nl[il].sum(1)) * scale
    o.sort()
    return o.mean(), o[int(.025*B_)], o[int(.975*B_)], float((o > 0).mean())

def qtab(T, col, lab, k=5):
    d = T[T[col].notna() & np.isfinite(T[col])].copy()
    d["q"] = pd.qcut(d[col], k, labels=False, duplicates="drop")
    print(f"\n  {lab}   (quintiles of {col})")
    print(f"    {'q':<3}{'n':>7}{'WR%':>8}{'$/tr':>9}{'total$':>10}{'ask':>7}")
    for q, g in d.groupby("q"):
        print(f"    {int(q)+1:<3}{len(g):>7}{g.won.mean()*100:>8.2f}"
              f"{g.pnl.mean():>9.3f}{g.pnl.sum():>10.0f}{g.ask.mean():>7.2f}")
    m, l, h, p = boot(d[d.q == d.q.max()], d[d.q == d.q.min()])
    print(f"    top-bottom $/tr {m:+.3f}  95%CI [{l:+.3f},{h:+.3f}]  P(>0)={p:.3f}  "
          f"{'EXCLUDES 0' if l > 0 or h < 0 else 'includes 0'}")

# ── main ──────────────────────────────────────────────────────────────────────
S = load_spot()
cfg = B.live_config()
G = signals(S, cfg)
print(f"config {cfg['version']}  qualifying signals={len(G)}  "
      f"z known={G.z.notna().sum()} ({G.z.notna().mean()*100:.1f}%)  slip={SLIP}c")

def allocate(d):
    """Two slots per close cluster, earliest signal first — mirrors backtest.simulate."""
    return pd.concat([g.sort_values("secs", ascending=False).head(cfg["max_conc"])
                      for _, g in d.groupby("close_ts")])

T = allocate(G)            # the trades the live config actually takes
T = T[np.isfinite(T.z)]
print(f"modelled trades={len(T)}  WR={T.won.mean()*100:.2f}%  "
      f"total={T.pnl.sum():+.0f}  $/tr={T.pnl.mean():+.3f}")

print("\n" + "="*78 + "\n1. VOLATILITY AS A LEVEL — day and trade\n" + "="*78)
piv = T.pivot_table(index="day", columns="series", values="rv60", aggfunc="mean")
D = T.groupby("day").agg(n=("pnl", "size"), tot=("pnl", "sum"), per_tr=("pnl", "mean"),
                         wr=("won", "mean"))
D["vol"] = piv.mean(axis=1); D = D[D.n >= 20]
print(f"\n  n={len(D)} days.  6-coin mean entry realized vol vs:")
for tgt, lab in (("tot", "daily total $"), ("per_tr", "daily $/trade"),
                 ("wr", "daily win rate"), ("n", "trades/day")):
    r, p = stats.pearsonr(D.vol, D[tgt]); rho, pp = stats.spearmanr(D.vol, D[tgt])
    print(f"    {lab:<18} r={r:+.3f} (p={p:.3f})   rho={rho:+.3f} (p={pp:.3f})")
T2 = T.copy()
T2["sxa"] = T2.series.astype(str) + "|" + pd.cut(
    T2.ask, [87.9, 90.5, 91.5, 92.5, 93.01], labels=list("abcd")).astype(str)
T2["vq"] = T2.groupby("sxa")["rv60"].transform(lambda s: pd.qcut(s, 5, labels=False, duplicates="drop"))
print("\n  entry rv60 quintiles WITHIN (series x ask cent) — removes both confounds")
print(f"    {'q':<3}{'n':>7}{'WR%':>8}{'$/tr':>9}{'rv60 bp/min':>14}")
for q, g in T2.groupby("vq"):
    print(f"    {int(q)+1:<3}{len(g):>7}{g.won.mean()*100:>8.2f}{g.pnl.mean():>9.3f}{g.rv60.mean()*1e4:>14.2f}")
m, l, h, p = boot(T2[T2.vq == T2.vq.max()], T2[T2.vq == T2.vq.min()])
print(f"    top-bottom $/tr {m:+.3f}  95%CI [{l:+.3f},{h:+.3f}]  P(>0)={p:.3f}")

print("\n" + "="*78 + "\n2. VOLATILITY AS A DENOMINATOR — same numerator, three normalisations\n" + "="*78)
for c, l in (("dist", "raw distance to strike      (no time, no vol)"),
             ("dt",   "distance / sqrt(time left)  (time only)"),
             ("z",    "distance / (vol*sqrt(time)) (time + VOL)")):
    qtab(T, c, l)
d = T.copy(); d["dq"] = pd.qcut(d.dt, 5, labels=False)
d["zq"] = d.groupby("dq")["z"].transform(lambda s: pd.qcut(s, 3, labels=False, duplicates="drop"))
m, l, h, p = boot(d[d.zq == 2], d[d.zq == 0])
print(f"\n  vol's OWN contribution — high-z vs low-z WITHIN a dt quintile: "
      f"{m:+.3f}/tr CI[{l:+.3f},{h:+.3f}] P(>0)={p:.3f}")
f = T.copy(); f["askb"] = pd.cut(f.ask, [87.9, 90.5, 91.5, 92.5, 93.01], labels=["88-90", "91", "92", "93"])
f["zq"] = f.groupby("askb", observed=True)["z"].transform(lambda s: pd.qcut(s, 3, labels=False, duplicates="drop"))
print("\n  is z already in the price?  $/trade by ask cent x z tercile")
print(f.pivot_table(index="askb", columns="zq", values="pnl", aggfunc="mean", observed=True).round(3).to_string())
m, l, h, p = boot(f[f.zq == 2], f[f.zq == 0])
print(f"  high-z vs low-z WITHIN ask: {m:+.3f}/tr CI[{l:+.3f},{h:+.3f}] P(>0)={p:.3f}")

print("\n" + "="*78 + f"\n3. HOLDOUT — cut fitted on <{SPLIT}, scored on >={SPLIT}, SLOTS REUSED\n" + "="*78)
cut = G.loc[G.day < SPLIT, "z"].quantile(0.20)
print(f"  drop-rule: z < {cut:.3f} (20th pct of IN-SAMPLE signals)")
print("  Rejected signals free their concurrency slot for the next signal in the same\n"
      "  cluster, so this is a re-simulation, not a filter over a finished trade list.")
for nm, sub in (("FULL   ", G), ("IS     ", G[G.day < SPLIT]), ("HOLDOUT", G[G.day >= SPLIT]),
                ("EXACT-c", G[G.day >= "2026-08-22"])):
    days = sub.day.nunique()
    b = allocate(sub)
    g = allocate(sub[(sub.z >= cut) | sub.z.isna()])
    print(f"  {nm} {days:>3}d  base {len(b):>5}tr {b.won.mean()*100:>6.2f}% {b.pnl.sum():>+7.0f} "
          f"({b.pnl.sum()/days:+6.2f}/day)  ->  gated {len(g):>5}tr {g.won.mean()*100:>6.2f}% "
          f"{g.pnl.sum():>+7.0f} ({g.pnl.sum()/days:+6.2f}/day)   delta {g.pnl.sum()-b.pnl.sum():>+6.0f}")
ho = G[G.day >= SPLIT]
b, g = allocate(ho), allocate(ho[(ho.z >= cut) | ho.z.isna()])
bm, gm = b.groupby("close_ts").pnl.sum(), g.groupby("close_ts").pnl.sum()
keys = sorted(set(bm.index) | set(gm.index))
diff = np.array([gm.get(k, 0.0) - bm.get(k, 0.0) for k in keys]); K = len(diff)
o = np.sort(diff[RNG.integers(0, K, (4000, K))].sum(1))
print(f"\n  HOLDOUT delta {diff.sum():+.0f}  95%CI [{o[100]:+.0f},{o[3900]:+.0f}]  "
      f"P(>0)={(o>0).mean():.3f}  ({K} settlement clusters)")
print("\n  cut sensitivity (full sample, slots reused) — flat, not a knife edge")
base = allocate(G)
print(f"    {'pctile':<8}{'z cut':>8}{'n':>7}{'WR%':>8}{'$/tr':>9}{'vs base $':>11}")
for p_ in (5, 10, 15, 20, 25, 30, 40):
    c = G.z.quantile(p_ / 100)
    r = allocate(G[(G.z >= c) | G.z.isna()])
    print(f"    {p_:<8}{c:>8.3f}{len(r):>7}{r.won.mean()*100:>8.2f}{r.pnl.mean():>9.3f}"
          f"{r.pnl.sum()-base.pnl.sum():>+11.0f}")
print("\n  per-series (full sample, slots reused)")
print(f"    {'series':<12}{'base $/tr':>11}{'gated $/tr':>12}{'delta $':>9}")
for s in sorted(cfg["series"]):
    sg = G[G.series == s]
    b_, g_ = allocate(sg), allocate(sg[(sg.z >= cut) | sg.z.isna()])
    print(f"    {s:<12}{b_.pnl.mean():>11.3f}{g_.pnl.mean():>12.3f}{g_.pnl.sum()-b_.pnl.sum():>9.0f}")
