#!/usr/bin/env python3
"""Find 'rocket' wallets (rare + high per-trade edge) and combine with 'boosters'."""
import glob
from pathlib import Path
import pandas as pd
from itertools import product

BASE = Path(__file__).parent
BANKROLL = 2000
SLIP = 0.03

# Load screener report
report = pd.read_csv(BASE / "wallet_hunt_report.csv")
# Broader filter: include ALL 2/3+ (even n<200) since we WANT low-n rockets
report = report[(report['hard_score'] >= 2) & (report['t_stat'] >= 3)]


def load_wallet_trades(wallet):
    prefix = wallet[:8]
    path = BASE / f"_tmp_{prefix}_0.03.csv"
    if not path.exists(): return None
    try:
        df = pd.read_csv(path)
        df['entry_ts'] = pd.to_datetime(df['entry_ts'])
        df['exit_ts'] = pd.to_datetime(df['exit_ts'])
        return df
    except:
        return None


def pnl_at_size(df, bet):
    def per(row):
        e = min(row['entry_price'] + SLIP, 0.99)
        x = max(row['exit_price'] - SLIP, 0.01)
        lp = bet/e * (x - e)
        sp = bet/(1-e) * ((1-x) - (1-e))
        return lp if abs(lp) >= abs(sp) else sp
    return df.apply(per, axis=1)


# Compute per-wallet archetype metrics
rows = []
for _, r in report.iterrows():
    df = load_wallet_trades(r['wallet'])
    if df is None or len(df) < 5: continue
    weeks_history = (df['entry_ts'].max() - df['entry_ts'].min()).days / 7
    if weeks_history < 20: continue

    # PnL at $100/trade flat (standardized comparison)
    pnl = pnl_at_size(df, 100)
    total = pnl.sum()
    per_trade = total / len(df)
    win_rate = (pnl > 0).mean() * 100
    weekly_trades = len(df) / weeks_history

    rows.append({
        'wallet': r['wallet'],
        'hard_score': r['hard_score'],
        't_stat': r['t_stat'],
        'n_trades': len(df),
        'weeks_history': round(weeks_history, 1),
        'weekly_trade_freq': round(weekly_trades, 2),
        'total_pnl_100': round(total, 2),
        'pnl_per_trade': round(per_trade, 2),
        'win_rate_pct': round(win_rate, 1),
        'weekly_pnl_avg': round(total / weeks_history, 2),
    })

df = pd.DataFrame(rows)

# ROCKETS: infrequent trader (<4 trades/wk), high $ per trade OR high win rate
rockets = df[(df['weekly_trade_freq'] < 4) &
             ((df['pnl_per_trade'] > 30) | (df['win_rate_pct'] > 65))]
rockets = rockets.sort_values('pnl_per_trade', ascending=False)

# BOOSTERS: frequent + consistent
boosters = df[(df['weekly_trade_freq'] >= 3) & (df['weekly_pnl_avg'] > 30)]
boosters = boosters.sort_values('weekly_pnl_avg', ascending=False)

print(f"=== ROCKET candidates (low freq, high per-trade $, high win rate) — {len(rockets)} ===")
print(rockets[['wallet','n_trades','weeks_history','weekly_trade_freq','pnl_per_trade','win_rate_pct','weekly_pnl_avg']].head(15).to_string(index=False))

print(f"\n=== BOOSTER candidates (high freq, consistent PnL, high win rate) — {len(boosters)} ===")
print(boosters[['wallet','n_trades','weeks_history','weekly_trade_freq','pnl_per_trade','win_rate_pct','weekly_pnl_avg']].head(10).to_string(index=False))

# Test rocket × booster combos
print(f"\n=== ROCKET × BOOSTER COMBOS ===")
combos = []
for _, rocket in rockets.head(8).iterrows():
    for _, booster in boosters.head(8).iterrows():
        if rocket['wallet'] == booster['wallet']: continue
        # Load both
        df_r = load_wallet_trades(rocket['wallet']); df_r['w'] = 'R'
        df_b = load_wallet_trades(booster['wallet']); df_b['w'] = 'B'
        # Peak concurrent
        events = []
        for d in [df_r, df_b]:
            for _, row in d.iterrows():
                events.append((row['entry_ts'], +1))
                events.append((row['exit_ts'], -1))
        events.sort()
        curr, peak = 0, 0
        for _, delta in events: curr += delta; peak = max(peak, curr)
        # Fit bet size to bankroll
        bet = min(100, (BANKROLL // max(peak,1)) // 5 * 5)
        if bet < 20: continue
        # PnL at that bet size
        df_r['pnl'] = pnl_at_size(df_r, bet)
        df_b['pnl'] = pnl_at_size(df_b, bet)
        combined = pd.concat([df_r, df_b], ignore_index=True)
        combined['week'] = combined['entry_ts'].dt.to_period('W')
        weekly = combined.groupby('week')['pnl'].sum()
        weeks = max(1, (combined['entry_ts'].max() - combined['entry_ts'].min()).days / 7)
        combos.append({
            'rocket': rocket['wallet'][:12] + '...',
            'booster': booster['wallet'][:12] + '...',
            'bet': bet,
            'peak_conc': peak,
            'weekly_avg': round(combined['pnl'].sum() / weeks, 2),
            'weekly_median': round(weekly.median(), 2),
            'weekly_p75': round(weekly.quantile(0.75), 2),
            'weekly_max': round(weekly.max(), 2),
            'weekly_min': round(weekly.min(), 2),
            'win_rate': round((weekly > 0).mean() * 100, 1),
            'active_weeks': len(weekly),
            'n_trades': len(combined),
        })

if not combos:
    print("no rocket+booster combos found. widen filters.")
    import sys; sys.exit()
c = pd.DataFrame(combos).sort_values('weekly_avg', ascending=False)
print(c.head(15).to_string(index=False))

# Full addresses of best 5
print(f"\n=== TOP 5 ROCKET+BOOSTER (full addresses) ===")
for i, row in c.head(5).iterrows():
    r_full = df[df['wallet'].str.startswith(row['rocket'][:10])].iloc[0]['wallet']
    b_full = df[df['wallet'].str.startswith(row['booster'][:10])].iloc[0]['wallet']
    print(f"\n#{i+1}: weekly avg ${row['weekly_avg']}, median ${row['weekly_median']}, win rate {row['win_rate']}%")
    print(f"  Rocket:  {r_full}")
    print(f"  Booster: {b_full}")
    print(f"  Bet size: ${row['bet']}, peak concurrent: {row['peak_conc']}")
