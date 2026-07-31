#!/usr/bin/env python3
"""WTI $100 crude oil — liquidity rewards sizing analysis.

WHAT THIS DOES
--------------
1. Finds the WTI $100 market on Polymarket and reads its reward params.
2. Pulls the live CLOB order book for real depth at each price level.
3. Computes required capital per N-share unit and implied ROC at several
   daily reward assumptions.
4. Prints a concrete quote placement plan given the parameters you already
   found: bid/ask 0.355-0.375, rewardsMaxSpread 4.5c, rewardsMinSize 100.

COLLATERAL MODEL (important)
-----------------------------
On Polymarket's binary CLOB, resting orders require full collateral:
  - Bid at P for N shares  -> N * P locked (buying YES tokens)
  - Ask at P for N shares  -> N * (1-P) locked (selling YES = long NO)
Both legs lock capital simultaneously until filled or cancelled.

FILL RISK
---------
Every fill is adverse selection by definition — the taker hit you because
they think the probability moved. For a macro market like WTI crude you
have no informational edge on directional moves. The rewards are the ONLY
reason to be here; the fill risk is the cost of collecting them. The
question is whether the daily reward rate justifies that cost.

usage:
    python wti_rewards_size.py                  # live market + book data
    python wti_rewards_size.py --daily-reward 5 # override reward assumption
    python wti_rewards_size.py --sizes 100 200 500
"""
import argparse, json, time
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


def find_wti_market(pages=20):
    """Search for WTI / crude oil markets with rewards set."""
    hits, offset = [], 0
    keywords = ("wti", "crude oil", "oil price", "barrel")
    for _ in range(pages):
        r = requests.get(f"{GAMMA}/markets", params={
            "closed": "false", "limit": 100, "offset": offset,
            "order": "volume24hr", "ascending": "false"}, timeout=30)
        if r.status_code != 200:
            break
        batch = r.json()
        if not batch:
            break
        for m in batch:
            q = str(m.get("question") or "").lower()
            if any(k in q for k in keywords):
                hits.append(m)
        offset += 100
        time.sleep(0.15)
    return hits


def clob_book(token_id):
    """Return (bids, asks) lists of {price, size} sorted best-first."""
    try:
        r = requests.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=20)
        if r.status_code != 200:
            return [], []
        j = r.json()
        bids = sorted([{"price": float(b["price"]), "size": float(b["size"])}
                       for b in (j.get("bids") or [])], key=lambda x: -x["price"])
        asks = sorted([{"price": float(a["price"]), "size": float(a["size"])}
                       for a in (j.get("asks") or [])], key=lambda x: x["price"])
        return bids, asks
    except Exception:
        return [], []


def depth_at(levels, n_shares):
    """Total shares available within the first n_shares on one side."""
    total = 0
    for lvl in levels:
        if total >= n_shares:
            break
        total += lvl["size"]
    return min(total, n_shares)


def rewards_from_market(m):
    """Extract daily reward info from a market object."""
    clob_rewards = jparse(m.get("clobRewards"), [])
    if not clob_rewards and m.get("clobRewards"):
        clob_rewards = m.get("clobRewards")

    daily_usd = None
    if isinstance(clob_rewards, list) and clob_rewards:
        # Polymarket clobRewards is typically: [{rewardsDailyRate, ...}, ...]
        for cr in clob_rewards:
            if isinstance(cr, dict):
                v = f(cr.get("rewardsDailyRate") or cr.get("dailyRate")
                      or cr.get("rate") or cr.get("amount"))
                if v is not None:
                    daily_usd = v
                    break
    elif isinstance(clob_rewards, dict):
        daily_usd = f(clob_rewards.get("rewardsDailyRate")
                      or clob_rewards.get("dailyRate"))

    return {
        "daily_usd": daily_usd,
        "min_size": f(m.get("rewardsMinSize")),
        "max_spread": f(m.get("rewardsMaxSpread")),
        "raw": clob_rewards,
    }


