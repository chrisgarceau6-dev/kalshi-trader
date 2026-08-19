#!/usr/bin/env python3
"""What Kalshi actually pays for, across the whole published program history.

Reads programs_full.json (see fetch_programs.py / README) and answers:
  - which series get incentives, and how much per day
  - liquidity vs volume split
  - whether the crypto 15M series this account trades are ever incentivized
"""
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
progs = json.loads((HERE / "programs_full.json").read_text())
MINE = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXDOGE15M", "KXBNB15M",
        "KXXRP15M", "KXHYPE15M", "KXNEAR15M", "KXWTI15M"]


def dt(s):
    """Kalshi mixes microsecond precisions (…09.0077Z), which fromisoformat rejects."""
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, tz = rest[:-6], rest[-6:]
        s = f"{head}.{frac.ljust(6, '0')[:6]}{tz}"
    return datetime.fromisoformat(s)


print(f"{len(progs):,} programs\n")
by_type = Counter(p["incentive_type"] for p in progs)
print("by type:", dict(by_type))
dollars = defaultdict(float)
for p in progs:
    dollars[p["incentive_type"]] += p["period_reward"] / 10000
print("total $ published:", {k: f"${v:,.0f}" for k, v in dollars.items()})

start = min(dt(p["start_date"]) for p in progs)
end = max(dt(p["end_date"]) for p in progs)
days = (end - start).total_seconds() / 86400
print(f"window: {start:%Y-%m-%d} -> {end:%Y-%m-%d}  ({days:.0f} days)")
print(f"blended: ${sum(dollars.values())/days:,.0f}/day across the whole exchange\n")

# ── the question that matters for this account ────────────────────────────────
print("=" * 64)
print("DOES THE EXISTING BOT'S UNIVERSE GET INCENTIVES?")
print("=" * 64)
ser = defaultdict(lambda: {"n": 0, "usd": 0.0, "types": Counter()})
for p in progs:
    s = p["market_ticker"].split("-")[0]
    ser[s]["n"] += 1
    ser[s]["usd"] += p["period_reward"] / 10000
    ser[s]["types"][p["incentive_type"]] += 1
for m in MINE:
    d = ser.get(m)
    if not d:
        print(f"  {m:<12} NONE — never incentivized in {len(progs):,} programs")
    else:
        print(f"  {m:<12} {d['n']:>5} programs  ${d['usd']:>9,.0f}  "
              f"${d['usd']/days:>7,.0f}/day  {dict(d['types'])}")

# ── where the money actually is ───────────────────────────────────────────────
print("\n" + "=" * 64)
print("TOP 25 SERIES BY INCENTIVE DOLLARS")
print("=" * 64)
print(f"{'series':<22}{'programs':>9}{'total $':>12}{'$/day':>10}  types")
for s, d in sorted(ser.items(), key=lambda kv: -kv[1]["usd"])[:25]:
    print(f"{s:<22}{d['n']:>9,}{d['usd']:>12,.0f}{d['usd']/days:>10,.0f}  {dict(d['types'])}")

# ── program shape ─────────────────────────────────────────────────────────────
print("\nprogram window minutes:",
      dict(Counter(int((dt(p['end_date'])-dt(p['start_date'])).total_seconds()/60)
                   for p in progs).most_common(8)))
print("reward $ per program  :",
      dict(Counter(round(p['period_reward']/10000) for p in progs).most_common(10)))
print("target size           :", dict(Counter(p.get('target_size_fp') for p in progs).most_common()))
print("discount factor       :", dict(Counter(p.get('discount_factor_bps') for p in progs).most_common()))
