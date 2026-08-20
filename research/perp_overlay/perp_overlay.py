#!/usr/bin/env python3
"""What happens to P&L if every Kalshi 15M entry is paired with an opposing perp?

YES on a floor market  -> short the perp (both lose if spot falls through K)
NO  on a floor market  -> long  the perp

Entry set is taken from scripts/backtest.py so it matches the live gates exactly
(same qualifies(), same one-entry-per-ticker-side, same max_conc slotting).
Spot comes from Coinbase 1-min closes (fetch_spot.py). The perp is priced as spot;
basis and funding are handled as explicit cost terms, not modelled per-tick.

Resampling unit is the close cluster (CLAUDE.md invariant 3).
"""
import csv, glob, gzip, json, math, os, statistics, sys
from collections import defaultdict

ROOT = "/Users/chrisgarceau/pm"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B          # live_config, qualifies, pnl, FEE

PRODUCT = {"KXBTC15M": "BTC-USD", "KXETH15M": "ETH-USD", "KXSOL15M": "SOL-USD",
           "KXDOGE15M": "DOGE-USD", "KXXRP15M": "XRP-USD", "KXBNB15M": "BNB-USD"}


def load_rows(since=None, until=None):
    """Same dedupe as backtest.load, but keeps floor_strike."""
    out, seen = [], set()
    ip = lambda v: int(v) if v not in ("", "None") else -1
    for path in sorted(glob.glob(os.path.join(ROOT, "data", "candles", "*.csv.gz"))):
        day = os.path.basename(path)[:10]
        if (since and day < since) or (until and day > until):
            continue
        with gzip.open(path, "rt") as f:
            for r in csv.DictReader(f):
                k = (r["ticker"], r["side"], r["candle_idx"])
                if k in seen:
                    continue
                seen.add(k)
                try:
                    strike = float(r["floor_strike"] or 0)
                except ValueError:
                    strike = 0.0
                out.append(dict(series=r["series"], ticker=r["ticker"],
                                close_ts=int(r["close_ts"]), side=r["side"],
                                ask=int(r["ask"]), secs=float(r["secs_left"]),
                                won=r["won"] == "True", p1=ip(r["prior_1"]),
                                p2=ip(r["prior_2"]), p3=ip(r["prior_3"]),
                                strike=strike))
    return out


def pick_entries(rows, cfg):
    """Mirror backtest.simulate's selection, returning full dicts."""
    clusters = defaultdict(list)
    for r in rows:
        clusters[r["close_ts"]].append(r)
    picked = []
    for cts, crows in clusters.items():
        best = {}
        for r in crows:
            if not B.qualifies(cfg, r["series"], r["side"], r["ask"], r["secs"],
                               r["p1"], r["p2"], r["p3"]):
                continue
            k = (r["ticker"], r["side"])
            if k not in best or r["secs"] > best[k]["secs"]:
                best[k] = r
        picked += sorted(best.values(), key=lambda r: -r["secs"])[:cfg["max_conc"]]
    return picked


def load_spot():
    spot = {}
    for series, prod in PRODUCT.items():
        p = os.path.join(HERE, f"spot_{prod}.json")
        if os.path.exists(p):
            spot[series] = {int(k): v for k, v in json.load(open(p)).items()}
    return spot


