#!/usr/bin/env python3
"""Settlement-aware correction for poly-copy-backtest round-trips.

THE BUG THIS FIXES
------------------
poly-copy-backtest builds a round-trip only when a BUY is matched by a
SELL or REDEEM. Traders redeem winners (fires REDEEM -> round-trip exists)
but abandon losers, which settle to zero and are never redeemed (no exit
event -> no round-trip). So the round-trip sample is winner-biased and
every PnL/Sharpe figure from it is inflated.

Meanwhile /positions is biased the OPPOSITE way: redeemed winners vanish
from it, leaving mostly worthless losing tokens.

The two are COMPLEMENTARY. Winners live in the trades feed, losers live in
the positions snapshot. Merge them and you get the whole book.

WHAT THIS DOES
--------------
1. Loads a round-trip CSV produced by poly-copy-backtest --out
2. Pulls /positions for the same wallet
3. Any position that has RESOLVED (curPrice ~0 or ~1) and is still sitting
   there is a trade the matcher never saw -> add it as a synthetic
   round-trip, entry = avgPrice, exit = 0.0 or 1.0
4. Positions at intermediate prices are genuinely still open -> excluded,
   and reported separately so you know the size of the unknown
5. Recomputes hit rate / PnL / Sharpe on the merged set

usage:
    python poly_settle_correct.py <wallet> <roundtrip_csv> [--slippage 0.03]
                                  [--bet 100] [--fee-mult 0.0]

    python poly_settle_correct.py 0xfbf3d501e88815464642d0e913f15379c3eeb218 vp_s03.csv

NOTE ON FEES: --fee-mult defaults to 0 (gross). To match the backtest's
fee model, grep FEE_MULT in kalshi_weather_edge.py and pass that value.
Fees are applied to entry only here; a loser exiting at 0.0 has no exit
fee anyway, so the approximation is small.

WHAT THIS STILL CANNOT FIX
--------------------------
* Positions the wallet MERGED or transferred rather than sold/redeemed
* Genuinely open positions (reported, not guessed)
* Fill quality / latency — no orderbook history exists, so --slippage
  remains an assumption, not a measurement
"""
import argparse, sys, time
import requests
import pandas as pd
import numpy as np

DATA = "https://data-api.polymarket.com"


def fetch_positions(wallet, max_pages=12):
    out, offset, LIMIT = [], 0, 500
    for _ in range(max_pages):
        r = requests.get(f"{DATA}/positions",
                         params={"user": wallet, "limit": LIMIT, "offset": offset},
                         timeout=30)
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} from /positions: {r.text[:200]}")
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < LIMIT:
            break
        offset += LIMIT
        time.sleep(0.25)
    return out


def pnl_of(entry, exit_, bet, slip, fee_mult):
    """Same shape as the backtest: buy at entry+slip, sell at exit-slip."""
    e = min(max(entry + slip, 0.01), 0.99)
    x = min(max(exit_ - slip, 0.0), 1.0)
    entry_fee = fee_mult * e * (1 - e)
    contracts = bet / (e + entry_fee)
    return contracts * x - contracts * (e + entry_fee)


def block(label, entries, exits, bet, slip, fee_mult):
    if len(entries) == 0:
        print(f"\n[{label}] n=0")
        return None
    pnls = np.array([pnl_of(e, x, bet, slip, fee_mult)
                     for e, x in zip(entries, exits)])
    hit = (pnls > 0).mean()
    sharpe = pnls.mean() / pnls.std() if pnls.std() > 0 else float("nan")
    print(f"\n[{label}] n={len(pnls)}")
    print(f"  hit rate:        {hit:.1%}")
    print(f"  total PnL:       ${pnls.sum():,.0f}")
    print(f"  mean PnL/trade:  ${pnls.mean():+,.2f}")
    print(f"  Sharpe/trade:    {sharpe:+.3f}")
    return pnls


