#!/usr/bin/env python3
"""Kalshi as hedge venue for sportsbook promo extraction — MA-legal $250/wk strategy.

THE MECHANISM
-------------
MA-legal sportsbooks (DK, FD, BetMGM, Caesars, ESPN Bet) constantly offer
promos: sign-up bonus bets, deposit matches, odds boosts, "insurance" bets,
"bet $5 get $200 in bonus bets" etc. Each promo is a subsidy from the book
to acquire/retain customers. Face value of these promos is real dollars —
the extraction rate (real cash you keep) is typically 70-80% via correct
hedging.

Kalshi is the hedge venue: it prices the SAME NFL/MLB/NCAAF/UFC games as
DK/FD, in a binary contract form, without the sportsbook's ~4-6% vig.
Hedging the promo bet on Kalshi locks in the promo value regardless of
game outcome.

This is not "beating the market" — the edge comes from the platform giving
you free money (subsidy). Structurally identical to Polymarket liquidity
rewards; that mechanism just isn't available in MA. This one is.

REALISTIC INCOME
----------------
Sign-up bonuses across 5 books: $600-1000 one-time
Ongoing reload promos: ~$100-300/week across all books (odds boosts,
  bet-and-get, profit boosts, insurance offers)
Occasional cross-venue arbs (Kalshi vs Vegas mispricing): $50-150/week
Total realistic: $200-450/week sustained after the sign-up phase

KALSHI FEE MATH
---------------
Taker fee = 0.07 × price × (1-price) per contract (rounded up per order)
Maker fee = 0.0175 × price × (1-price) per contract (75% discount)
At extreme prices (0.05 or 0.95) fees are tiny: $0.003/contract.

usage
    python kalshi_promo_hedge.py sports          # list live Kalshi sports
    python kalshi_promo_hedge.py promo --type bonus-bet --face 200 --hedge-price 0.20
    python kalshi_promo_hedge.py promo --type boost --boost 0.30 --stake 100 --dk-price -140 --kalshi-price 0.42
    python kalshi_promo_hedge.py arb --dk -140 --kalshi 0.42 --stake 100
    python kalshi_promo_hedge.py signups          # list known MA sign-up promos with EV
"""
import argparse, json, math, time, sys
import requests

BASE = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "chris-hedge/0.1"}
KALSHI_TAKER_MULT = 0.07
KALSHI_MAKER_MULT = 0.0175


# ---------------------------------------------------------------- fee helpers

def kalshi_fee(price, n, maker=False):
    """Per-order fee, rounded UP to next cent."""
    if n <= 0: return 0.0
    mult = KALSHI_MAKER_MULT if maker else KALSHI_TAKER_MULT
    cents = mult * n * price * (1 - price) * 100
    return math.ceil(round(cents, 6)) / 100


def american_to_prob(odds):
    """Convert American moneyline odds to implied probability (with vig)."""
    if odds < 0:
        return -odds / (-odds + 100)
    else:
        return 100 / (odds + 100)


def american_to_payout(odds, stake):
    """Profit if bet wins (excluding stake)."""
    if odds < 0:
        return stake * 100 / -odds
    else:
        return stake * odds / 100


# ---------------------------------------------------------------- kalshi pulls

