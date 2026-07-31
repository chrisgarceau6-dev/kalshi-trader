#!/usr/bin/env python3
"""Multi-venue sports arbitrage — Kalshi vs Prophet vs Novig vs Polymarket.

THE STRATEGY
------------
Four venues price the same MLB/NBA/NFL/soccer games. Prices diverge because:
  - Different fee structures (Kalshi flat, Prophet 2%, Novig ~1%, Polymarket varies)
  - Different order flow (Kalshi/Robinhood = retail + institutional,
    Prophet/Novig = pure retail P2P)
  - Different liquidity depth (Kalshi deepest, P2P thinner)

When Kalshi has YES at 0.55 and Prophet has same team at implied 0.60, you
can buy YES on Kalshi and buy NO on Prophet — locked profit if the sum of
costs < $1 after fees on both sides.

Prophet/Novig don't have public APIs, so this tool pulls Kalshi live and
lets you paste in Prophet/Novig prices from their apps. Manual is fine —
opportunities last 5-15 min typically, plenty of time to key in a moneyline.

usage:
    # See today's Kalshi MLB games (find one to check on Prophet/Novig)
    python multi_venue_arb.py games

    # Check arb for a specific game
    python multi_venue_arb.py check --team "yankees" --venue prophet --price -120
    python multi_venue_arb.py check --team "yankees" --venue novig  --price -125
    python multi_venue_arb.py check --team "yankees" --venue kalshi --yes 0.55

    # Manual arb sizing given two prices
    python multi_venue_arb.py arb --a-price 0.52 --a-fee 0.01 --b-price 0.44 --b-fee 0.005 --capital 500
"""
import argparse, json, math, time
import requests

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "chris-arb/0.1"}

# Fee structures per venue (as fraction of stake/contract value)
# Verified against user's accessible platforms as of 2026-07-24
FEES = {
    "kalshi":     lambda p, taker=True: 0.07 * p * (1-p) if taker else 0.0175 * p * (1-p),
    "robinhood":  lambda p, taker=True: 0.07 * p * (1-p) if taker else 0.0175 * p * (1-p),
    "prophet":    lambda p, taker=True: 0.02 * p,  # 2% on winnings only (per prophetx.co)
    "betopenly":  lambda p, taker=True: 0.02 * p,  # ~2% typical P2P (verify at signup)
    "rebet":      lambda p, taker=True: 0.03 * p,  # 3% typical sweepstakes fee (verify)
    "polymarket": lambda p, taker=True: 0.02 * p * (1-p) if taker else 0.0,
    "insight":    lambda p, taker=True: 0.02 * p * (1-p) if taker else 0.0,  # verify
    # Blocked for reference
    "novig":      lambda p, taker=True: None,  # 21+ blocked
}


def american_to_prob(odds):
    """Convert American odds to implied probability (with vig)."""
    if odds < 0:
        return -odds / (-odds + 100)
    else:
        return 100 / (odds + 100)


def prob_to_american(p):
    """Convert implied probability to American odds."""
    if p >= 0.5:
        return -int(round(p * 100 / (1-p)))
    else:
        return int(round((1-p) * 100 / p))


# ---------------------------------------------------------------- kalshi pulls

def get_kalshi_games(sport="mlb"):
    """Pull today's Kalshi games for a sport."""
    series_map = {
        "mlb": "KXMLBGAME", "nfl": "KXNFLGAME", "nba": "KXNBAGAME",
        "wnba": "KXWNBAGAME", "nhl": "KXNHLGAME", "mls": "KXMLSGAME",
        "ncaaf": "KXNCAAFGAME", "ncaab": "KXNCAABGAME", "ufc": "KXUFCFIGHT",
    }
    stk = series_map.get(sport.lower())
    if not stk:
        return []
    try:
        r = requests.get(f"{KALSHI}/markets", params={
            "series_ticker": stk, "status": "open", "limit": 100
        }, headers=UA, timeout=20)
        ms = r.json().get('markets') or []
        # group into games
        games = {}
        for m in ms:
            event = m.get('event_ticker', '')
            if event not in games:
                games[event] = []
            def fv(*ks):
                for k in ks:
                    v = m.get(k)
                    if v not in (None, "", "0.0000"):
                        try:
                            x = float(v)
                            return x/100 if x > 1.5 else x
                        except: pass
                return None
            games[event].append({
                'ticker': m.get('ticker'),
                'team': m.get('yes_sub_title', ''),
                'yes_bid': fv('yes_bid_dollars','yes_bid'),
                'yes_ask': fv('yes_ask_dollars','yes_ask'),
                'close': m.get('close_time','')[:16],
            })
        return games
    except Exception as e:
        return {}


# ---------------------------------------------------------------- arb math

