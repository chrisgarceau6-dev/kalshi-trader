#!/usr/bin/env python3
"""Liquidity Rewards scanner -- finds markets currently paying to rest
limit orders near the midpoint, independent of directional skill.

WHY THIS IS DIFFERENT FROM THE MM BACKTEST THAT JUST FAILED
-------------------------------------------------------------
That test measured pure spread P&L -- can you profit from the bid-ask
gap itself. It couldn't, because adverse selection ate it (72% of
markets net negative at n=90).

Liquidity Rewards are a separate, additive income stream: Polymarket
pays a daily pUSD reward, funded from its own fee/treasury pool, to
wallets with competitive resting orders near the midpoint -- whether or
not those orders ever fill. This does not require being right about
anything. The risk that remains is: (a) opportunity cost of capital
tied up in resting orders, (b) adverse selection on the FILLS you do
get (smaller separate risk), and (c) event-shock risk if you're
providing on a market that can gap violently (news-driven binaries).

THIS SCANNER
------------
Pulls active markets, reads reward-eligibility fields (min_incentive_size,
max_incentive_spread) plus a rough "event risk" flag (days to resolution
-- short-fused political/news markets are exactly what you want to AVOID,
per the $5k/day Iran-deal market that's one headline from a 90c move).

usage:
    python liquidity_rewards_scan.py --probe
    python liquidity_rewards_scan.py --min-days-to-resolve 14 --top 25
"""
import argparse, time
import requests
import pandas as pd

GAMMA = "https://gamma-api.polymarket.com"


def f(v, d=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


def probe():
    print("pulling a page of active markets to inspect reward fields...")
    r = requests.get(f"{GAMMA}/markets", params={
        "closed": "false", "limit": 20, "order": "volume24hr",
        "ascending": "false"}, timeout=30)
    print(f"status: {r.status_code}")
    if r.status_code != 200:
        print(r.text[:300]); return
    ms = r.json()
    print(f"{len(ms)} markets returned")
    if not ms:
        return
    m = ms[0]
    reward_keys = [k for k in m.keys() if "incentive" in k.lower()
                  or "reward" in k.lower() or "rewards" in k.lower()]
    print(f"\nreward-related keys on a market object: {reward_keys}")
    print(f"\nfull sample of first market's reward fields:")
    for k in reward_keys:
        print(f"  {k}: {m.get(k)}")
    print(f"\nquestion: {m.get('question')}")
    print(f"endDate: {m.get('endDate')}")

    with_rewards = [x for x in ms if f(x.get("rewardsMinSize")) or
                    f(x.get("rewardsMaxSpread")) or x.get("clobRewards")]
    print(f"\n{len(with_rewards)}/{len(ms)} markets in this page show "
          f"any nonzero reward field")


def fetch_markets(pages, min_volume):
    out, offset = [], 0
    for _ in range(pages):
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
        time.sleep(0.15)
    return out


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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--probe", action="store_true")
    p.add_argument("--pages", type=int, default=10)
    p.add_argument("--min-volume", type=float, default=1000.0)
    p.add_argument("--min-days-to-resolve", type=float, default=14.0,
                   help="skip markets resolving sooner than this -- "
                        "avoids the news-shock trap")
    p.add_argument("--top", type=int, default=25)
    p.add_argument("--out", default="liquidity_reward_candidates.csv")
    a = p.parse_args()

    if a.probe:
        probe(); return

    print(f"pulling {a.pages} pages of active markets...")
    markets = fetch_markets(a.pages, a.min_volume)
    print(f"  {len(markets)} markets with volume24hr >= ${a.min_volume:,.0f}")

    rows = []
    for m in markets:
        min_size = f(m.get("rewardsMinSize"))
        max_spread = f(m.get("rewardsMaxSpread"))
        has_rewards = bool(min_size or max_spread or m.get("clobRewards"))
        if not has_rewards:
            continue
        dtr = days_to_resolve(m)
        if dtr is not None and dtr < a.min_days_to_resolve:
            continue
        rows.append({
            "question": str(m.get("question"))[:60],
            "volume24hr": round(f(m.get("volume24hr"), 0), 0),
            "liquidity": round(f(m.get("liquidity"), 0), 0),
            "bestBid": f(m.get("bestBid")),
            "bestAsk": f(m.get("bestAsk")),
            "spread": round((f(m.get("bestAsk"), 0) or 0) -
                            (f(m.get("bestBid"), 0) or 0), 4),
            "rewards_min_size": min_size,
            "rewards_max_spread": max_spread,
            "days_to_resolve": round(dtr, 1) if dtr is not None else None,
        })

    if not rows:
        print("\nno reward-eligible markets found in this pull -- run "
              "--probe to confirm the field names are right, or increase "
              "--pages to pull more of the market list")
        return

    R = pd.DataFrame(rows).sort_values(
        ["rewards_max_spread", "volume24hr"], ascending=[False, False]
    ).head(a.top)
    R.to_csv(a.out, index=False)
    with pd.option_context("display.width", 200, "display.max_colwidth", 62):
        print(f"\n=== top {len(R)} reward-eligible, non-short-fused markets ===")
        print(R.to_string(index=False))
    print(f"\nsaved -> {a.out}")

    print("\n--- READ THIS ---")
    print("rewards_max_spread = how far from midpoint your order can sit and")
    print("still qualify -- wider means easier to earn without getting picked")
    print("off. rewards_min_size = minimum order size to be reward-eligible;")
    print("check this against your $2k bankroll before committing capital.")
    print("days_to_resolve filters out near-term news-shock markets on")
    print("purpose -- steady, long-dated markets are the safer place to")
    print("actually rest capital for this.")


if __name__ == "__main__":
    main()
