#!/usr/bin/env python3
"""What the Volume Incentive Program is actually worth to the existing 15M bot.

Rules (help.kalshi.com/incentive-programs/volume-incentive-program):
  reward = (your volume / total volume in the program window) x pool
  capped at $0.005 per contract you traded
  qualifying price range $0.03-$0.97 — the bot trades 90-93c, inside it
  makers and takers both count; automated traders are eligible

So the payout per contract is  min(0.005, pool / total_volume).
The cap only binds when the pool exceeds $0.005 x total volume — otherwise the
pool is diluted across everyone else's volume and the cap is irrelevant.
"""
import json, time, urllib.request
from collections import defaultdict
from pathlib import Path
from statistics import median

API = "https://api.elections.kalshi.com/trade-api/v2"
HERE = Path(__file__).resolve().parent
CONTRACTS_PER_TRADE = 80        # $75 bet at ~92c
TRADES_PER_DAY = 140            # measured 2026-08-18 (CLAUDE.md dated observations)


def get(path, params=""):
    req = urllib.request.Request(f"{API}/{path}?{params}",
                                 headers={"User-Agent": "incentive-research/1.0"})
    for a in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(1.2 * (a + 1))
    return {}


def settled_volumes(series, want=60):
    """Contract volume of recently settled markets in a series."""
    out, cursor = [], None
    while len(out) < want:
        p = f"series_ticker={series}&status=settled&limit=200"   # settled = finalized here
        if cursor:
            p += f"&cursor={cursor}"
        r = get("markets", p)
        ms = r.get("markets", [])
        if not ms:
            break
        for m in ms:
            # Kalshi returns fixed-point strings; plain `volume` is absent on 15M
            v = m.get("volume_fp") or m.get("volume")
            if v:
                out.append(float(v))
        cursor = r.get("next_cursor")
        if not cursor:
            break
    return out[:want]


progs = json.loads((HERE / "programs_full.json").read_text())
vol_progs = defaultdict(list)
for p in progs:
    if p["incentive_type"] == "volume":
        vol_progs[p["market_ticker"].split("-")[0]].append(p)

print("VOLUME INCENTIVE — what the bot would actually collect\n")
print(f"{'series':<12}{'programs':>9}{'pool $ median':>15}{'mkt volume median':>19}"
      f"{'$/contract':>12}{'cap binds?':>12}")
print("-" * 79)

rows = []
for series in ("KXBTC15M", "KXETH15M"):
    ps = vol_progs.get(series, [])
    if not ps:
        print(f"{series:<12} no volume programs")
        continue
    pools = [p["period_reward"] / 10000 for p in ps]
    vols = settled_volumes(series)
    if not vols:
        print(f"{series:<12} {len(ps):>9} pools ${median(pools):>8,.2f}   (no volume data)")
        continue
    pool, vol = median(pools), median(vols)
    per_contract = min(0.005, pool / vol) if vol else 0
    rows.append((series, pool, vol, per_contract))
    print(f"{series:<12}{len(ps):>9,}{pool:>15,.2f}{vol:>19,.0f}"
          f"{per_contract:>12.5f}{'YES' if pool/vol > 0.005 else 'no':>12}")

print("\nper-trade and per-day value to the bot (80 contracts/trade):")
print(f"{'series':<12}{'$/trade':>10}{'$/day if ALL trades were this series':>40}")
for series, pool, vol, pc in rows:
    print(f"{series:<12}{pc*CONTRACTS_PER_TRADE:>10.3f}"
          f"{pc*CONTRACTS_PER_TRADE*TRADES_PER_DAY:>40.2f}")

if rows:
    # the bot spreads across 7 series; only 2 are incentivized
    avg_pc = sum(r[3] for r in rows) / len(rows)
    share_incentivized = 2 / 7
    daily = avg_pc * CONTRACTS_PER_TRADE * TRADES_PER_DAY * share_incentivized
    print(f"\nrealistic: only 2 of 7 traded series are incentivized")
    print(f"  blended $/contract      : ${avg_pc:.5f}")
    print(f"  blended $/trade         : ${avg_pc*CONTRACTS_PER_TRADE:.3f}")
    print(f"  expected $/day           : ${daily:.2f}")
    print(f"  vs strategy target       : $6.50 profit per winning trade")
    print(f"  vs Kalshi fee per trade  : ~$0.45 (0.07 x 80 x 0.92 x 0.08)")
