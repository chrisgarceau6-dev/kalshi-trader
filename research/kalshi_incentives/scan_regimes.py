#!/usr/bin/env python3
"""Classify live liquidity programs into the two regimes that matter.

  REAL   — reference prices sum near 100c: a genuine two-sided market. Quoting here
           means quoting at the touch, with real fill and adverse-selection risk.
  PENNY  — both sides' reference prices sit in the low cents against a wide spread
           and little or no volume. Resting bids there are nearly unfillable (a YES
           bid at 2c plus a NO bid at 3c would be a 95c arbitrage for anyone selling
           into them), so the rewards are collected with almost no trading risk.

Outputs the pool available in each regime and the competing capital already parked.
"""
import json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.elections.kalshi.com/trade-api/v2"
HERE = Path(__file__).resolve().parent
N = int(sys.argv[1]) if len(sys.argv) > 1 else 600


def get(p):
    req = urllib.request.Request(f"{API}/{p}", headers={"User-Agent": "incentive-research/1.0"})
    for a in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.6 * (a + 1))
    return {}


def dt(s):
    s = s.replace("Z", "+00:00")
    if "." in s:
        h, rest = s.split(".", 1)
        f, tz = rest[:-6], rest[-6:]
        s = f"{h}.{f.ljust(6,'0')[:6]}{tz}"
    return datetime.fromisoformat(s)


progs, cursor = [], None
while True:
    r = get("incentive_programs?status=active&type=liquidity&limit=1000"
            + (f"&cursor={cursor}" if cursor else ""))
    b = r.get("incentive_programs", [])
    if not b:
        break
    progs.extend(b)
    cursor = r.get("next_cursor")
    if not cursor:
        break

now = datetime.now(timezone.utc)
live = {}
for p in progs:
    try:
        if dt(p["start_date"]) <= now <= dt(p["end_date"]):
            t = p["market_ticker"]
            if t not in live or p["period_reward"] > live[t]["period_reward"]:
                live[t] = p
    except Exception:
        pass
sample = sorted(live.values(), key=lambda p: -p["period_reward"])[:N]
print(f"{len(live):,} live programs; inspecting top {len(sample)} by pool", file=sys.stderr)

rows = []
for i, p in enumerate(sample):
    tk = p["market_ticker"]
    b = (get(f"markets/{tk}/orderbook") or {}).get("orderbook_fp") or {}
    m = (get(f"markets/{tk}") or {}).get("market", {})
    time.sleep(0.08)
    target = float(p.get("target_size_fp") or 0)
    disc = (p.get("discount_factor_bps") or 5000) / 10000
    win_h = (dt(p["end_date"]) - dt(p["start_date"])).total_seconds() / 3600
    pool_h = (p["period_reward"] / 10000) / win_h if win_h else 0

    def side(nm):
        lv = sorted([(round(float(x[0]) * 100, 2), float(x[1]))
                     for x in (b.get(f"{nm}_dollars") or [])], key=lambda x: -x[0])
        if not lv:
            return None, 0.0, 0.0, 0.0
        cum, ref = 0.0, lv[-1][0]
        for pr, sz in lv:
            cum += sz
            if cum >= target / 5:
                ref = pr
                break
        raw = sum(sz * (disc ** max(0, int(round(ref - pr)))) for pr, sz in lv)
        cap = sum(sz * pr / 100 for pr, sz in lv if pr >= ref)   # capital parked at/above ref
        return ref, raw, sum(s for _, s in lv), cap

    yr, yraw, ydep, ycap = side("yes")
    nr, nraw, ndep, ncap = side("no")
    rows.append(dict(ticker=tk, pool_h=pool_h, target=target,
                     y_ref=yr, n_ref=nr, y_raw=yraw, n_raw=nraw,
                     y_dep=ydep, n_dep=ndep, field_cap=ycap + ncap,
                     two_sided=(ydep >= target and ndep >= target),
                     vol=float(m.get("volume_fp") or 0),
                     oi=float(m.get("open_interest_fp") or 0),
                     spread=(float(m.get("yes_ask_dollars") or 0)
                             - float(m.get("yes_bid_dollars") or 0)) * 100))
    if (i + 1) % 100 == 0:
        print(f"  ...{i+1}/{len(sample)}", file=sys.stderr)

(HERE / "regimes.json").write_text(json.dumps(rows, indent=1))
print(f"wrote regimes.json ({len(rows)} markets)")
