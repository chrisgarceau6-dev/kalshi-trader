#!/usr/bin/env python3
"""Full Polymarket rewards universe scan.

Pulls every open market, extracts the daily reward dollar rate from
clobRewards, computes expected daily $ at your capital level assuming
proportional share, and ranks by capital efficiency.

This is what lets us build a portfolio hitting $150-250+/week.
"""
import argparse, json, time, sys
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"
CLOB  = "https://clob.polymarket.com"


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def jparse(v, d=None):
    if v is None:
        return d
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return d


def daily_rate(clob_rewards):
    """Pull rewardsDailyRate from the clobRewards blob."""
    if not clob_rewards:
        return None
    if isinstance(clob_rewards, list):
        for cr in clob_rewards:
            if isinstance(cr, dict):
                v = f(cr.get("rewardsDailyRate"))
                if v is not None:
                    return v
    elif isinstance(clob_rewards, dict):
        return f(clob_rewards.get("rewardsDailyRate"))
    return None


def days_to_resolve(m):
    end = m.get("endDate") or m.get("endDateIso")
    if not end:
        return None
    try:
        dt = pd.Timestamp(end)
        if dt.tzinfo is None:
            dt = dt.tz_localize("UTC")
        now = pd.Timestamp.utcnow()
        if now.tzinfo is None:
            now = now.tz_localize("UTC")
        return (dt - now).total_seconds() / 86400
    except Exception:
        return None


def clob_book_depth(token_id, mid, band):
    """Sum of qualifying bid+ask notional within [mid-band, mid+band]."""
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=15)
        if r.status_code != 200:
            return None, None
        j = r.json()
        bid_qual = sum(float(b["size"]) for b in (j.get("bids") or [])
                       if float(b["price"]) >= mid - band)
        ask_qual = sum(float(a["size"]) for a in (j.get("asks") or [])
                       if float(a["price"]) <= mid + band)
        return bid_qual, ask_qual
    except Exception:
        return None, None


def fetch_all_markets(pages=40, min_volume=100):
    out, offset = [], 0
    for i in range(pages):
        r = requests.get(f"{GAMMA}/markets", params={
            "closed": "false", "limit": 100, "offset": offset,
            "order": "volume24hr", "ascending": "false"}, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        out += [m for m in batch if f(m.get("volume24hr"), 0) >= min_volume]
        offset += 100
        time.sleep(0.1)
        if (i+1) % 5 == 0:
            print(f"  pulled {len(out)} markets ({offset} scanned)...", file=sys.stderr)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pages",        type=int, default=40)
    p.add_argument("--min-volume",   type=float, default=100)
    p.add_argument("--min-days",     type=float, default=3.0,
                   help="min days to resolve (short-fused = shock risk)")
    p.add_argument("--capital",      type=float, default=100.0,
                   help="capital per market in USD (both sides combined)")
    p.add_argument("--no-depth",     action="store_true",
                   help="skip CLOB depth pull (fast mode, uses default share)")
    p.add_argument("--assumed-share", type=float, default=0.10,
                   help="fallback share estimate when depth not pulled")
    p.add_argument("--top",          type=int, default=40)
    p.add_argument("--out",          default="rewards_universe.csv")
    a = p.parse_args()

    print(f"pulling up to {a.pages} pages of open markets...")
    ms = fetch_all_markets(a.pages, a.min_volume)
    print(f"  {len(ms)} markets with vol24h >= ${a.min_volume:.0f}")

    rows = []
    for m in ms:
        cr = jparse(m.get("clobRewards"), [])
        dr = daily_rate(cr)
        if not dr or dr <= 0:
            continue
        dtr = days_to_resolve(m)
        if dtr is not None and dtr < a.min_days:
            continue

        min_size   = f(m.get("rewardsMinSize"))
        max_spread = f(m.get("rewardsMaxSpread"))
        bid        = f(m.get("bestBid"))
        ask        = f(m.get("bestAsk"))
        if not (bid and ask and min_size and max_spread):
            continue
        mid = (bid + ask) / 2
        band = max_spread / 100.0 if max_spread > 1 else max_spread

        # capital per unit (min qualifying quote on both sides)
        cap_per_unit = min_size * bid + min_size * (1 - ask)
        n_units      = max(1, int(a.capital / cap_per_unit)) if cap_per_unit > 0 else 0
        my_shares    = n_units * min_size

        # depth check for realistic share estimate
        bid_qual, ask_qual = (None, None)
        if not a.no_depth:
            toks = jparse(m.get("clobTokenIds"), [])
            if toks:
                bid_qual, ask_qual = clob_book_depth(toks[0], mid, band)
                time.sleep(0.05)

        if bid_qual is not None and ask_qual is not None:
            tot_qual = (bid_qual + ask_qual) / 2  # avg side depth
            share    = my_shares / (my_shares + tot_qual) if tot_qual > 0 else 1.0
        else:
            share = a.assumed_share

        expected_daily = dr * share
        weekly         = expected_daily * 7

        rows.append({
            "question":     str(m.get("question"))[:52],
            "daily_pool":   round(dr, 2),
            "vol24h":       round(f(m.get("volume24hr"), 0)),
            "liq":          round(f(m.get("liquidity"), 0)),
            "mid":          round(mid, 4),
            "spread_c":     round((ask - bid) * 100, 2),
            "max_c":        round(band * 100, 2),
            "min_size":     int(min_size),
            "dtr":          round(dtr, 1) if dtr else None,
            "bid_qual":     int(bid_qual) if bid_qual is not None else None,
            "ask_qual":     int(ask_qual) if ask_qual is not None else None,
            "cap_used":     round(n_units * cap_per_unit, 2),
            "share_pct":    round(share * 100, 2),
            "daily_$":      round(expected_daily, 2),
            "weekly_$":     round(weekly, 2),
            "wk_ROC_%":     round(weekly / (n_units * cap_per_unit) * 100, 1)
                            if cap_per_unit else 0,
        })

    if not rows:
        print("no reward-paying markets matched. relax filters (--min-days 0)")
        return

    R = pd.DataFrame(rows).sort_values("weekly_$", ascending=False)
    R.to_csv(a.out, index=False)

    print(f"\n=== TOP {min(a.top, len(R))} REWARD MARKETS "
          f"(cap=${a.capital}/market, share from live book depth) ===")
    with pd.option_context("display.width", 260, "display.max_colwidth", 48):
        print(R.head(a.top).to_string(index=False))

    print(f"\n=== PORTFOLIO SUMMARY (top {a.top}) ===")
    top = R.head(a.top)
    print(f"  markets:        {len(top)}")
    print(f"  total capital:  ${top['cap_used'].sum():,.2f}")
    print(f"  daily expected: ${top['daily_$'].sum():,.2f}")
    print(f"  weekly:         ${top['weekly_$'].sum():,.2f}")
    print(f"  vs. $250/wk goal: {top['weekly_$'].sum()/250*100:.0f}%")
    print(f"\nsaved -> {a.out}")


if __name__ == "__main__":
    main()
