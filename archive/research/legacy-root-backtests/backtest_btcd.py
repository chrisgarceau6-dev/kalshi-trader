#!/usr/bin/env python3
"""KXBTCD targeted late-certainty backtest.

Problem: KXBTCD has ~188 strikes per hourly close. API pagination only returns
~4,000 markets = <1 day of history. A naive fetch-all approach is infeasible.

Solution:
  1. Fetch 30 days of BTC 1-minute prices from Coinbase (~144 API calls).
  2. For each hourly close, use BTC's price at T-300s (mid-trigger-window)
     as the reference for strike scanning — NOT the close price. This avoids
     survivorship bias (scanning around close price selects only winning trades).
  3. Construct KXBTCD tickers directly from the format
     KXBTCD-{YY}{MON}{DD}{HH}-T{price}.99 for ~22 candidate strikes.
  4. Fetch Kalshi candles for those tickers. Apply late-certainty logic.
  5. Determine settlement: BTC_close > strike → YES won.

Using trigger-time price for scanning means we correctly capture both winners
(BTC stayed above strike at close) and losers (BTC dropped below strike).

Run: python3 backtest_btcd.py [--days 30]
"""

import time, argparse, requests
from datetime import datetime, timezone, timedelta
from kalshi_auth import get as kalshi_get

FEE      = 0.07
BET      = 45
OOS_FRAC = 0.33

MIN_ASK   = 90
MAX_ASK   = 93
MIN_SECS  = 600
MAX_SECS  = 2400
PRIOR_N   = 2
PRIOR_MIN = 75

# Strikes scanned relative to BTC price AT TRIGGER TIME (T-300s before close),
# not the close price — avoids survivorship bias.
# At 90-93c ask, the strike is roughly $500-$3000 below current BTC price
# depending on volatility. Scan a wide enough window to capture all cases.
STRIKE_SCAN_LOW  = 200    # minimum $ below trigger-time price to check
STRIKE_SCAN_HIGH = 3500   # maximum $ below trigger-time price to check
STRIKE_STEP      = 100    # KXBTCD uses $100 increments

EDT = timezone(timedelta(hours=-4))
MONTH_ABBR = {1:"JAN",2:"FEB",3:"MAR",4:"APR",5:"MAY",6:"JUN",
              7:"JUL",8:"AUG",9:"SEP",10:"OCT",11:"NOV",12:"DEC"}


def coinbase_minute_btc(days):
    """Fetch BTC-USD 1-minute closes from Coinbase for past N days.
    Returns dict of {utc_minute_ts: close_price}.
    Used to get trigger-time price (T-300s) instead of close price,
    avoiding survivorship bias in strike selection."""
    prices = {}
    end = int(time.time())
    granularity = 60
    chunk = 300 * granularity  # 300 candles × 60s = 5 hours per call
    start = end - days * 86400
    t = start
    n_calls = 0
    while t < end:
        t_end = min(t + chunk, end)
        try:
            r = requests.get(
                "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                params={"start": datetime.fromtimestamp(t, tz=timezone.utc).isoformat(),
                        "end":   datetime.fromtimestamp(t_end, tz=timezone.utc).isoformat(),
                        "granularity": granularity},
                timeout=10,
            )
            if r.status_code == 200:
                for candle in r.json():
                    ts, low, high, open_, close, vol = candle
                    prices[int(ts)] = close
            n_calls += 1
        except Exception:
            pass
        t = t_end
        time.sleep(0.15)
    print(f"  {n_calls} Coinbase API calls, {len(prices)} minute prices")
    return prices


def trigger_time_price(btc_minute_prices, close_ts):
    """Return BTC price at T-1500s (midpoint of 600-2400s trigger window).
    Falls back to nearby minutes if exact minute not available."""
    for offset in (1500, 1440, 1560, 1380, 1620, 1320, 1680, 1260, 1740):
        p = btc_minute_prices.get(close_ts - offset)
        if p:
            return p
    return None


def build_ticker(close_utc_ts, strike_dollars):
    """Construct KXBTCD ticker from close UTC timestamp and strike price."""
    dt_et = datetime.fromtimestamp(close_utc_ts, tz=EDT)
    mon   = MONTH_ABBR[dt_et.month]
    date  = f"{dt_et.year % 100:02d}{mon}{dt_et.day:02d}{dt_et.hour:02d}"
    price = f"{strike_dollars - 0.01:.2f}"
    return f"KXBTCD-{date}-T{price}"


