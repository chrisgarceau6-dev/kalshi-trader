#!/usr/bin/env python3
"""Shared layer for the perp/spot research program. See PREREG.md.

Joins the Kalshi candle archive to 1-min Coinbase spot and builds the features
every hypothesis needs. Selection mirrors scripts/backtest.py exactly so any
result maps onto live P&L.

Cache: features_cache.pkl (delete to rebuild).
"""
import csv, glob, gzip, json, math, os, pickle, sys
from collections import defaultdict

import numpy as np

ROOT = "/Users/chrisgarceau/pm"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

PRODUCT = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD",
           "KXDOGE15M": "DOGE-USD", "KXXRP15M": "XRP-USD", "KXBNB15M": "BNB-USD"}
IS_END, OOS_START = "2026-07-31", "2026-08-01"
VOL_WINDOW = 60          # trailing 1-min returns for sigma (pre-registered)
AVG_SECS = 60            # settlement is a 60s BRTI average
NORM = lambda z: 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ── data ──────────────────────────────────────────────────────────────────────

def load_spot():
    out = {}
    for series, prod in PRODUCT.items():
        p = os.path.join(HERE, f"spot_{prod}.json")
        if os.path.exists(p):
            out[series] = {int(k): v for k, v in json.load(open(p)).items()}
    return out


def load_archive():
    rows, seen = [], set()
    ip = lambda v: int(v) if v not in ("", "None") else -1
    for path in sorted(glob.glob(os.path.join(ROOT, "data", "candles", "*.csv.gz"))):
        day = os.path.basename(path)[:10]
        with gzip.open(path, "rt") as f:
            for r in csv.DictReader(f):
                k = (r["ticker"], r["side"], r["candle_idx"])
                if k in seen:
                    continue
                seen.add(k)
                try:
                    strike = float(r["floor_strike"] or 0)
                except ValueError:
                    continue
                if not strike:
                    continue
                rows.append(dict(day=day, series=r["series"], ticker=r["ticker"],
                                 cts=int(r["close_ts"]), side=r["side"],
                                 ask=int(r["ask"]), secs=float(r["secs_left"]),
                                 won=r["won"] == "True", strike=strike,
                                 p1=ip(r["prior_1"]), p2=ip(r["prior_2"]),
                                 p3=ip(r["prior_3"])))
    return rows


def _sigma(sm, m0, n=VOL_WINDOW):
    """sd of trailing n one-minute log returns ending at minute m0."""
    px = [sm.get(m0 - 60 * i) for i in range(n + 1)]
    rets = [math.log(px[i] / px[i + 1]) for i in range(n)
            if px[i] and px[i + 1] and px[i] > 0 and px[i + 1] > 0]
    if len(rets) < int(0.7 * n):
        return None
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1))