def bucket(ts):
    return int(ts // 60 * 60)


def build(cfg, since=None, until=None, lagged=True, drop_bnb=False):
    rows = load_rows(since, until)
    spot = load_spot()
    trades, skipped = [], defaultdict(int)
    for r in pick_entries(rows, cfg):
        s = spot.get(r["series"])
        if s is None or (drop_bnb and r["series"] == "KXBNB15M"):
            skipped["no spot product"] += 1
            continue
        t_entry = r["close_ts"] - r["secs"]
        b_entry = bucket(t_entry) - (60 if lagged else 0)
        b_settle = bucket(r["close_ts"]) - 60          # last full minute before close
        pe, ps = s.get(b_entry), s.get(b_settle)
        if pe is None or ps is None or not r["strike"]:
            skipped["missing spot minute"] += 1
            continue
        sign = 1.0 if r["side"] == "yes" else -1.0     # +1 = need spot ABOVE strike
        ret = (ps - pe) / pe
        trades.append(dict(
            cts=r["close_ts"], series=r["series"], side=r["side"], ask=r["ask"],
            secs=r["secs"], won=r["won"], strike=r["strike"], pe=pe, ps=ps, ret=ret,
            sign=sign,
            # distance to the strike at entry, in the direction that wins
            money=sign * (pe - r["strike"]) / pe,
            # hedge return per $1 of perp notional: gains when the market moves
            # against the Kalshi position
            hedge=-sign * ret,
            kalshi=B.pnl(r["won"], r["ask"], cfg["bet"], 0.0),
        ))
    return trades, skipped


def cluster_stats(trades, q, bps):
    per = defaultdict(float)
    cost = q * bps / 10000.0
    for t in trades:
        per[t["cts"]] += t["kalshi"] + q * t["hedge"] - cost
    vals = list(per.values())
    eq = peak = dd = 0.0
    for k in sorted(per):
        eq += per[k]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dict(total=sum(vals), n_cl=len(vals),
                sd=statistics.pstdev(vals) if len(vals) > 1 else 0.0, dd=dd)


def main():
    cfg = B.live_config()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    lagged = "--concurrent" not in sys.argv     # default: no look-ahead on entry px
    since = args[0] if args else None
    trades, skipped = build(cfg, since=since, lagged=lagged,
                            drop_bnb="--drop-bnb" in sys.argv)
    print(f"[entry px = {'close of minute BEFORE entry (no look-ahead)' if lagged else 'close of the entry minute'}]")
    if not trades:
        sys.exit(f"no trades joined to spot: {dict(skipped)}")
    n = len(trades)
    bet = cfg["bet"]
    print(f"live config {cfg['version']} | {n:,} entries joined to spot | "
          f"${bet:.0f}/trade | skipped {dict(skipped)}")

    base = sum(t["kalshi"] for t in trades)
    wr = sum(1 for t in trades if t["won"]) / n * 100
    print(f"\nBASELINE (no perp): {n} trades  {wr:.2f}%WR  "
          f"${base:+,.0f}  ({base/n:+.2f}/tr)")

    # ── moneyness: how far is spot from the strike when we enter? ──────────────
    m = sorted(t["money"] * 10000 for t in trades)          # in bps
    def pct(p): return m[int(p * (len(m) - 1))]
    print(f"\nDISTANCE TO STRIKE AT ENTRY (bps of spot, + = favourable side)")
    print(f"  p10 {pct(.10):7.1f} | p25 {pct(.25):7.1f} | median {pct(.50):7.1f} | "
          f"p75 {pct(.75):7.1f} | p90 {pct(.90):7.1f}   mean {statistics.mean(m):7.1f}")
    wrong = sum(1 for x in m if x < 0) / len(m) * 100
    print(f"  spot already on the LOSING side of the strike at entry: {wrong:.1f}%")

    # ── the hedge leg on its own ──────────────────────────────────────────────
    h = [t["hedge"] for t in trades]
    hw = [t["hedge"] for t in trades if t["won"]]
    hl = [t["hedge"] for t in trades if not t["won"]]
    print(f"\nHEDGE LEG, per $1 of perp notional (bps)")
    print(f"  all trades   mean {statistics.mean(h)*1e4:+7.2f}  sd {statistics.pstdev(h)*1e4:7.2f}")
    print(f"  Kalshi wins  mean {statistics.mean(hw)*1e4:+7.2f}  (n={len(hw)})")
    print(f"  Kalshi loses mean {statistics.mean(hl)*1e4:+7.2f}  (n={len(hl)})")
    print(f"  -> $1 of hedge returns {statistics.mean(h)*1e4:+.2f}bp on average; that mean is"
          f" the drift you are taking on, not a hedge benefit")

    # ── variance-minimising size ──────────────────────────────────────────────
    mk, mh = statistics.mean([t["kalshi"] for t in trades]), statistics.mean(h)
    cov = sum((t["kalshi"] - mk) * (t["hedge"] - mh) for t in trades) / n
    var = statistics.pvariance(h)
    q_var = -cov / var
    # size that would fully cover an average loss
    q_cover = bet / statistics.mean(hl) if hl and statistics.mean(hl) > 0 else float("nan")
    print(f"\nSIZING")
    print(f"  variance-minimising notional : ${q_var:,.0f} per ${bet:.0f} bet "
          f"({q_var/bet:.0f}x the bet)")
    print(f"  notional to fully cover the average loss: ${q_cover:,.0f} "
          f"({q_cover/bet:.0f}x the bet)")

    # ── the grid ──────────────────────────────────────────────────────────────
    print(f"\nP&L BY HEDGE SIZE AND ROUND-TRIP PERP COST")
    print(f"  {'notional':>10} {'x bet':>6} {'gross':>10} {'2bp':>10} "
          f"{'5bp':>10} {'10bp':>10} {'clust sd':>9} {'maxDD':>9} {'BE bp':>7} {'loss cov':>9}")
    b0 = cluster_stats(trades, 0, 0)
    print(f"  {'—':>10} {'0':>6} {base:>+10,.0f} {base:>+10,.0f} "
          f"{base:>+10,.0f} {base:>+10,.0f} {b0['sd']:>9.2f} {b0['dd']:>+9,.0f} "
          f"{'—':>7} {'—':>9}")
    sizes = [50, 250, 500, 1000, round(q_var, -1), 2500, 5000, 10000,
             round(q_cover, -2) if q_cover == q_cover else 20000]
    for q in sorted(set(int(x) for x in sizes if x == x and x > 0)):
        g = cluster_stats(trades, q, 0)
        row = [cluster_stats(trades, q, b)["total"] for b in (2, 5, 10)]
        # round-trip cost (bps) at which the overlay's gross gain/loss is wiped out
        # relative to running unhedged: (gross_hedged - base) / notional
        be = (g["total"] - base) / q * 10000 / n * n / 1.0
        be = (g["total"] - base) / (q * n) * 10000
        covered = sum(1 for t in trades
                      if not t["won"] and q * t["hedge"] >= bet) / max(len(hl), 1) * 100
        star = " <- var-min" if abs(q - q_var) < 15 else ""
        print(f"  {q:>10,} {q/bet:>6.0f} {g['total']:>+10,.0f} "
              + " ".join(f"{v:>+10,.0f}" for v in row)
              + f" {g['sd']:>9.2f} {g['dd']:>+9,.0f} {be:>+7.2f} {covered:>8.0f}%{star}")
    print("  BE bp = round-trip perp cost at which the overlay breaks even vs not "
          "hedging (negative = it never does)")
    print("  loss cov = share of Kalshi losses the perp leg fully offsets")

    print(f"\n  baseline cluster sd ${b0['sd']:.2f}; at var-min size "
          f"${cluster_stats(trades, q_var, 0)['sd']:.2f} "
          f"({(1-cluster_stats(trades,q_var,0)['sd']/b0['sd'])*100:.1f}% lower)")

    # ── by side, to expose the drift term ─────────────────────────────────────
    print(f"\nDRIFT CHECK — the overlay is only a hedge if these cancel")
    for side in ("yes", "no"):
        ss = [t for t in trades if t["side"] == side]
        if ss:
            print(f"  {side.upper():<4} n={len(ss):>5}  mean hedge return "
                  f"{statistics.mean([t['hedge'] for t in ss])*1e4:+7.2f}bp  "
                  f"mean spot move {statistics.mean([t['ret'] for t in ss])*1e4:+7.2f}bp")

    # ── per series, at the var-min size, zero cost ────────────────────────────
    print(f"\nPER SERIES at var-min size, gross of perp cost")
    for se in sorted({t["series"] for t in trades}):
        ss = [t for t in trades if t["series"] == se]
        b = sum(t["kalshi"] for t in ss)
        hq = sum(q_var * t["hedge"] for t in ss)
        print(f"  {se:<10} n={len(ss):>5}  kalshi {b:>+8,.0f}  overlay {hq:>+8,.0f}  "
              f"net {b+hq:>+8,.0f}   hedge sd {statistics.pstdev([t['hedge'] for t in ss])*1e4:>6.1f}bp")


if __name__ == "__main__":
    main()
