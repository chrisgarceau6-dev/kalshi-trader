#!/usr/bin/env python3
"""Does prior-candle price dispersion predict outcomes? (pre-registered, 2026-08-18)

H1: at a fixed ask, higher dispersion -> lower win rate.
Buckets by vol = max(ask,p1,p2,p3) - min(ask,p1,p2,p3) in cents.
IS = 2026-06-11..07-31, HOLDOUT = 2026-08-01..08-17.

Result: H1 REFUTED. WR is non-monotonic (MID is worst in both windows) and the
cluster-bootstrapped HIGH-LOW delta includes zero in both. High-dispersion entries
are 66% of volume and 77% of holdout profit, so excluding them removes most of the
P&L. High dispersion means the ask is transiting 90-93c toward 100c as certainty
resolves (Invariant 6), which is the strategy working, not a danger signal.

Run: python3 scripts/vol_bucket_test.py
"""
import gzip, csv, glob, math, collections, statistics, random
random.seed(11)
MIN_ASK, MAX_ASK, MIN_S, MAX_S, PMIN = 90, 93, 150, 600, 75
SPLIT = "2026-08-01"

def load():
    ent = {}
    for f in sorted(glob.glob("data/candles/*.csv.gz")):
        day = f.split("/")[-1][:10]
        with gzip.open(f, "rt") as fh:
            for r in csv.DictReader(fh):
                try:
                    ask = int(r["ask"]); secs = float(r["secs_left"])
                    p1, p2, p3 = int(r["prior_1"]), int(r["prior_2"]), int(r["prior_3"])
                except (ValueError, TypeError):
                    continue
                if not (MIN_ASK <= ask <= MAX_ASK and MIN_S <= secs <= MAX_S): continue
                if p1 < PMIN or p2 < PMIN: continue
                if ask <= 91 and p3 < 80: continue
                k = (r["ticker"], r["side"])
                if k in ent and ent[k]["secs"] >= secs: continue
                ent[k] = {"day": day, "ask": ask, "secs": secs, "won": r["won"] == "True",
                          "vol": max(ask,p1,p2,p3) - min(ask,p1,p2,p3), "clu": r["close_ts"]}
    return list(ent.values())

def pnl(e):
    c = 7500.0 / e["ask"]
    fee = math.ceil(0.07 * c * (e["ask"]/100) * (1-e["ask"]/100)) / 100.0
    return (c*(100-e["ask"])/100.0 - fee) if e["won"] else -(c*e["ask"]/100.0)

def buck(v): return "LOW" if v <= 4 else ("MID" if v <= 9 else "HIGH")

def clusters(rs):
    c = collections.defaultdict(list)
    for e in rs: c[e["clu"]].append(e)
    return list(c.values())

def bootdiff(a, b, B=3000):
    ca, cb = clusters(a), clusters(b); out = []
    for _ in range(B):
        x = [e for g in (random.choice(ca) for _ in ca) for e in g]
        y = [e for g in (random.choice(cb) for _ in cb) for e in g]
        out.append(sum(e["pnl"] for e in x)/len(x) - sum(e["pnl"] for e in y)/len(y))
    out.sort(); return statistics.mean(out), out[int(.025*B)], out[int(.975*B)]

E = load()
for e in E: e["pnl"] = pnl(e)
for nm, rs in (("IN-SAMPLE", [e for e in E if e["day"] < SPLIT]),
               ("HOLDOUT",   [e for e in E if e["day"] >= SPLIT])):
    print(f"\n{nm}  (n={len(rs)})")
    print(f"  {'bucket':<7}{'n':>7}{'WR':>9}{'$/trade':>10}{'total':>10}")
    for b in ("LOW", "MID", "HIGH"):
        s = [e for e in rs if buck(e["vol"]) == b]
        if not s: continue
        w = sum(e["won"] for e in s); p = sum(e["pnl"] for e in s)
        print(f"  {b:<7}{len(s):>7}{w/len(s)*100:>8.2f}%{p/len(s):>10.3f}{p:>10.0f}")
    lo = [e for e in rs if buck(e["vol"])=="LOW"]; hi = [e for e in rs if buck(e["vol"])=="HIGH"]
    m, l, h = bootdiff(hi, lo)
    print(f"  HIGH-LOW {m:+.3f}/trade  95%CI [{l:+.3f},{h:+.3f}]  "
          f"{'includes 0' if l < 0 < h else 'EXCLUDES 0'}")
    tot = sum(e["pnl"] for e in rs); hp = sum(e["pnl"] for e in hi)
    print(f"  HIGH supplies {hp/tot*100:.0f}% of profit on {len(hi)/len(rs)*100:.0f}% of volume")