def build(force=False):
    cache = os.path.join(HERE, "features_cache.pkl")
    if os.path.exists(cache) and not force:
        return pickle.load(open(cache, "rb"))

    spot, rows = load_spot(), load_archive()
    btc = spot.get("KXBTC15M", {})
    out, skip = [], defaultdict(int)
    sig_cache, btc_sig_cache = {}, {}

    for r in rows:
        sm = spot.get(r["series"])
        if sm is None:
            skip["no product"] += 1
            continue
        m_entry = int((r["cts"] - r["secs"]) // 60 * 60) - 60      # no look-ahead
        m_conc = m_entry + 60
        m_settle = int(r["cts"] // 60 * 60) - 60
        pe, pc, ps = sm.get(m_entry), sm.get(m_conc), sm.get(m_settle)
        if pe is None or ps is None:
            skip["missing spot"] += 1
            continue

        key = (r["series"], m_entry)
        if key not in sig_cache:
            sig_cache[key] = _sigma(sm, m_entry)
        sig = sig_cache[key]
        if not sig or sig <= 0:
            skip["no sigma"] += 1
            continue

        sign = 1.0 if r["side"] == "yes" else -1.0
        tau_eff = max(r["secs"] - AVG_SECS * 2.0 / 3.0, 30.0)
        denom = sig * math.sqrt(tau_eff / 60.0)
        d = sign * math.log(pe / r["strike"])
        z = d / denom

        def mom(k):
            pk = sm.get(m_entry - 60 * k)
            if not pk:
                return None
            return -sign * math.log(pe / pk) / (sig * math.sqrt(k))

        bkey = m_entry
        if bkey not in btc_sig_cache:
            btc_sig_cache[bkey] = _sigma(btc, m_entry)
        bsig = btc_sig_cache[bkey]
        bpe, bp3 = btc.get(m_entry), btc.get(m_entry - 180)
        btc_m3 = None
        if bsig and bpe and bp3:
            btc_m3 = -sign * math.log(bpe / bp3) / (bsig * math.sqrt(3))

        out.append(dict(
            day=r["day"], series=r["series"], ticker=r["ticker"], cts=r["cts"],
            side=r["side"], ask=r["ask"], secs=r["secs"], won=r["won"],
            strike=r["strike"], p1=r["p1"], p2=r["p2"], p3=r["p3"],
            sign=sign, pe=pe, pc=pc, ps=ps, sigma=sig, z=z, tau_eff=tau_eff,
            p_model=NORM(z), edge_model=NORM(z) - r["ask"] / 100.0,
            d_bps=d * 1e4, m1=mom(1), m3=mom(3), m5=mom(5), btc_m3=btc_m3,
            ret=(ps - pe) / pe, hedge=-sign * (ps - pe) / pe,
            # concurrent-minute z, as the upper bound a live bot could see
            z_conc=(sign * math.log(pc / r["strike"]) / denom) if pc else None,
        ))
    print(f"built {len(out):,} candidate rows  skipped {dict(skip)}", file=sys.stderr)
    pickle.dump(out, open(cache, "wb"))
    return out


# ── selection: mirrors backtest.simulate ──────────────────────────────────────

def select(cands, cfg, rank="secs", extra=None):
    """One entry per (ticker,side), earliest signal wins, capped at max_conc."""
    clusters = defaultdict(list)
    for r in cands:
        clusters[r["cts"]].append(r)
    picked = []
    for cts, crows in clusters.items():
        best = {}
        for r in crows:
            if not B.qualifies(cfg, r["series"], r["side"], r["ask"], r["secs"],
                               r["p1"], r["p2"], r["p3"]):
                continue
            if extra and not extra(r):
                continue
            k = (r["ticker"], r["side"])
            if k not in best or r["secs"] > best[k]["secs"]:
                best[k] = r
        pool = list(best.values())
        keyf = {"secs": lambda r: -r["secs"], "z": lambda r: -r["z"]}[rank]
        picked += sorted(pool, key=keyf)[:cfg["max_conc"]]
    return picked


def pnl(t, bet, slip=0.0):
    return B.pnl(t["won"], t["ask"], bet, slip)


def split(trades):
    return ([t for t in trades if t["day"] <= IS_END],
            [t for t in trades if t["day"] >= OOS_START])


# ── stats ─────────────────────────────────────────────────────────────────────

def cluster_boot(trades, valfn, iters=3000, seed=7):
    """Bootstrap the MEAN of valfn over trades, resampling close clusters."""
    per = defaultdict(list)
    for t in trades:
        per[t["cts"]].append(valfn(t))
    keys = list(per)
    sums = np.array([sum(per[k]) for k in keys], dtype=float)
    cnts = np.array([len(per[k]) for k in keys], dtype=float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(keys), size=(iters, len(keys)))
    tot, n = sums[idx].sum(1), cnts[idx].sum(1)
    d = np.sort(tot / np.maximum(n, 1))
    return float(sums.sum() / max(cnts.sum(), 1)), float(d[int(.0125 * iters)]), \
        float(d[int(.9875 * iters)])


def boot_diff(a, b, valfn, iters=3000, seed=7):
    """CI on mean(a) - mean(b), resampling clusters jointly."""
    pa, pb = defaultdict(list), defaultdict(list)
    for t in a:
        pa[t["cts"]].append(valfn(t))
    for t in b:
        pb[t["cts"]].append(valfn(t))
    keys = sorted(set(pa) | set(pb))
    sa = np.array([sum(pa.get(k, [])) for k in keys], float)
    na = np.array([len(pa.get(k, [])) for k in keys], float)
    sb = np.array([sum(pb.get(k, [])) for k in keys], float)
    nb = np.array([len(pb.get(k, [])) for k in keys], float)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(keys), size=(iters, len(keys)))
    d = np.sort(sa[idx].sum(1) / np.maximum(na[idx].sum(1), 1)
                - sb[idx].sum(1) / np.maximum(nb[idx].sum(1), 1))
    obs = sa.sum() / max(na.sum(), 1) - sb.sum() / max(nb.sum(), 1)
    return float(obs), float(d[int(.0125 * iters)]), float(d[int(.9875 * iters)]), \
        float((d > 0).mean())


def buckets(trades, keyf, n=5):
    """Split into n equal-count buckets by keyf; returns [(lo,hi,[trades])]."""
    ok = [t for t in trades if keyf(t) is not None]
    ok.sort(key=keyf)
    out, step = [], len(ok) / n
    for i in range(n):
        chunk = ok[int(i * step):int((i + 1) * step)]
        if chunk:
            out.append((keyf(chunk[0]), keyf(chunk[-1]), chunk))
    return out


def edge_pp(t):
    return (1.0 if t["won"] else 0.0) - t["ask"] / 100.0


def report(name, trades, keyf, bet=50.0, n=5):
    """Bucket table + top-vs-bottom CI, IS and OOS."""
    print(f"\n{'='*78}\n{name}\n{'='*78}")
    for tag, ss in (("IN-SAMPLE  (Jun 11 - Jul 31)", split(trades)[0]),
                    ("HOLDOUT    (Aug 1 - Aug 19)", split(trades)[1])):
        bs = buckets(ss, keyf, n)
        if not bs:
            print(f"{tag}: no data")
            continue
        print(f"\n{tag}   n={len(ss):,}")
        print(f"  {'bucket':>18} {'n':>6} {'WR%':>7} {'edge pp':>9} {'$/tr':>8} {'total$':>9}")
        for lo, hi, ch in bs:
            wr = sum(1 for t in ch if t["won"]) / len(ch) * 100
            ep = sum(edge_pp(t) for t in ch) / len(ch) * 100
            pl = [pnl(t, bet) for t in ch]
            print(f"  [{lo:>7.2f},{hi:>7.2f}] {len(ch):>6} {wr:>7.2f} {ep:>+9.2f} "
                  f"{sum(pl)/len(pl):>+8.2f} {sum(pl):>+9,.0f}")
        obs, lo_, hi_, pgt = boot_diff(bs[-1][2], bs[0][2], edge_pp)
        print(f"  top-minus-bottom edge: {obs*100:+.2f}pp  "
              f"CI [{lo_*100:+.2f}, {hi_*100:+.2f}]  P(>0)={pgt:.3f}")


if __name__ == "__main__":
    build(force="--force" in sys.argv)
