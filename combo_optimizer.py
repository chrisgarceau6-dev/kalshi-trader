#!/usr/bin/env python3
"""Find the best combination of copy-trading wallets to hit $250/wk on $2k.

Loads all backtest CSVs from the hunt, simulates 1/2/3/4-wallet portfolios,
computes actual weekly PnL considering capital constraints and concurrency.
"""
import glob, os, sys
from itertools import combinations
from pathlib import Path
import pandas as pd
import numpy as np

BASE = Path(__file__).parent
BANKROLL = 2000

MIN_WEEKS_HISTORY = 20  # need at least 20 weeks of trade history
# Load screener results
report = pd.read_csv(BASE / "wallet_hunt_report.csv")
report = report[(report['hard_score'] >= 2) & (report['n'] >= 100) & (report['t_stat'] >= 3)]

# Filter by actual timespan
def wallet_timespan(wallet):
    prefix = wallet[:8]
    path = BASE / f"_tmp_{prefix}_0.03.csv"
    if not path.exists(): return 0
    try:
        df = pd.read_csv(path)
        df['entry_ts'] = pd.to_datetime(df['entry_ts'])
        return (df['entry_ts'].max() - df['entry_ts'].min()).days / 7
    except:
        return 0

report['weeks_history'] = report['wallet'].apply(wallet_timespan)
report = report[report['weeks_history'] >= MIN_WEEKS_HISTORY]
print(f"Candidate wallets (2/3+ HARD, n>=100, t>=3, >={MIN_WEEKS_HISTORY}wk history): {len(report)}")


def load_wallet_trades(wallet):
    """Load the 3c-slippage backtest CSV for a wallet."""
    prefix = wallet[:8]
    path = BASE / f"_tmp_{prefix}_0.03.csv"
    if not path.exists(): return None
    try:
        df = pd.read_csv(path)
        df['entry_ts'] = pd.to_datetime(df['entry_ts'])
        df['exit_ts'] = pd.to_datetime(df['exit_ts'])
        return df
    except Exception:
        return None


def pnl_at_size(df, bet_dollars, slip=0.03):
    """Compute per-trade PnL assuming flat $bet copy sizing."""
    def per_trade(row):
        entry = min(row['entry_price'] + slip, 0.99)
        exit_p = max(row['exit_price'] - slip, 0.01)
        long_p  = bet_dollars/entry * (exit_p - entry)
        short_p = bet_dollars/(1-entry) * ((1-exit_p) - (1-entry))
        return long_p if abs(long_p) >= abs(short_p) else short_p
    return df.apply(per_trade, axis=1)


def peak_concurrent(dfs):
    """Peak simultaneous open positions across combined wallets."""
    events = []
    for df in dfs:
        for _, r in df.iterrows():
            events.append((r['entry_ts'], +1))
            events.append((r['exit_ts'], -1))
    events.sort()
    curr, peak = 0, 0
    for _, delta in events:
        curr += delta
        peak = max(peak, curr)
    return peak


def combo_stats(wallets, bet_size):
    """Given wallet addresses + a flat bet size, compute combined weekly PnL."""
    dfs = []
    for w in wallets:
        d = load_wallet_trades(w)
        if d is None: return None
        d = d.copy()
        d['copy_pnl'] = pnl_at_size(d, bet_size)
        d['wallet'] = w
        dfs.append(d)
    peak = peak_concurrent(dfs)
    combined = pd.concat(dfs, ignore_index=True)
    combined['week'] = combined['entry_ts'].dt.to_period('W')
    weekly = combined.groupby('week')['copy_pnl'].sum()
    tstart, tend = combined['entry_ts'].min(), combined['entry_ts'].max()
    weeks_total = max(1, (tend - tstart).days / 7)
    return {
        'wallets': tuple(sorted(wallets)),
        'n_wallets': len(wallets),
        'bet_size': bet_size,
        'peak_concurrent': peak,
        'peak_capital': peak * bet_size,
        'fits_bankroll': peak * bet_size <= BANKROLL,
        'total_trades': len(combined),
        'active_weeks': len(weekly),
        'timespan_weeks': round(weeks_total, 1),
        'weekly_mean': round(weekly.mean(), 2),           # avg when trades happen
        'weekly_time_avg': round(combined['copy_pnl'].sum() / weeks_total, 2),  # spread across all weeks
        'weekly_median': round(weekly.median(), 2),
        'weekly_p25': round(weekly.quantile(0.25), 2),
        'weekly_p75': round(weekly.quantile(0.75), 2),
        'weekly_min': round(weekly.min(), 2),
        'weekly_max': round(weekly.max(), 2),
        'pct_profitable_weeks': round((weekly > 0).mean() * 100, 1),
        'total_pnl': round(combined['copy_pnl'].sum(), 2),
    }