def sizing_table(bid, ask, daily_reward, sizes):
    """Print ROC at each size level given capital locked on both sides."""
    mid = (bid + ask) / 2
    print(f"\n=== sizing table  (bid={bid:.3f}  ask={ask:.3f}  mid={mid:.3f}) ===")
    print(f"{'shares':>8}  {'bid_cap':>9}  {'ask_cap':>9}  {'total_cap':>10}  "
          f"{'daily_reward':>13}  {'daily_ROC':>10}  {'ann_ROC':>9}")
    print("-" * 78)
    for n in sizes:
        bid_cap  = n * bid
        ask_cap  = n * (1 - ask)
        total    = bid_cap + ask_cap
        daily_rc = daily_reward / total if total > 0 else 0
        ann_rc   = daily_rc * 365
        print(f"{n:>8,}  ${bid_cap:>8,.2f}  ${ask_cap:>8,.2f}  ${total:>9,.2f}  "
              f"  ${daily_reward:>11,.4f}  {daily_rc*100:>9.3f}%  {ann_rc*100:>8.1f}%")
    print()
    print("bid_cap   = capital locked posting the bid side (YES tokens)")
    print("ask_cap   = capital locked posting the ask side (NO collateral)")
    print("total_cap = both sides simultaneously locked")
    print("daily_ROC and ann_ROC assume rewards are split across ALL providers")
    print("proportionally — if you're the only provider your share is higher.")


