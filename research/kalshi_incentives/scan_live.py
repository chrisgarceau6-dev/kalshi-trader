#!/usr/bin/env python3
"""Rank every currently-live Kalshi liquidity incentive by what it would actually pay.

Implements the published LIP scoring against live order books:

  raw score(order) = size x discount_factor ** ticks_below_reference_price
  reference price  = walking down from best bid, the first level where cumulative
                     resting size reaches target_size / 5
  snapshot score   = your share of yes-side raw score + your share of no-side
  reward           = (your score / all scores) x pool x (counted snapshots / all)

Both sides must independently meet target size or the snapshot pays nobody, so the
scanner reports each side's depth against target as a hard gate.

    python3 scan_live.py            # top 25 by $/hour per $100 of capital
    python3 scan_live.py --all      # every live program
    python3 scan_live.py --quote 200  # assume we add 200 contracts per side
"""
import argparse, json, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.elections.kalshi.com/trade-api/v2"
HERE = Path(__file__).resolve().parent


def get(path, params=""):
    req = urllib.request.Request(f"{API}/{path}?{params}",
                                 headers={"User-Agent": "incentive-research/1.0"})
    for a in range(3):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except Exception:
            time.sleep(0.8 * (a + 1))
    return {}


def dt(s):
    s = s.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, tz = rest[:-6], rest[-6:]
        s = f"{head}.{frac.ljust(6, '0')[:6]}{tz}"
    return datetime.fromisoformat(s)


def active_liquidity():
    out, cursor = [], None
    while True:
        p = "status=active&type=liquidity&limit=1000" + (f"&cursor={cursor}" if cursor else "")
        r = get("incentive_programs", p)
        b = r.get("incentive_programs", [])
        if not b:
            break
        out.extend(b)
        cursor = r.get("next_cursor")
        if not cursor:
            break
        time.sleep(0.05)
    return out


def side_levels(book, side):
    """(price_cents, size) for one side's resting bids, best first."""
    raw = (book.get("orderbook_fp") or {}).get(f"{side}_dollars", []) or []
    lv = []
    for level in raw:
        try:
            lv.append((round(float(level[0]) * 100, 2), float(level[1])))
        except (IndexError, TypeError, ValueError):
            continue
    lv.sort(key=lambda x: -x[0])
    return lv


def score_side(levels, target, discount):
    """Reference price, total raw score on the book, and depth at/above reference."""
    if not levels:
        return None, 0.0, 0.0
    need, cum, ref = target / 5.0, 0.0, levels[-1][0]
    for price, size in levels:
        cum += size
        if cum >= need:
            ref = price
            break
    raw, at_ref = 0.0, 0.0
    for price, size in levels:
        ticks = max(0, int(round(ref - price)))
        raw += size * (discount ** ticks)
        if price >= ref:
            at_ref += size
    return ref, raw, at_ref


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quote", type=float, default=200, help="contracts we would rest per side")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--limit", type=int, default=140, help="markets to inspect (API budget)")
    args = ap.parse_args()

    progs = active_liquidity()
    now = datetime.now(timezone.utc)
    live = {}
    for p in progs:
        try:
            if not (dt(p["start_date"]) <= now <= dt(p["end_date"])):
                continue
        except Exception:
            continue
        t = p["market_ticker"]
        # one row per market: keep the richest concurrent pool
        if t not in live or p["period_reward"] > live[t]["period_reward"]:
            live[t] = p
    print(f"{len(progs):,} active-status programs -> {len(live):,} running right now "
          f"({now:%Y-%m-%d %H:%M}Z)\n", file=sys.stderr)

    ranked = sorted(live.values(), key=lambda p: -p["period_reward"])[:args.limit]
    rows = []
    for i, p in enumerate(ranked):
        tk = p["market_ticker"]
        book = get(f"markets/{tk}/orderbook")
        mkt = (get(f"markets/{tk}") or {}).get("market", {})
        time.sleep(0.12)
        target = float(p.get("target_size_fp") or 0)
        disc = (p.get("discount_factor_bps") or 5000) / 10000
        hours = max((dt(p["end_date"]) - now).total_seconds() / 3600, 1e-9)
        pool = p["period_reward"] / 10000
        window_h = (dt(p["end_date"]) - dt(p["start_date"])).total_seconds() / 3600
        pool_per_h = pool / window_h if window_h else 0

        yes_lv, no_lv = side_levels(book, "yes"), side_levels(book, "no")
        y_ref, y_raw, y_at = score_side(yes_lv, target, disc)
        n_ref, n_raw, n_at = score_side(no_lv, target, disc)
        y_depth = sum(s for _, s in yes_lv)
        n_depth = sum(s for _, s in no_lv)
        two_sided = y_depth >= target and n_depth >= target

        # our share if we rest `quote` at the reference price on both sides
        q = args.quote
        share = 0.0
        if y_raw + q > 0 and n_raw + q > 0:
            share = (q / (y_raw + q) + q / (n_raw + q)) / 2
        # capital: a yes bid at r costs r, a no bid at (100-r) costs the complement
        cap = q * ((y_ref or 50) + (n_ref or 50)) / 100 if (y_ref or n_ref) else q
        usd_h = pool_per_h * share
        per_100 = usd_h / (cap / 100) if cap else 0

        rows.append(dict(ticker=tk, pool=pool, pool_h=pool_per_h, hours_left=hours,
                         target=target, disc=disc, y_depth=y_depth, n_depth=n_depth,
                         two_sided=two_sided, y_ref=y_ref, n_ref=n_ref,
                         y_raw=y_raw, n_raw=n_raw, share=share, cap=cap,
                         usd_h=usd_h, per_100=per_100,
                         vol=float(mkt.get("volume_fp") or 0),
                         oi=float(mkt.get("open_interest_fp") or 0),
                         spread=(float(mkt.get("yes_ask_dollars") or 0)
                                 - float(mkt.get("yes_bid_dollars") or 0)) * 100))
        if (i + 1) % 25 == 0:
            print(f"  ...{i+1}/{len(ranked)}", file=sys.stderr)

    (HERE / "scan_live.json").write_text(json.dumps(rows, indent=1))
    rows.sort(key=lambda r: -r["per_100"])
    shown = rows if args.all else rows[:25]
    print(f"{'ticker':<34}{'pool/h':>8}{'2-sided':>9}{'yes/no depth vs target':>24}"
          f"{'share':>8}{'$/h':>8}{'capital':>9}{'$/h per $100':>14}")
    print("-" * 114)
    for r in shown:
        depth = "%.0f/%.0f vs %.0f" % (r["y_depth"], r["n_depth"], r["target"])
        print(f"{r['ticker'][:33]:<34}{r['pool_h']:>8.2f}"
              f"{'YES' if r['two_sided'] else 'no':>9}{depth:>24}"
              f"{r['share']*100:>7.1f}%{r['usd_h']:>8.2f}{r['cap']:>9.0f}{r['per_100']:>14.2f}")

    qual = [r for r in rows if r["two_sided"]]
    print(f"\n{len(qual)}/{len(rows)} inspected markets currently have enough two-sided "
          f"depth for ANY snapshot to pay out.")
    if qual:
        print(f"best $/h per $100 capital: {max(r['per_100'] for r in qual):.2f}")


if __name__ == "__main__":
    main()
