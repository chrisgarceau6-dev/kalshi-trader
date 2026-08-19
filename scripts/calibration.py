#!/usr/bin/env python3
"""Three structural checks on the 15M markets, from the archive.

  A. Calibration — does an X-cent ask win X% of the time? (the band is 88-96c;
     the archive does not keep anything outside it, so this is not a full curve)
  B. Variance collapse — settlement is a 60-second average of BRTI, so the
     distribution tightens faster near expiry than a point-estimate model implies.
     If that is mispriced, the edge should grow as secs_left falls.
  C. Index basis — the strike IS a BRTI print (window start), so strike vs
     Coinbase at the same minute measures the basis the bot's filters ignore.

All confidence intervals are cluster-robust: the seven series settle simultaneously,
so a close timestamp is one observation, never seven (CLAUDE.md invariant 3).
"""
import csv, gzip, json, math, statistics, time, urllib.request
from collections import defaultdict
from pathlib import Path

ARCHIVE = Path(__file__).resolve().parent.parent / "data" / "candles"
CACHE = Path(__file__).resolve().parent.parent / "data" / ".btc_spot_cache.json"

rows = []
for p in sorted(ARCHIVE.glob("*.csv.gz")):
    for r in csv.DictReader(gzip.open(p, "rt")):
        try:
            rows.append({
                "series": r["series"], "close_ts": int(r["close_ts"]),
                "ask": float(r["ask"]), "secs": int(r["secs_left"]),
                "won": r["won"] in ("True", "true", "1"),
                "strike": float(r["floor_strike"] or 0), "side": r["side"],
            })
        except (TypeError, ValueError):
            continue

print(f"{len(rows):,} archived observations, "
      f"{len({r['close_ts'] for r in rows}):,} close clusters, "
      f"{len({r['series'] for r in rows})} series\n")


def cluster_ci(items, key=lambda r: r["won"] - r["ask"] / 100):
    """Mean and 95% CI treating each close cluster as one observation."""
    per = defaultdict(list)
    for r in items:
        per[r["close_ts"]].append(key(r))
    means = [statistics.mean(v) for v in per.values()]
    if len(means) < 3:
        return None
    m = statistics.mean(means)
    se = statistics.stdev(means) / math.sqrt(len(means))
    return m, m - 1.96 * se, m + 1.96 * se, len(means)


# ── A. calibration ────────────────────────────────────────────────────────────
print("A. CALIBRATION — realized win rate vs the price paid")
print(f"{'ask':>5} | {'n':>7} | {'realized WR':>11} | {'edge (WR - ask)':>26}")
print("-" * 58)
for cent in range(88, 97):
    sub = [r for r in rows if int(r["ask"]) == cent]
    if len(sub) < 200:
        continue
    wr = sum(r["won"] for r in sub) / len(sub) * 100
    ci = cluster_ci(sub)
    star = "  <-- significant" if ci and (ci[1] > 0 or ci[2] < 0) else ""
    print(f"{cent:>4}c | {len(sub):>7,} | {wr:>10.2f}% | "
          f"{ci[0]*100:>+7.2f}pp [{ci[1]*100:+.2f},{ci[2]*100:+.2f}]{star}")

# ── B. variance collapse ──────────────────────────────────────────────────────
print("\nB. VARIANCE COLLAPSE — edge by time to expiry (90-93c entries only)")
print(f"{'secs left':>12} | {'n':>7} | {'edge':>28}")
print("-" * 55)
band = [r for r in rows if 90 <= r["ask"] <= 93]
for lo, hi in [(100, 150), (150, 240), (240, 360), (360, 480), (480, 600), (600, 800)]:
    sub = [r for r in band if lo <= r["secs"] < hi]
    ci = cluster_ci(sub)
    if not ci:
        continue
    star = "  <-- significant" if ci[1] > 0 or ci[2] < 0 else ""
    print(f"{lo:>5}-{hi:<6}s | {len(sub):>7,} | "
          f"{ci[0]*100:>+7.2f}pp [{ci[1]*100:+.2f},{ci[2]*100:+.2f}]{star}")

# ── C. index basis ────────────────────────────────────────────────────────────
btc = {r["close_ts"]: r["strike"] for r in rows if r["series"] == "KXBTC15M" and r["strike"]}
if CACHE.exists():
    spot = {int(k): v for k, v in json.loads(CACHE.read_text()).items()}
else:
    spot, step = {}, 300 * 60
    t, hi = min(btc) - 1200, max(btc) + 180
    while t < hi:
        url = ("https://api.exchange.coinbase.com/products/BTC-USD/candles"
               f"?granularity=60&start={t}&end={min(t+step, hi)}")
        req = urllib.request.Request(url, headers={"User-Agent": "basis-probe/1.0"})
        try:
            for r in json.loads(urllib.request.urlopen(req, timeout=20).read()):
                spot[int(r[0])] = float(r[4])
        except Exception:
            pass
        t += step
        time.sleep(0.12)
    CACHE.write_text(json.dumps(spot))

basis = []
for close_ts, strike in btc.items():
    s = spot.get((close_ts - 960) // 60 * 60)   # candle ending at the window start
    if s:
        basis.append((strike - s) / s * 10000)

print(f"\nC. INDEX BASIS — BRTI strike vs Coinbase at the same minute (n={len(basis):,})")
if basis:
    basis.sort()
    print(f"  median {statistics.median(basis):+.2f}bp   mean {statistics.mean(basis):+.2f}bp   "
          f"sd {statistics.stdev(basis):.2f}bp")
    print(f"  p05 {basis[int(.05*len(basis))]:+.2f}bp   p95 {basis[int(.95*len(basis))]:+.2f}bp   "
          f"|basis|>10bp in {sum(1 for b in basis if abs(b) > 10)/len(basis)*100:.1f}% of windows")
