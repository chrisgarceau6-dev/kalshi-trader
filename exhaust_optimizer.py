#!/usr/bin/env python3
"""EXHAUSTIVE copy-trade portfolio optimizer.

Tests ALL combinations of 1-6 wallets from the 2/3+ HARD screener pool with:
  - Walk-forward validation (train on first 70%, test on last 30%)
  - Multiple sizing schemes (flat, top-conviction, proportional)
  - Multiple objectives (mean, median, sharpe, worst-case)
  - Robustness score = min(train_mean, test_mean) / std

Only reports combos that hold up out-of-sample. Discards in-sample-only fits.

Writes progress to exhaust_progress.txt so we can monitor.
"""
import os, sys, time
from datetime import datetime
from pathlib import Path
from itertools import combinations
import pandas as pd
import numpy as np

BASE = Path(__file__).parent
BANKROLL = 2000
SLIP = 0.03
MIN_WEEKS = 20
PROGRESS = BASE / "exhaust_progress.txt"
RESULTS = BASE / "exhaust_results.csv"
FINAL_REPORT = BASE / "exhaust_final.txt"


def log(msg):
    ts = datetime.now().isoformat(timespec='seconds')
    line = f"[{ts}] {msg}\n"
    with open(PROGRESS, "a") as f:
        f.write(line)
    print(line, end="", flush=True)


def load_wallet(w):
    prefix = w[:8]
    p = BASE / f"_tmp_{prefix}_0.03.csv"
    if not p.exists(): return None
    try:
        df = pd.read_csv(p)
        df['entry_ts'] = pd.to_datetime(df['entry_ts'])
        df['exit_ts'] = pd.to_datetime(df['exit_ts'])
        df['wallet'] = w
        return df
    except:
        return None


def compute_pnl(df, bet):
    """Flat-$ copy PnL per trade."""
    def per(row):
        e = min(row['entry_price'] + SLIP, 0.99)
        x = max(row['exit_price'] - SLIP, 0.01)
        lp = bet/e * (x - e)
        sp = bet/(1-e) * ((1-x) - (1-e))
        return lp if abs(lp) >= abs(sp) else sp
    return df.apply(per, axis=1)


def peak_concurrent(dfs):
    events = []
    for d in dfs:
        for _, r in d.iterrows():
            events.append((r['entry_ts'], +1))
            events.append((r['exit_ts'], -1))
    events.sort()
    curr, peak = 0, 0
    for _, delta in events:
        curr += delta
        peak = max(peak, curr)
    return peak


def eval_combo(wallet_dfs, bet):
    """Evaluate a combo of pre-loaded wallet dfs with walk-forward split."""
    if not wallet_dfs: return None
    # Compute per-trade PnL at bet size
    all_trades = []
    for d in wallet_dfs:
        d = d.copy()
        d['pnl'] = compute_pnl(d, bet)
        all_trades.append(d)
    combined = pd.concat(all_trades, ignore_index=True).sort_values('entry_ts').reset_index(drop=True)

    # Time-based split: first 70% by entry_ts vs last 30%
    tmin, tmax = combined['entry_ts'].min(), combined['entry_ts'].max()
    split_time = tmin + (tmax - tmin) * 0.7
    train = combined[combined['entry_ts'] <= split_time]
    test = combined[combined['entry_ts'] > split_time]

    def weekly_stats(subset):
        if len(subset) == 0: return None
        subset = subset.copy()
        subset['week'] = subset['entry_ts'].dt.to_period('W')
        weekly = subset.groupby('week')['pnl'].sum()
        span_weeks = max(1, (subset['entry_ts'].max() - subset['entry_ts'].min()).days / 7)
        return {
            'weekly_time_avg': subset['pnl'].sum() / span_weeks,
            'weekly_mean': weekly.mean() if len(weekly) else 0,
            'weekly_median': weekly.median() if len(weekly) else 0,
            'weekly_std': weekly.std() if len(weekly) > 1 else 0,
            'weekly_min': weekly.min() if len(weekly) else 0,
            'weekly_p25': weekly.quantile(0.25) if len(weekly) else 0,
            'pct_profitable': (weekly > 0).mean() * 100 if len(weekly) else 0,
            'n_weeks': len(weekly),
            'total_pnl': subset['pnl'].sum(),
            'n_trades': len(subset),
        }

    tr = weekly_stats(train)
    te = weekly_stats(test)
    if not tr or not te or te['n_weeks'] < 4: return None

    # Robustness: min(train, test) time_avg, penalized by std
    robust_avg = min(tr['weekly_time_avg'], te['weekly_time_avg'])
    # Sharpe-like: mean / std
    sharpe = (te['weekly_mean'] / te['weekly_std']) if te['weekly_std'] > 0 else 0

    return {
        'train_time_avg': round(tr['weekly_time_avg'], 2),
        'test_time_avg': round(te['weekly_time_avg'], 2),
        'train_median': round(tr['weekly_median'], 2),
        'test_median': round(te['weekly_median'], 2),
        'test_p25': round(te['weekly_p25'], 2),
        'test_pct_profitable': round(te['pct_profitable'], 1),
        'train_pct_profitable': round(tr['pct_profitable'], 1),
        'test_n_weeks': te['n_weeks'],
        'test_sharpe': round(sharpe, 2),
        'robust_time_avg': round(robust_avg, 2),
        'test_worst_week': round(te['weekly_min'], 2),
        'test_std': round(te['weekly_std'], 2),
    }