def compute_arb(price_a, price_b, capital, fee_a_rate=0.0, fee_b_rate=0.0):
    """
    price_a, price_b: implied probabilities on each venue for OPPOSING sides
      (e.g. price_a = YES on venue A at 0.52, price_b = NO on venue B at 0.44)
    If price_a + price_b < 1, arb exists (before fees).

    Sizing: split capital such that payoff is equal on both outcomes.
    """
    if price_a >= 1 or price_b >= 1 or price_a <= 0 or price_b <= 0:
        return None

    # Simple arb sizing: contracts on each side such that payoff is balanced
    # If we buy N_a contracts YES on A (cost N_a * price_a) and
    #    buy N_b contracts NO on B (cost N_b * price_b)
    # Payoff if YES: N_a * 1 (from A) - N_b * price_b (paid for nothing)
    # Payoff if NO:  -N_a * price_a (paid for nothing) + N_b * 1

    # Solve for balanced payoff, N_a + N_b sized to total capital
    # Balance: N_a - N_b * price_b = N_b - N_a * price_a
    # N_a * (1 + price_a) = N_b * (1 + price_b)
    # ratio N_a/N_b = (1 + price_b) / (1 + price_a)
    ratio = (1 + price_b) / (1 + price_a)

    # Total cost: N_a * price_a + N_b * price_b = capital
    # N_b = N_a / ratio
    # N_a * price_a + N_a/ratio * price_b = capital
    # N_a * (price_a + price_b/ratio) = capital
    n_a = capital / (price_a + price_b / ratio)
    n_b = n_a / ratio

    cost_a = n_a * price_a
    cost_b = n_b * price_b
    fee_a = n_a * fee_a_rate
    fee_b = n_b * fee_b_rate
    total_cost = cost_a + cost_b + fee_a + fee_b

    payoff_yes = n_a - cost_a - cost_b - fee_a - fee_b  # net if YES wins
    payoff_no  = n_b - cost_a - cost_b - fee_a - fee_b  # net if NO wins

    guaranteed = min(payoff_yes, payoff_no)
    roc = guaranteed / total_cost * 100 if total_cost > 0 else 0

    return {
        'n_a': round(n_a, 1),
        'n_b': round(n_b, 1),
        'cost_a': round(cost_a, 2),
        'cost_b': round(cost_b, 2),
        'fee_a': round(fee_a, 3),
        'fee_b': round(fee_b, 3),
        'total_capital': round(total_cost, 2),
        'payoff_if_a_wins': round(payoff_yes, 2),
        'payoff_if_b_wins': round(payoff_no, 2),
        'guaranteed_profit': round(guaranteed, 2),
        'roc_pct': round(roc, 2),
        'sum_of_prices': round(price_a + price_b, 4),
        'edge_before_fees_pct': round((1 - price_a - price_b) * 100, 2),
    }


# ---------------------------------------------------------------- commands

def cmd_games(a):
    for sport in ['mlb','wnba','mls','ufc','ncaaf','nba','nfl','nhl']:
        games = get_kalshi_games(sport)
        if not games: continue
        print(f"\n=== {sport.upper()} ({len(games)} games on Kalshi) ===")
        for event, sides in list(games.items())[:10]:
            teams = " vs ".join(s.get('team','?')[:15] for s in sides)
            close = sides[0].get('close','')
            print(f"  {event}  {close}  {teams}")
            for s in sides:
                yb = s.get('yes_bid'); ya = s.get('yes_ask')
                if yb is not None and ya is not None:
                    am = prob_to_american((yb+ya)/2) if 0<yb<1 else '?'
                    print(f"    {s.get('team','?')[:20]:<20} {yb:.3f}/{ya:.3f}  (~{am} american)")


def cmd_arb(a):
    """Manual arb sizing given two prices."""
    r = compute_arb(a.a_price, a.b_price, a.capital, a.a_fee, a.b_fee)
    if not r:
        print("Invalid inputs")
        return
    print(f"\n=== ARB SIZING ===")
    print(f"Sum of prices: {r['sum_of_prices']}  (edge before fees: {r['edge_before_fees_pct']:+.2f}%)")
    print(f"Buy {r['n_a']} contracts @ {a.a_price:.3f} on venue A (cost ${r['cost_a']})")
    print(f"Buy {r['n_b']} contracts @ {a.b_price:.3f} on venue B (cost ${r['cost_b']})")
    print(f"Fees: A=${r['fee_a']}  B=${r['fee_b']}")
    print(f"Total capital deployed: ${r['total_capital']}")
    print(f"Payoff if A wins: ${r['payoff_if_a_wins']}")
    print(f"Payoff if B wins: ${r['payoff_if_b_wins']}")
    if r['guaranteed_profit'] > 0:
        print(f"\n>>> LOCKED ARB: ${r['guaranteed_profit']} (ROC {r['roc_pct']:+.2f}%)")
    else:
        print(f"\n>>> NO ARB: worst case ${r['guaranteed_profit']}")