def candle_yes_ask(c):
    try:
        return int(round(float(c["yes_ask"]["close_dollars"]) * 100))
    except (KeyError, ValueError, TypeError):
        return None


def prior_ok(candles, i):
    window = candles[max(0, i - PRIOR_N):i]
    if len(window) < PRIOR_N:
        return False
    return all(candle_yes_ask(c) is not None and candle_yes_ask(c) >= PRIOR_MIN for c in window)


def find_trigger(candles, close_ts):
    for i, c in enumerate(candles):
        secs = close_ts - c.get("end_period_ts", 0)
        if not (MIN_SECS <= secs <= MAX_SECS):
            continue
        ask = candle_yes_ask(c)
        if ask is None or not (MIN_ASK <= ask <= MAX_ASK):
            continue
        if not prior_ok(candles, i):
            continue
        return ask
    return None


def fetch_candles(ticker, close_ts):
    for attempt in range(3):
        try:
            code, r = kalshi_get(
                f"/series/KXBTCD/markets/{ticker}/candlesticks",
                {"start_ts": close_ts - 900, "end_ts": close_ts + 10, "period_interval": 1},
            )
            if code == 200:
                return sorted(r.get("candlesticks", []), key=lambda c: c.get("end_period_ts", 0))
        except Exception:
            pass
        time.sleep(2 ** attempt)
    return []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days",  type=int, default=30)
    parser.add_argument("--hours", type=int, default=24, help="Close times per day to test (default all 24)")
    args = parser.parse_args()

    print(f"\nKXBTCD targeted backtest — {args.days}d, BET=${BET}, FEE={int(FEE*100)}%")
    print(f"Config: ask=[{MIN_ASK},{MAX_ASK}]c, {MIN_SECS}-{MAX_SECS}s, prior n={PRIOR_N} min={PRIOR_MIN}c")
    print(f"Strike scan: ${STRIKE_SCAN_LOW}-${STRIKE_SCAN_HIGH} below TRIGGER-TIME price in ${STRIKE_STEP} steps")
    print(f"{'='*70}")

    print("Fetching BTC 1-min prices from Coinbase (~144 calls)...", flush=True)
    btc_min = coinbase_minute_btc(args.days)
    if not btc_min:
        print("  ERROR: no Coinbase data. Check network."); return

    # Build list of (close_utc_ts, btc_close) for every hour in range
    # btc_close = price at the start of the close minute (the settlement value)
    now    = int(time.time())
    cutoff = (now - args.days * 86400) // 3600 * 3600
    close_times = []
    ts = cutoff
    while ts <= now - 3600:
        btc_close = btc_min.get(ts)
        if btc_close:
            close_times.append((ts, btc_close))
        ts += 3600

    n_closes = len(close_times)
    oos_split = int(n_closes * (1 - OOS_FRAC))
    days_actual = (close_times[-1][0] - close_times[0][0]) / 86400 if n_closes > 1 else 1
    oos_days = days_actual * OOS_FRAC

    t0 = datetime.fromtimestamp(close_times[0][0],  tz=timezone.utc).strftime("%b %d")
    t1 = datetime.fromtimestamp(close_times[-1][0], tz=timezone.utc).strftime("%b %d")
    print(f"  {n_closes} close times  {t0}→{t1}  ({days_actual:.1f}d)")
    print(f"  Train: {oos_split}  OOS: {n_closes - oos_split}")
    print(f"  Candle fetches: ~{n_closes * (STRIKE_SCAN_HIGH - STRIKE_SCAN_LOW) // STRIKE_STEP}")
    print(f"\nRunning simulation...", flush=True)

    MAX_STRIKES = 3
    strike_all = [[] for _ in range(MAX_STRIKES)]
    strike_oos = [[] for _ in range(MAX_STRIKES)]
    cluster_counts = {0: 0, 1: 0, 2: 0, 3: 0}
    total_candles = 0
    missing_tickers = 0
    skipped_no_trigger_price = 0

    for cidx, (close_ts, btc_close) in enumerate(close_times):
        is_oos = cidx >= oos_split

        # Use BTC price at T-300s (trigger time) for strike selection.
        # This avoids survivorship bias — we don't know the close price yet.
        btc_trigger = trigger_time_price(btc_min, close_ts)
        if not btc_trigger:
            skipped_no_trigger_price += 1
            continue

        # Scan strikes $STRIKE_SCAN_LOW to $STRIKE_SCAN_HIGH below trigger price
        btc_ref = round(btc_trigger / 100) * 100
        candidate_strikes = range(
            int(btc_ref - STRIKE_SCAN_HIGH),
            int(btc_ref - STRIKE_SCAN_LOW + STRIKE_STEP),
            STRIKE_STEP,
        )

        triggers = []
        for strike in candidate_strikes:
            if strike <= 0:
                continue
            ticker = build_ticker(close_ts, strike)
            candles = fetch_candles(ticker, close_ts)
            total_candles += 1
            if not candles:
                missing_tickers += 1
                continue
            ask = find_trigger(candles, close_ts)
            if ask is not None:
                won = btc_close >= strike
                triggers.append((ask, won))
            time.sleep(0.015)

        # Sort highest ask first (most certain)
        triggers.sort(key=lambda x: x[0], reverse=True)
        n_triggers = min(len(triggers), MAX_STRIKES)
        cluster_counts[min(n_triggers, 3)] = cluster_counts.get(min(n_triggers, 3), 0) + 1

        for k, (ask, won) in enumerate(triggers[:MAX_STRIKES]):
            contracts = BET / (ask / 100)
            pnl = round(contracts * (1.0 - ask / 100) * (1 - FEE), 2) if won else -BET
            strike_all[k].append(pnl)
            if is_oos:
                strike_oos[k].append(pnl)

        if (cidx + 1) % 50 == 0:
            print(f"  {cidx+1}/{n_closes} close times  ({total_candles} candles, {missing_tickers} missing, {skipped_no_trigger_price} skipped)...", flush=True)

    print(f"\n  Total candle fetches: {total_candles} ({missing_tickers} empty, {skipped_no_trigger_price} skipped — no T-300s price)")
    print(f"\n  Trigger frequency per close time:")
    for k in range(4):
        pct = cluster_counts.get(k, 0) / n_closes * 100
        print(f"    {k} triggers: {cluster_counts.get(k,0):4d} close times ({pct:.1f}%)")

    print(f"\n  Strike breakdown (highest ask first):")
    print(f"  {'':10} {'All n':>6} {'WR':>7} {'$/tr':>7} {'$/day':>8} │ {'OOS n':>6} {'WR':>7} {'$/tr':>7} {'$/day':>8}")
    print("  " + "-"*85)
    labels = ["1st strike", "2nd strike", "3rd strike"]
    for k in range(MAX_STRIKES):
        all_r = strike_all[k]
        oos_r = strike_oos[k]
        if not all_r:
            break
        all_wr  = sum(1 for p in all_r if p > 0) / len(all_r) * 100
        all_ppt = sum(all_r) / len(all_r)
        all_ppd = sum(all_r) / days_actual
        oos_wr  = sum(1 for p in oos_r if p > 0) / len(oos_r) * 100 if oos_r else 0
        oos_ppt = sum(oos_r) / len(oos_r) if oos_r else 0
        oos_ppd = sum(oos_r) / oos_days if oos_r else 0
        flag = " (current)" if k == 0 else ""
        print(f"  {labels[k]:10}{flag:9} {len(all_r):>6} {all_wr:>6.1f}% {all_ppt:>+7.2f} {all_ppd:>+8.2f} │ "
              f"{len(oos_r):>6} {oos_wr:>6.1f}% {oos_ppt:>+7.2f} {oos_ppd:>+8.2f}")

    extra_oos = [p for k in range(1, MAX_STRIKES) for p in strike_oos[k]]
    base_ppd  = sum(strike_oos[0]) / oos_days if strike_oos[0] else 0
    extra_ppd = sum(extra_oos) / oos_days if extra_oos else 0
    print(f"\n  KXBTCD OOS summary:")
    print(f"    1st-strike baseline:       {base_ppd:>+.2f}/day")
    print(f"    Multi-strike uplift:       {extra_ppd:>+.2f}/day")
    print(f"    Combined:                  {base_ppd + extra_ppd:>+.2f}/day")
    print(f"\n  Note: 'won' determined by Coinbase close price vs strike.")
    print(f"  Kalshi may use a different BTC index — expect small discrepancies.")


if __name__ == "__main__":
    main()