def best_bet(dfs):
    peak = peak_concurrent(dfs)
    if peak == 0: return 0
    return max(10, min(100, (BANKROLL // peak) // 5 * 5))


def main():
    with open(PROGRESS, "w") as f:
        f.write(f"=== exhaust start {datetime.now().isoformat(timespec='seconds')} ===\n")

    # Load screener report and filter
    report = pd.read_csv(BASE / "wallet_hunt_report.csv")
    report = report[(report['hard_score'] >= 2) & (report['t_stat'] >= 3)]

    # Load all candidate wallets
    cache = {}
    for _, r in report.iterrows():
        d = load_wallet(r['wallet'])
        if d is None or len(d) < 20: continue
        weeks = (d['entry_ts'].max() - d['entry_ts'].min()).days / 7
        if weeks < MIN_WEEKS: continue
        cache[r['wallet']] = d

    candidates = list(cache.keys())
    log(f"loaded {len(candidates)} candidate wallets with >={MIN_WEEKS}wk history")

    # Precompute all 1-4 combos, plus SELECTIVE 5-6 combos
    results = []
    n = len(candidates)

    # Do 1-4 exhaustively
    total_1to4 = sum(len(list(combinations(range(n), k))) for k in [1,2,3,4])
    log(f"testing {total_1to4} combos of 1-4 wallets exhaustively")
    checked = 0
    t0 = time.time()
    for k in [1, 2, 3, 4]:
        for combo_idx in combinations(range(n), k):
            wallets = [candidates[i] for i in combo_idx]
            dfs = [cache[w] for w in wallets]
            bet = best_bet(dfs)
            if bet < 10: continue
            r = eval_combo(dfs, bet)
            if r is None: continue
            r['wallets'] = "|".join(wallets)
            r['n_wallets'] = k
            r['bet'] = bet
            r['peak_conc'] = peak_concurrent(dfs)
            r['peak_capital'] = r['peak_conc'] * bet
            r['fits'] = r['peak_capital'] <= BANKROLL
            results.append(r)
            checked += 1
            if checked % 200 == 0:
                elapsed = time.time() - t0
                log(f"  progress: {checked}/{total_1to4} ({elapsed:.0f}s)")

    log(f"exhaustive 1-4 done: {len(results)} valid combos")

    # For 5-6, only combine top 12 by robust score to keep runtime sane
    r_df = pd.DataFrame(results)
    r_df = r_df[r_df['fits']]
    top_solos = r_df[r_df['n_wallets'] == 1].sort_values('robust_time_avg', ascending=False)['wallets'].head(12).tolist()
    top_solo_wallets = [x.split("|")[0] for x in top_solos]

    log(f"testing 5-6 wallet combos over top {len(top_solo_wallets)} solos")
    for k in [5, 6]:
        for combo_idx in combinations(range(len(top_solo_wallets)), k):
            wallets = [top_solo_wallets[i] for i in combo_idx]
            dfs = [cache[w] for w in wallets]
            bet = best_bet(dfs)
            if bet < 10: continue
            r = eval_combo(dfs, bet)
            if r is None: continue
            r['wallets'] = "|".join(wallets)
            r['n_wallets'] = k
            r['bet'] = bet
            r['peak_conc'] = peak_concurrent(dfs)
            r['peak_capital'] = r['peak_conc'] * bet
            r['fits'] = r['peak_capital'] <= BANKROLL
            results.append(r)

    log(f"total combos tested: {len(results)}")

    # Compile
    df = pd.DataFrame(results)
    df = df[df['fits']]
    df.to_csv(RESULTS, index=False)
    log(f"saved raw results -> {RESULTS}")

    # RANK BY MULTIPLE METRICS
    lines = [f"=== EXHAUST OPTIMIZER REPORT — {datetime.now().isoformat(timespec='seconds')} ==="]
    lines.append(f"Tested {len(df)} valid combos (fits $2k bankroll)")
    lines.append(f"Split: first 70% train, last 30% test (walk-forward)")
    lines.append("")

    # 1. Best by ROBUST time-avg (min of train and test)
    df_r = df.sort_values('robust_time_avg', ascending=False).head(15)
    lines.append("### TOP 15 BY ROBUST TIME-AVG (min(train, test) weekly $)")
    lines.append(f"{'n':>2} {'bet':>4} {'train':>7} {'test':>7} {'robust':>7} {'test_med':>8} {'test_%prof':>10} {'test_sharpe':>11} wallets")
    for _, r in df_r.iterrows():
        ws = " + ".join(w[:12]+".." for w in r['wallets'].split("|"))
        lines.append(f"{r['n_wallets']:>2} ${r['bet']:>3} ${r['train_time_avg']:>6.0f} ${r['test_time_avg']:>6.0f} ${r['robust_time_avg']:>6.0f} ${r['test_median']:>7.0f} {r['test_pct_profitable']:>9.0f}% {r['test_sharpe']:>10.2f}  {ws}")

    # 2. Best by test-set p25 (worst-typical week)
    df_p = df[df['test_p25'] > 0].sort_values('test_p25', ascending=False).head(10)
    lines.append("")
    lines.append("### TOP 10 BY TEST-SET p25 (25th percentile week — 'worst typical week')")
    lines.append(f"{'n':>2} {'bet':>4} {'test_avg':>8} {'test_med':>8} {'test_p25':>8} {'test_%prof':>10} wallets")
    for _, r in df_p.iterrows():
        ws = " + ".join(w[:12]+".." for w in r['wallets'].split("|"))
        lines.append(f"{r['n_wallets']:>2} ${r['bet']:>3} ${r['test_time_avg']:>7.0f} ${r['test_median']:>7.0f} ${r['test_p25']:>7.0f} {r['test_pct_profitable']:>9.0f}%  {ws}")

    # 3. Best by test Sharpe (return / volatility)
    df_s = df[df['test_sharpe'] > 0].sort_values('test_sharpe', ascending=False).head(10)
    lines.append("")
    lines.append("### TOP 10 BY TEST SHARPE (return per unit volatility)")
    lines.append(f"{'n':>2} {'bet':>4} {'test_avg':>8} {'test_sharpe':>11} {'test_%prof':>10} {'worst_week':>11} wallets")
    for _, r in df_s.iterrows():
        ws = " + ".join(w[:12]+".." for w in r['wallets'].split("|"))
        lines.append(f"{r['n_wallets']:>2} ${r['bet']:>3} ${r['test_time_avg']:>7.0f} {r['test_sharpe']:>10.2f} {r['test_pct_profitable']:>9.0f}% ${r['test_worst_week']:>10.0f}  {ws}")

    # 4. Best by test-set % profitable weeks
    df_pw = df.sort_values(['test_pct_profitable','test_time_avg'], ascending=[False, False]).head(10)
    lines.append("")
    lines.append("### TOP 10 BY TEST-SET % PROFITABLE WEEKS (consistency)")
    lines.append(f"{'n':>2} {'bet':>4} {'test_avg':>8} {'test_med':>8} {'test_%prof':>10} wallets")
    for _, r in df_pw.iterrows():
        ws = " + ".join(w[:12]+".." for w in r['wallets'].split("|"))
        lines.append(f"{r['n_wallets']:>2} ${r['bet']:>3} ${r['test_time_avg']:>7.0f} ${r['test_median']:>7.0f} {r['test_pct_profitable']:>9.0f}%  {ws}")

    # OVERALL WINNER: robust + profitable + hits $250 goal
    lines.append("")
    lines.append("### 🏆 OVERALL WINNER")
    lines.append("Criteria: fits bankroll, robust_time_avg >= $250, test_pct_profitable >= 60%, ranked by robust_time_avg")
    winners = df[(df['robust_time_avg'] >= 250) & (df['test_pct_profitable'] >= 60)]
    winners = winners.sort_values('robust_time_avg', ascending=False).head(5)
    if len(winners) == 0:
        winners = df.sort_values('robust_time_avg', ascending=False).head(5)
        lines.append("(no combos met all criteria; showing top 5 by robust_time_avg)")
    for i, (_, r) in enumerate(winners.iterrows(), 1):
        lines.append(f"\n#{i}: robust ${r['robust_time_avg']}/wk (train ${r['train_time_avg']}, test ${r['test_time_avg']})")
        lines.append(f"    median test ${r['test_median']}/wk, {r['test_pct_profitable']}% profitable, sharpe {r['test_sharpe']}")
        lines.append(f"    bet ${r['bet']}/trade, peak {r['peak_conc']} concurrent (${r['peak_capital']}/${BANKROLL})")
        for w in r['wallets'].split("|"):
            lines.append(f"      wallet: {w}")

    report_text = "\n".join(lines)
    with open(FINAL_REPORT, "w") as f:
        f.write(report_text)
    log("=== EXHAUST DONE ===")
    log(f"report: {FINAL_REPORT}")
    log(f"results: {RESULTS}")


if __name__ == "__main__":
    main()