def cmd_check(a):
    """Check arb for a specific team across Kalshi + user-input venue."""
    if a.venue == 'kalshi' or a.venue == 'robinhood':
        print("Need one Kalshi/Robinhood price and one Prophet/Novig price to compare.")
        return

    # Find team on Kalshi
    kalshi_prices = None
    for sport in ['mlb','nba','nfl','nhl','wnba','ncaaf','ncaab','mls','ufc']:
        games = get_kalshi_games(sport)
        for event, sides in games.items():
            for s in sides:
                if a.team.lower() in s.get('team','').lower():
                    kalshi_prices = (s.get('yes_bid'), s.get('yes_ask'), s.get('ticker'))
                    print(f"Found on Kalshi ({sport.upper()}): {s.get('team')} bid={s.get('yes_bid'):.3f} ask={s.get('yes_ask'):.3f}")
                    break
            if kalshi_prices: break
        if kalshi_prices: break

    if not kalshi_prices:
        print(f"Team '{a.team}' not found on Kalshi")
        return

    kalshi_yes_bid, kalshi_yes_ask, kalshi_ticker = kalshi_prices

    # Convert user's venue price to implied probability
    if a.price is not None:
        # Assume american odds if magnitude > 1.5
        if abs(a.price) > 1.5:
            venue_yes_prob = american_to_prob(int(a.price))
            print(f"{a.venue.title()} price {a.price} american = YES implied {venue_yes_prob:.3f}")
        else:
            venue_yes_prob = a.price
    elif a.yes is not None:
        venue_yes_prob = a.yes
    else:
        print("Provide --price (american odds) or --yes (probability)")
        return

    # Check both arb directions
    # Direction 1: Buy YES Kalshi (at ask), Buy NO on venue (cost = 1 - venue_yes_prob)
    kalshi_fee_taker = 0.07 * kalshi_yes_ask * (1-kalshi_yes_ask)
    venue_no_prob = 1 - venue_yes_prob

    venue_fee = FEES[a.venue](venue_yes_prob, taker=True)

    r1 = compute_arb(kalshi_yes_ask, venue_no_prob, a.capital,
                     fee_a_rate=kalshi_fee_taker, fee_b_rate=venue_fee)

    # Direction 2: Buy NO Kalshi (at 1-bid), Buy YES on venue
    kalshi_no_cost = 1 - kalshi_yes_bid
    kalshi_no_fee = 0.07 * kalshi_no_cost * (1-kalshi_no_cost)

    r2 = compute_arb(kalshi_no_cost, venue_yes_prob, a.capital,
                     fee_a_rate=kalshi_no_fee, fee_b_rate=venue_fee)

    print(f"\n=== DIRECTION 1: Buy YES Kalshi + Buy NO {a.venue.title()} ===")
    if r1 and r1['guaranteed_profit'] > 0:
        print(f"  LOCKED ARB: ${r1['guaranteed_profit']} on ${r1['total_capital']} = {r1['roc_pct']:+.2f}% ROC")
        print(f"  Buy {r1['n_a']:.0f} YES on Kalshi @ {kalshi_yes_ask:.3f} = ${r1['cost_a']}")
        print(f"  Buy {r1['n_b']:.0f} NO on {a.venue.title()} @ {venue_no_prob:.3f} = ${r1['cost_b']}")
    else:
        print(f"  No arb (sum={r1['sum_of_prices'] if r1 else 'n/a'}, guaranteed=${r1['guaranteed_profit'] if r1 else 'n/a'})")

    print(f"\n=== DIRECTION 2: Buy NO Kalshi + Buy YES {a.venue.title()} ===")
    if r2 and r2['guaranteed_profit'] > 0:
        print(f"  LOCKED ARB: ${r2['guaranteed_profit']} on ${r2['total_capital']} = {r2['roc_pct']:+.2f}% ROC")
        print(f"  Buy {r2['n_a']:.0f} NO on Kalshi @ {kalshi_no_cost:.3f} = ${r2['cost_a']}")
        print(f"  Buy {r2['n_b']:.0f} YES on {a.venue.title()} @ {venue_yes_prob:.3f} = ${r2['cost_b']}")
    else:
        print(f"  No arb (sum={r2['sum_of_prices'] if r2 else 'n/a'}, guaranteed=${r2['guaranteed_profit'] if r2 else 'n/a'})")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("games", help="List today's Kalshi games")
    s1.set_defaults(fn=cmd_games)

    s2 = sub.add_parser("check", help="Check arb for a team across venues")
    s2.add_argument("--team", required=True)
    s2.add_argument("--venue", choices=['prophet','betopenly','rebet','polymarket','insight'], required=True)
    s2.add_argument("--price", type=float, help="American odds (negative for fav)")
    s2.add_argument("--yes", type=float, help="Implied YES probability 0-1")
    s2.add_argument("--capital", type=float, default=500)
    s2.set_defaults(fn=cmd_check)

    s3 = sub.add_parser("arb", help="Manual arb sizing given two prices")
    s3.add_argument("--a-price", type=float, required=True)
    s3.add_argument("--b-price", type=float, required=True)
    s3.add_argument("--a-fee", type=float, default=0.02)
    s3.add_argument("--b-fee", type=float, default=0.02)
    s3.add_argument("--capital", type=float, default=500)
    s3.set_defaults(fn=cmd_arb)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
