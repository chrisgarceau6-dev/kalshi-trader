#!/usr/bin/env python3
"""Evaluate the hourly crypto ladders against the live 15M gates.

The question that stopped this going live in August was multi-strike stacking:
188 strikes settle on ONE BRTI print, and the strikes are nested (BTC > 72,499
implies BTC > 72,399), so two strikes in one close is not diversification, it is
leverage on a single event. That is measured here explicitly.
"""
import csv, gzip, os, sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = "/Users/chrisgarceau/pm"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import backtest as B

cfg = B.live_config()
BET = cfg["bet"]


def load(path):
    rows = []
    ip = lambda v: int(v) if v not in ("", "None") else -1
    with gzip.open(path, "rt") as f:
        for r in csv.DictReader(f):
            rows.append(dict(series=r["series"], ticker=r["ticker"],
                             cts=int(r["close_ts"]), side=r["side"],
                             ask=int(r["ask"]), secs=float(r["secs_left"]),
                             won=r["won"] == "True", p1=ip(r["prior_1"]),
                             p2=ip(r["prior_2"]), p3=ip(r["prior_3"]),
                             strike=float(r["floor_strike"])))
    return rows


def qualifying(rows):
    """First qualifying candle per (ticker, side) — same rule as the harness."""
    best = {}
    for r in rows:
        if not B.qualifies(cfg, r["series"], r["side"], r["ask"], r["secs"],
                           r["p1"], r["p2"], r["p3"]):
            continue
        k = (r["ticker"], r["side"])
        if k not in best or r["secs"] > best[k]["secs"]:
            best[k] = r
    return list(best.values())


def summarize(tag, trades, days):
    if not trades:
        print(f"  {tag:<34} no qualifying trades")
        return
    pl = [B.pnl(t["won"], t["ask"], BET, 0.0) for t in trades]
    wr = sum(1 for t in trades if t["won"]) / len(trades) * 100
    print(f"  {tag:<34} n={len(trades):>5} {wr:>7.2f}%WR {sum(pl)/len(pl):>+7.2f}/tr "
          f"{sum(pl):>+9,.0f} {sum(pl)/days:>+8.2f}/day")


all_rows = []
for s in ("KXBTCD", "KXETHD"):
    p = os.path.join(HERE, f"hourly_{s}.csv.gz")
    if os.path.exists(p):
        all_rows += load(p)
if not all_rows:
    sys.exit("no hourly archive built yet")

span = (max(r["cts"] for r in all_rows) - min(r["cts"] for r in all_rows)) / 86400
print(f"{len(all_rows):,} archived rows | "
      f"{len({r['cts'] for r in all_rows})} hourly closes | {span:.1f} days")
print(f"live gates: ask [{cfg['min_ask']},{cfg['max_ask']}]c  "
      f"secs [{cfg['min_secs']},{cfg['max_secs']}]  prior>={cfg['prior_min']}x"
      f"{cfg['lookback']}  ${BET:.0f}/trade\n")

print("=" * 84)
print("A. DOES THE LADDER EVEN QUALIFY? and how often do strikes stack")
print("=" * 84)
for s in ("KXBTCD", "KXETHD"):
    rows = [r for r in all_rows if r["series"] == s]
    if not rows:
        continue
    q = qualifying(rows)
    closes = len({r["cts"] for r in rows})
    per_close = defaultdict(list)
    for t in q:
        per_close[t["cts"]].append(t)
    dist = defaultdict(int)
    for cts, ts in per_close.items():
        dist[len(ts)] += 1
    print(f"\n  {s}: {closes} closes, {len(q)} qualifying entries in "
          f"{len(per_close)} closes ({len(per_close)/closes*100:.0f}% of hours)")
    print("    entries per close: " + "  ".join(
        f"{k}->{dist[k]}" for k in sorted(dist)))
    summarize("    all qualifying", q, span)
    # first strike only vs the extras
    firsts, extras = [], []
    for cts, ts in per_close.items():
        ts = sorted(ts, key=lambda r: -r["secs"])
        firsts.append(ts[0])
        extras += ts[1:]
    summarize("    first strike per close only", firsts, span)
    summarize("    the stacked extras", extras, span)

print("\n" + "=" * 84)
print("B. NESTED-STRIKE CORRELATION — do stacked entries win/lose together?")
print("=" * 84)
both = defaultdict(list)
for t in qualifying(all_rows):
    both[(t["cts"], t["series"])].append(t)
multi = {k: v for k, v in both.items() if len(v) > 1}
if multi:
    agree = sum(1 for v in multi.values()
                if len({x["won"] for x in v}) == 1)
    allwin = sum(1 for v in multi.values() if all(x["won"] for x in v))
    alllose = sum(1 for v in multi.values() if not any(x["won"] for x in v))
    print(f"  {len(multi)} closes had 2+ qualifying strikes")
    print(f"  identical outcome across strikes: {agree} ({agree/len(multi)*100:.0f}%)"
          f"   all win {allwin}   all lose {alllose}")
    worst = min((sum(B.pnl(x["won"], x["ask"], BET, 0) for x in v), k)
                for k, v in multi.items())
    print(f"  worst single close: ${worst[0]:+,.0f} "
          f"({len(multi[worst[1]])} strikes, "
          f"{datetime.fromtimestamp(worst[1][0], timezone.utc):%Y-%m-%d %H:%MZ})")
else:
    print("  no close ever had 2+ qualifying strikes")

print("\n" + "=" * 84)
print("C. COLLISION WITH THE LIVE 15M BOOK — same BRTI print, doubled exposure")
print("=" * 84)
q = qualifying(all_rows)
top = [t for t in q if t["cts"] % 3600 == 0]
print(f"  {len(top)}/{len(q)} hourly entries ({len(top)/max(len(q),1)*100:.0f}%) settle "
      f"exactly on the hour, which is also a 15M close on the same underlying.")
print("  Those are not two bets. They are one BRTI print, and MAX_CONCURRENT=2")
print("  would not see them as related.")

print("\n" + "=" * 84)
print("D. 15M BASELINE OVER THE SAME CALENDAR WINDOW, for scale")
print("=" * 84)
lo = datetime.fromtimestamp(min(r["cts"] for r in all_rows), timezone.utc).strftime("%Y-%m-%d")
hi = datetime.fromtimestamp(max(r["cts"] for r in all_rows), timezone.utc).strftime("%Y-%m-%d")
rows15 = B.load(since=lo, until=hi)
pc, tr = B.simulate(rows15, cfg, 0.0)
print(f"  15M {lo} -> {hi}: {len(tr):,} trades  ${sum(pc.values()):+,.0f}  "
      f"{sum(pc.values())/span:+.2f}/day")