def quote_plan(bid, ask, max_spread, min_size):
    """Print concrete quote placement relative to reward eligibility window."""
    mid = (bid + ask) / 2
    elig_bid_floor = mid - max_spread
    elig_ask_ceil  = mid + max_spread

    print(f"\n=== quote placement plan ===")
    print(f"live mid:              {mid:.4f}")
    print(f"rewardsMaxSpread:      {max_spread:.4f}c  "
          f"(band: [{elig_bid_floor:.4f}, {elig_ask_ceil:.4f}])")
    print(f"rewardsMinSize:        {min_size:.0f} shares")
    print()
    print(f"current book spread:   {ask - bid:.4f}  ({(ask-bid)*100:.2f}c)")
    print(f"  your bid at {bid:.3f}:  {mid - bid:.4f}c from mid  "
          f"[{'ELIGIBLE' if mid - bid <= max_spread else 'INELIGIBLE'}]")
    print(f"  your ask at {ask:.3f}:  {ask - mid:.4f}c from mid  "
          f"[{'ELIGIBLE' if ask - mid <= max_spread else 'INELIGIBLE'}]")
    print()
    print("FILL MANAGEMENT OPTIONS")
    print("  A. Flat immediately: when one side fills, cancel the other and")
    print("     accept inventory. Good if you want to avoid directional exposure.")
    print("     Cost: you lose the half-spread of the unfilled side.")
    print("  B. Hold both open: let both sides stay live. On a volatile macro")
    print("     market (WTI crude) this risks a correlated double-fill if news")
    print("     gaps the price — you'd end up long or short at bad basis.")
    print("  C. Inventory ladder: pre-decide a max net position (e.g., 300")
    print("     shares long) and cancel bid after that fills, cancel ask after")
    print("     300 short. Reduces but doesn't eliminate directional risk.")
    print()
    print("RECOMMENDED for WTI (high news-shock risk):")
    print("  Start with option A. Set alerts: any fill triggers immediate cancel")
    print("  on the opposite side. This reduces this to a pure reward-collection")
    print("  play with a small adverse-selection cost on each fill.")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--daily-reward", type=float, default=None,
                   help="override daily reward assumption in pUSD (skip API lookup)")
    p.add_argument("--bid", type=float, default=0.355)
    p.add_argument("--ask", type=float, default=0.375)
    p.add_argument("--max-spread", type=float, default=0.045,
                   help="rewardsMaxSpread in dollars (default: 4.5c = 0.045)")
    p.add_argument("--min-size", type=float, default=100)
    p.add_argument("--sizes", type=int, nargs="+",
                   default=[100, 200, 500, 1000, 2000])
    p.add_argument("--pages", type=int, default=20)
    a = p.parse_args()

    bid, ask = a.bid, a.ask

    # --- find the market and read reward params ---
    print("searching for WTI / crude oil markets on Polymarket...")
    markets = find_wti_market(a.pages)
    print(f"  found {len(markets)} crude-related markets")

    rewards_info = None
    best_market  = None
    for m in markets:
        ri = rewards_from_market(m)
        if ri["max_spread"] is not None and ri["min_size"] is not None:
            if best_market is None:
                best_market = m
                rewards_info = ri
            q = str(m.get("question") or "")
            if "100" in q or "hundred" in q.lower():
                best_market = m
                rewards_info = ri
                break

    if best_market:
        print(f"\nmarket: {best_market.get('question')}")
        print(f"  bestBid={best_market.get('bestBid')}  "
              f"bestAsk={best_market.get('bestAsk')}")
        print(f"  rewardsMinSize={rewards_info['min_size']}  "
              f"rewardsMaxSpread={rewards_info['max_spread']}")
        print(f"  daily reward from API: {rewards_info['daily_usd']}")
        print(f"  raw clobRewards: {json.dumps(rewards_info['raw'])[:200]}")

        # use CLI overrides if supplied, else live book values
        if best_market.get("bestBid") and not a.bid:
            bid = f(best_market.get("bestBid"))
        if best_market.get("bestAsk") and not a.ask:
            ask = f(best_market.get("bestAsk"))
        if rewards_info["max_spread"]:
            # API returns max_spread in cents (e.g. 4.5 means 4.5c = $0.045)
            raw_spread = rewards_info["max_spread"]
            a.max_spread = raw_spread / 100.0 if raw_spread > 1.0 else raw_spread
        if rewards_info["min_size"]:
            a.min_size = rewards_info["min_size"]

        # pull real CLOB depth
        toks = jparse(best_market.get("clobTokenIds"), [])
        if toks:
            print(f"\npulling CLOB order book for YES token {toks[0][:16]}...")
            bids, asks = clob_book(toks[0])
            if bids or asks:
                print(f"  top 5 bids: {[(b['price'], b['size']) for b in bids[:5]]}")
                print(f"  top 5 asks: {[(a_['price'], a_['size']) for a_ in asks[:5]]}")
                for size in a.sizes:
                    bd = depth_at(bids, size)
                    ad = depth_at(asks, size)
                    print(f"  depth check: {size} shares — "
                          f"bid side has {bd:.0f} available, "
                          f"ask side has {ad:.0f} available")
    else:
        print("  no WTI market with reward params found — using CLI defaults")

    # --- daily reward ---
    daily_reward = a.daily_reward
    if daily_reward is None:
        if rewards_info and rewards_info["daily_usd"] is not None:
            daily_reward = rewards_info["daily_usd"]
        else:
            # API didn't return a dollar amount; use a bracketed assumption
            print("\n  WARNING: API did not return a dollar-per-day reward amount.")
            print("  This is common — Polymarket often sets rewardsDailyRate in")
            print("  the CLOB rewards contract, not surfaced via gamma API.")
            print("  Showing table at $1, $3, $5, $10/day assumptions.")
            print("  To get the real number: check app.polymarket.com market page")
            print("  or run: --daily-reward <N>  with the actual figure.")
            for dr in [1.0, 3.0, 5.0, 10.0]:
                sizing_table(bid, ask, dr, a.sizes)
            quote_plan(bid, ask, a.max_spread, a.min_size)
            return

    sizing_table(bid, ask, daily_reward, a.sizes)
    quote_plan(bid, ask, a.max_spread, a.min_size)


if __name__ == "__main__":
    main()