def main():
    p = argparse.ArgumentParser()
    p.add_argument("wallet")
    p.add_argument("roundtrips", help="CSV from poly-copy-backtest --out")
    p.add_argument("--slippage", type=float, default=0.03)
    p.add_argument("--bet", type=float, default=100.0)
    p.add_argument("--fee-mult", type=float, default=0.0)
    p.add_argument("--out", default="settled_corrected.csv")
    a = p.parse_args()

    try:
        rt = pd.read_csv(a.roundtrips)
    except FileNotFoundError:
        print(f"missing {a.roundtrips}"); sys.exit(1)
    for c in ("entry_price", "exit_price"):
        if c not in rt.columns:
            print(f"{a.roundtrips} has no {c} column"); sys.exit(1)
        rt[c] = pd.to_numeric(rt[c], errors="coerce")
    rt = rt.dropna(subset=["entry_price", "exit_price"])
    print(f"loaded {len(rt)} round-trips from {a.roundtrips}")

    print(f"fetching /positions for {a.wallet[:12]}...")
    pos = fetch_positions(a.wallet.lower())
    if not pos:
        print("no positions returned — cannot correct; treat the round-trip "
              "numbers as winner-biased and unusable")
        sys.exit(1)
    P = pd.DataFrame(pos)
    for c in ["size", "avgPrice", "curPrice"]:
        if c not in P.columns:
            print(f"/positions missing {c} — schema drift"); sys.exit(1)
        P[c] = pd.to_numeric(P[c], errors="coerce")
    P = P[P["size"].fillna(0) > 0].dropna(subset=["avgPrice", "curPrice"])
    print(f"  {len(P)} live-size positions in snapshot")

    dead = P[P["curPrice"] <= 0.02].copy()      # settled to zero, never redeemed
    won  = P[P["curPrice"] >= 0.98].copy()      # won, not yet redeemed
    open_= P[(P["curPrice"] > 0.02) & (P["curPrice"] < 0.98)].copy()

    print(f"  resolved LOSSES sitting unredeemed: {len(dead)}   <-- the missing trades")
    print(f"  resolved WINS not yet redeemed:     {len(won)}")
    print(f"  genuinely still open:               {len(open_)}")

    print("\n" + "=" * 62)
    print(f"bet ${a.bet:.0f}/trade, {a.slippage*100:.0f}c one-way slippage, "
          f"fee_mult {a.fee_mult}")
    print("=" * 62)

    before = block("AS REPORTED (round-trips only — winner-biased)",
                   rt.entry_price.values, rt.exit_price.values,
                   a.bet, a.slippage, a.fee_mult)

    add_entry = np.concatenate([dead.avgPrice.values, won.avgPrice.values])
    add_exit = np.concatenate([np.zeros(len(dead)), np.ones(len(won))])
    block("ADDED BACK (resolved positions the matcher never saw)",
          add_entry, add_exit, a.bet, a.slippage, a.fee_mult)

    all_entry = np.concatenate([rt.entry_price.values, add_entry])
    all_exit = np.concatenate([rt.exit_price.values, add_exit])
    after = block("CORRECTED (merged, settlement-aware)",
                  all_entry, all_exit, a.bet, a.slippage, a.fee_mult)

    if before is not None and after is not None:
        print("\n--- VERDICT ---")
        print(f"  PnL {before.sum():+,.0f} -> {after.sum():+,.0f} "
              f"({after.sum() - before.sum():+,.0f} from the missing losses)")
        surviving = after.sum() > 0
        print(f"  edge survives correction: {'YES' if surviving else 'NO'}")
        if len(open_):
            frac = len(open_) / max(len(all_entry), 1)
            print(f"  caveat: {len(open_)} positions still open ({frac:.0%} of book) "
                  f"— unknown, excluded from both columns")

    out = pd.DataFrame({"entry_price": all_entry, "exit_price": all_exit})
    out["source"] = (["roundtrip"] * len(rt)
                     + ["settled_loss"] * len(dead) + ["settled_win"] * len(won))
    out.to_csv(a.out, index=False)
    print(f"\nsaved -> {a.out}")

    print("\nSANITY CHECK — share of exits at <=0.05 (a real book has plenty):")
    print(f"  before: {(rt.exit_price <= 0.05).mean():.1%}")
    print(f"  after:  {(pd.Series(all_exit) <= 0.05).mean():.1%}")


if __name__ == "__main__":
    main()