def best_bet_size(wallets):
    """Find largest flat bet size such that peak concurrent × bet <= bankroll."""
    dfs = [load_wallet_trades(w) for w in wallets]
    if any(d is None for d in dfs): return None
    peak = peak_concurrent(dfs)
    if peak == 0: return None
    # Round down to nearest $5 for cleanliness
    max_bet = (BANKROLL // peak) // 5 * 5
    return max(5, min(max_bet, 100))


candidates = report['wallet'].tolist()
print(f"\nAnalyzing candidates:")
for _, r in report.iterrows():
    print(f"  {r['wallet']} hard={r['hard_score']} t={r['t_stat']:.1f} n={r['n']} pnl_c=${r['pnl_corrected']:,.0f}")

results = []

# Solo
print("\n=== SOLO WALLETS ===")
for w in candidates:
    bet = best_bet_size([w])
    if bet is None: continue
    s = combo_stats([w], bet)
    if s: results.append(s)

# 2-wallet combos
print(f"\n=== 2-WALLET COMBOS ({len(candidates)*(len(candidates)-1)//2}) ===")
for combo in combinations(candidates, 2):
    bet = best_bet_size(list(combo))
    if bet is None: continue
    s = combo_stats(list(combo), bet)
    if s: results.append(s)

# 3-wallet combos (only top by solo weekly avg to keep runtime sane)
solo_ranked = sorted([r for r in results if r['n_wallets']==1],
                     key=lambda x: -x['weekly_time_avg'])[:12]
top12 = [w for r in solo_ranked for w in r['wallets']]
print(f"\n=== 3-WALLET COMBOS (top 12 solos, {len(list(combinations(top12,3)))} combos) ===")
for combo in combinations(top12, 3):
    bet = best_bet_size(list(combo))
    if bet is None: continue
    s = combo_stats(list(combo), bet)
    if s: results.append(s)

# 4-wallet combos (only top 8)
top8 = [w for r in solo_ranked[:8] for w in r['wallets']]
print(f"\n=== 4-WALLET COMBOS (top 8 solos, {len(list(combinations(top8,4)))} combos) ===")
for combo in combinations(top8, 4):
    bet = best_bet_size(list(combo))
    if bet is None: continue
    s = combo_stats(list(combo), bet)
    if s: results.append(s)

df = pd.DataFrame(results)
df.to_csv(BASE / "combo_optimizer_results.csv", index=False)
print(f"\nsaved {len(df)} combos -> combo_optimizer_results.csv")

# Best solo
print("\n=== TOP 10 SOLO BY WEEKLY TIME-AVG ===")
solo = df[df['n_wallets']==1].sort_values('weekly_time_avg', ascending=False).head(10)
print(solo[['wallets','bet_size','peak_concurrent','weekly_time_avg','weekly_median','weekly_p75','pct_profitable_weeks','total_trades']].to_string(index=False))

# Best 2
print("\n=== TOP 10 2-WALLET BY WEEKLY TIME-AVG (fits bankroll) ===")
c2 = df[(df['n_wallets']==2) & df['fits_bankroll']].sort_values('weekly_time_avg', ascending=False).head(10)
print(c2[['wallets','bet_size','peak_concurrent','weekly_time_avg','weekly_median','weekly_p75','pct_profitable_weeks']].to_string(index=False))

# Best 3
print("\n=== TOP 10 3-WALLET BY WEEKLY TIME-AVG (fits bankroll) ===")
c3 = df[(df['n_wallets']==3) & df['fits_bankroll']].sort_values('weekly_time_avg', ascending=False).head(10)
print(c3[['wallets','bet_size','peak_concurrent','weekly_time_avg','weekly_median','weekly_p75','pct_profitable_weeks']].to_string(index=False))

# Best 4
print("\n=== TOP 5 4-WALLET BY WEEKLY TIME-AVG (fits bankroll) ===")
c4 = df[(df['n_wallets']==4) & df['fits_bankroll']].sort_values('weekly_time_avg', ascending=False).head(5)
print(c4[['wallets','bet_size','peak_concurrent','weekly_time_avg','weekly_median','weekly_p75','pct_profitable_weeks']].to_string(index=False))

# Grand winner
print("\n=== BEST OVERALL COMBO ===")
best = df[df['fits_bankroll']].sort_values('weekly_time_avg', ascending=False).iloc[0]
print(f"Wallets: {best['wallets']}")
print(f"Bet size: ${best['bet_size']} per trade")
print(f"Peak concurrent: {best['peak_concurrent']} positions")
print(f"Peak capital: ${best['peak_capital']}/${BANKROLL}")
print(f"Weekly time-avg: ${best['weekly_time_avg']}")
print(f"Weekly median: ${best['weekly_median']}")
print(f"Weekly p75: ${best['weekly_p75']}")
print(f"Profitable weeks: {best['pct_profitable_weeks']}%")
print(f"Total trades: {best['total_trades']}")
print(f"Total PnL over {best['timespan_weeks']}wks: ${best['total_pnl']:,.0f}")