def kget(path, **params):
    r = requests.get(BASE + path, params=params, headers=UA, timeout=20)
    if r.status_code == 429:
        time.sleep(2); r = requests.get(BASE + path, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    time.sleep(0.15)
    return r.json()


def live_sports():
    """Pull today's live Kalshi sports markets, one row per game side."""
    rows = []
    for series in ["KXMLBGAME","KXWNBAGAME","KXNFLGAME","KXNHLGAME",
                   "KXUFCFIGHT","KXMLSGAME","KXNCAAFGAME","KXNCAABGAME",
                   "KXNBAGAME","KXTENNIS"]:
        try:
            j = kget("/markets", series_ticker=series, status="open", limit=100)
        except Exception:
            continue
        for m in j.get("markets") or []:
            def fv(*ks):
                for k in ks:
                    v = m.get(k)
                    if v not in (None, "", "0.0000"):
                        try:
                            x = float(v)
                            return x / 100 if x > 1.5 else x
                        except: pass
                return None
            yb = fv("yes_bid_dollars", "yes_bid")
            ya = fv("yes_ask_dollars", "yes_ask")
            if yb is None or ya is None: continue
            rows.append(dict(
                sport=series.replace("KX","").replace("GAME","").replace("FIGHT",""),
                ticker=m.get("ticker",""),
                event=m.get("event_ticker",""),
                side=m.get("yes_sub_title","")[:20],
                yes_bid=yb, yes_ask=ya,
                mid=(yb+ya)/2, spread=ya-yb,
                close=m.get("close_time","")[:16],
            ))
    return rows


# ---------------------------------------------------------------- promo math

def promo_bonus_bet(face, hedge_price, kalshi_fee_multiplier=KALSHI_TAKER_MULT):
    """Bonus bet extraction. Bonus bets pay PROFIT ONLY (not stake).
    Bet the full face on a high-odds selection, hedge on Kalshi.

    If you bet bonus $F on a side with Kalshi YES price = hedge_price:
      Win: payout = F * (1/hedge_price - 1) real dollars (bonus bet returns
           only profit portion, and the "profit" is at fair odds ≈ hedge_price)
      Loss: bonus bet expires worthless
    Meanwhile, hedge N NO contracts at (1 - hedge_price):
      Win of your side: lose N*hedge_price
      Loss of your side: gain N*(1-hedge_price)
    Balance so both outcomes yield same $, minimizing hedge cost.
    """
    p = hedge_price
    win_gross = face * (1.0 / p - 1.0)  # e.g., face=$200, p=0.20 -> $800 profit
    # Solve for N: win_gross - N*p = -0 + N*(1-p)  =>  N = win_gross / 1.0 = win_gross
    # Actually we want net payoff equal:
    # win side: win_gross - N*p
    # lose side: 0 + N*(1-p) - N*p_fee
    # Set equal: win_gross - N*p = N*(1-p) - fee
    # win_gross = N*(1-p) + N*p - fee = N - fee
    # N = win_gross + fee
    # Approximate ignoring fee first
    N = win_gross
    hedge_cost = N * (1 - p)
    fee = N * KALSHI_TAKER_MULT * p * (1 - p)
    hedge_cost += fee
    win_net  = win_gross - N * p - fee
    lose_net = N * (1 - p) - hedge_cost + N * (1-p) - N*(1-p)  # simplified
    # cleaner:
    # capital deployed: hedge_cost
    # if bonus wins: cash from bonus (=win_gross) + kalshi loss (-N*p) - fees = win_gross - N*p - fee
    # if bonus loses: kalshi payoff N*1 - hedge_cost = N - N*(1-p) - fee = N*p - fee
    win_final  = win_gross - N * p - fee
    lose_final = N - hedge_cost
    return dict(
        strategy="bonus-bet",
        face=face, hedge_price=p, contracts_N=round(N, 1),
        hedge_capital=round(hedge_cost, 2),
        fee=round(fee, 2),
        payoff_if_win=round(win_final, 2),
        payoff_if_lose=round(lose_final, 2),
        guaranteed=round(min(win_final, lose_final), 2),
        extraction_rate_pct=round(min(win_final, lose_final)/face*100, 1),
    )


def promo_odds_boost(stake, boost_pct, dk_price_american, kalshi_yes_price):
    """Odds boost: DK offers extra profit % on a specific bet. E.g., 30% boost
    on Team A at +200 means you get +260 effective payout on a winning stake.
    Hedge on Kalshi's price for the same team.
    """
    p_dk_vig = american_to_prob(dk_price_american)
    dk_win_payout = american_to_payout(dk_price_american, stake) * (1 + boost_pct)
    dk_lose = -stake

    # Hedge: buy Kalshi NO if DK bet is on YES side
    # Cost per NO contract = 1 - kalshi_yes_price
    # Pays $1 if team loses
    p_kalshi = kalshi_yes_price
    # For each contract: if win, lose (1-p_k); if lose, gain p_k
    # Total DK win + kalshi contribution should be balanced
    # DK wins:  +dk_win_payout - N*(1-p_k) - fee
    # DK loses: -stake + N*1 - N*(1-p_k) - fee = -stake + N*p_k - fee
    # Balance:  dk_win_payout - N*(1-p_k) = -stake + N*p_k
    # dk_win_payout + stake = N*p_k + N*(1-p_k) = N
    # N = dk_win_payout + stake
    N = dk_win_payout + stake
    hedge_cost = N * (1 - p_kalshi)
    fee = kalshi_fee(1 - p_kalshi, int(N))
    win_final = dk_win_payout - N * (1 - p_kalshi) - fee
    lose_final = -stake + N - hedge_cost - fee

    return dict(
        strategy="odds-boost",
        stake=stake, boost_pct=boost_pct, dk_price=dk_price_american,
        kalshi_yes_price=p_kalshi,
        dk_win_payout=round(dk_win_payout, 2),
        contracts_N=round(N, 1),
        hedge_capital=round(hedge_cost, 2),
        fee=round(fee, 2),
        payoff_if_win=round(win_final, 2),
        payoff_if_lose=round(lose_final, 2),
        guaranteed=round(min(win_final, lose_final), 2),
        total_capital=round(stake + hedge_cost, 2),
        return_on_capital_pct=round(min(win_final, lose_final)/(stake+hedge_cost)*100, 2),
    )


def promo_arb(dk_price_american, kalshi_yes_price, stake):
    """Pure arbitrage between DK moneyline and Kalshi binary.
    You bet DK on team A, buy Kalshi NO on team A.
    Only profitable if the two prices disagree enough to overcome vig+fees.
    """
    p_dk_novig = american_to_prob(dk_price_american)
    p_k = kalshi_yes_price
    dk_win_payout = american_to_payout(dk_price_american, stake)

    # Solve for balanced hedge
    N = dk_win_payout + stake
    hedge_cost = N * (1 - p_k)
    fee = kalshi_fee(1 - p_k, int(N))

    win_final = dk_win_payout - N * (1 - p_k) - fee
    lose_final = -stake + N - hedge_cost - fee

    return dict(
        strategy="arb",
        dk_price=dk_price_american,
        dk_implied_prob=round(p_dk_novig, 3),
        kalshi_yes_price=p_k,
        stake=stake,
        contracts_N=round(N, 1),
        total_capital=round(stake + hedge_cost, 2),
        payoff_if_win=round(win_final, 2),
        payoff_if_lose=round(lose_final, 2),
        guaranteed=round(min(win_final, lose_final), 2),
        return_on_capital_pct=round(min(win_final, lose_final)/(stake+hedge_cost)*100, 2),
    )


# ---------------------------------------------------------------- MA signup promos

MA_SIGNUP_PROMOS = [
    # Known MA-legal signup structures as of mid-2026 (verify current on each app)
    dict(book="DraftKings",  offer="Bet $5, get $200 in bonus bets",         face=200, real_ev_low=140, real_ev_high=160),
    dict(book="FanDuel",     offer="Bet $5, get $200 in bonus bets",         face=200, real_ev_low=140, real_ev_high=160),
    dict(book="BetMGM",      offer="First bet up to $1500 in bonus bets",    face=1500,real_ev_low=800, real_ev_high=1100),
    dict(book="Caesars",     offer="First bet up to $1000 back if it loses", face=1000,real_ev_low=550, real_ev_high=750),
    dict(book="ESPN BET",    offer="Bet $10, get $250 in bonus bets",        face=250, real_ev_low=175, real_ev_high=200),
    dict(book="Fanatics",    offer="Deposit match up to $1000",              face=1000,real_ev_low=650, real_ev_high=850),
]


def show_signups():
    print("MA-LEGAL SIGNUP PROMOS  (verify current offers in each app before signup)\n")
    print(f"{'book':<12}  {'offer':<45}  {'face':>7}  {'real EV':>15}")
    print("-" * 85)
    total_low, total_high = 0, 0
    for p in MA_SIGNUP_PROMOS:
        print(f"{p['book']:<12}  {p['offer']:<45}  ${p['face']:>6}  ${p['real_ev_low']:>4}-${p['real_ev_high']:>4}")
        total_low  += p['real_ev_low']
        total_high += p['real_ev_high']
    print("-" * 85)
    print(f"{'TOTAL':<12}  {'':<45}  {'':<7}  ${total_low:>4}-${total_high:>4}")
    print(f"\nAll require Kalshi hedge to lock in profit regardless of game outcome.")
    print(f"Realistic execution timeline: 1 book/week, ~$150-300 real cash per book,")
    print(f"~5-6 weeks to fully cycle sign-ups = ~$800-1400 one-time income.")
    print(f"\nRealistic sustained income after signups (from ongoing reload promos:")
    print(f"  - Daily odds boosts (per book): 5-15/day at avg $2-5 EV each")
    print(f"  - Weekly 'bet and get' offers: 2-3/week at $10-30 EV each")
    print(f"  - Sport-specific insurance offers: 1-3/week at $10-50 EV")
    print(f"  With 5 books active: $200-500/week ongoing.")


# ---------------------------------------------------------------- entry points

def cmd_sports(a):
    rows = live_sports()
    if not rows:
        print("no live sports markets found on Kalshi right now")
        return
    print(f"{'sport':<10}  {'side':<20}  {'bid':>6}  {'ask':>6}  {'spread':>7}  {'close':<17}  {'ticker':<40}")
    print("-" * 115)
    for r in rows[:50]:
        print(f"{r['sport']:<10}  {r['side']:<20}  {r['yes_bid']:>6.3f}  {r['yes_ask']:>6.3f}  "
              f"{r['spread']:>6.3f}  {r['close']:<17}  {r['ticker']:<40}")
    print(f"\ntotal live: {len(rows)} sports markets. To hedge a DK/FD bet:")
    print("  python kalshi_promo_hedge.py arb --dk <american_odds> --kalshi <yes_price> --stake <N>")


def cmd_promo(a):
    if a.type == "bonus-bet":
        r = promo_bonus_bet(a.face, a.hedge_price)
    elif a.type == "boost":
        r = promo_odds_boost(a.stake, a.boost, a.dk_price, a.kalshi_price)
    else:
        print(f"unknown promo type: {a.type}"); return
    print(json.dumps(r, indent=2))
    print(f"\n>>> guaranteed profit: ${r['guaranteed']}  "
          f"(extraction {r.get('extraction_rate_pct', 'N/A')}%)")


def cmd_arb(a):
    r = promo_arb(a.dk, a.kalshi, a.stake)
    print(json.dumps(r, indent=2))
    if r['guaranteed'] > 0:
        print(f"\n>>> LOCKED ARB: ${r['guaranteed']} on ${r['total_capital']} = "
              f"{r['return_on_capital_pct']}% ROC")
    else:
        print(f"\n>>> NO ARB: worst case ${r['guaranteed']}. Move on.")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("sports")
    s1.set_defaults(fn=cmd_sports)

    s2 = sub.add_parser("promo")
    s2.add_argument("--type", choices=["bonus-bet","boost"], required=True)
    s2.add_argument("--face", type=float, default=200)
    s2.add_argument("--stake", type=float, default=100)
    s2.add_argument("--hedge-price", type=float, default=0.20,
                    help="kalshi YES price of the team you bet ON at DK")
    s2.add_argument("--boost", type=float, default=0.30,
                    help="odds boost fraction (0.30 = 30% boost)")
    s2.add_argument("--dk-price", type=int, default=200,
                    help="DK american odds")
    s2.add_argument("--kalshi-price", type=float, default=0.35)
    s2.set_defaults(fn=cmd_promo)

    s3 = sub.add_parser("arb")
    s3.add_argument("--dk", type=int, required=True, help="DK american odds")
    s3.add_argument("--kalshi", type=float, required=True,
                    help="kalshi YES price of the team you bet ON at DK")
    s3.add_argument("--stake", type=float, default=100)
    s3.set_defaults(fn=cmd_arb)

    s4 = sub.add_parser("signups")
    s4.set_defaults(fn=show_signups if False else lambda a: show_signups())

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
