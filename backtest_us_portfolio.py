#!/usr/bin/env python3
"""Backtest the 4-wallet US-accessible portfolio at $75/trade.

Uses the per-wallet trade history CSVs (_tmp_*.csv, threshold=0.03)
and the poly_us_classifier to filter to US-accessible markets only.

Outputs:
  - backtest_us_portfolio.png  (equity curve + weekly PnL bar chart)
  - backtest_us_portfolio.txt  (summary stats)
"""
import sys
from pathlib import Path
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.gridspec import GridSpec
from poly_us_classifier import is_accessible

BASE = Path(__file__).parent
BET_SIZE = 75          # dollars per copy trade
STARTING_CAPITAL = 2000

WALLETS = {
    "workhorse (GrizzliesSuck)": BASE / "_tmp_0x412fe1_0.03.csv",
    "fbf-safe (VPenguin)":       BASE / "_tmp_0xfbf3d5_0.03.csv",
    "sentrio":                   BASE / "_tmp_0xdb83e8_0.03.csv",
    "sunguyen86":                BASE / "_tmp_0x7d83c9_0.03.csv",
}


def load_wallet(name, path):
    df = pd.read_csv(path, parse_dates=["entry_ts", "exit_ts"])
    df["wallet"] = name
    return df


def simulate_trade(row, bet=BET_SIZE):
    """Return dollar PnL for a single trade at fixed $bet stake."""
    ep = float(row["entry_price"])
    xp = float(row["exit_price"])
    if ep <= 0 or ep >= 1:
        return 0.0
    shares = bet / ep
    return shares * (xp - ep)


MAX_BET = 500  # practical Polymarket liquidity cap per trade

def simulate_compounding(trades_df, starting_capital=STARTING_CAPITAL, base_bet=BET_SIZE):
    """Weekly-rebalanced compounding: re-size bet at start of each week, cap at MAX_BET."""
    bet_pct = base_bet / starting_capital  # 75/2000 = 3.75%
    balance = starting_capital
    balances = []
    bets_used = []
    current_week = None
    week_bet = base_bet
    for _, row in trades_df.iterrows():
        week = row["exit_ts"].to_period("W")
        if week != current_week:
            week_bet = min(balance * bet_pct, MAX_BET)
            current_week = week
        ep = float(row["entry_price"])
        xp = float(row["exit_price"])
        if ep > 0 and ep < 1:
            pnl = (week_bet / ep) * (xp - ep)
        else:
            pnl = 0.0
        balance += pnl
        balances.append(balance)
        bets_used.append(week_bet)
    return balances, bets_used


def us_filter(df):
    mask = []
    for title in df["title"]:
        accessible, confidence, _ = is_accessible(str(title))
        mask.append(accessible is not False or confidence < 0.70)
    return df[mask].copy()


def main():
    frames = []
    for name, path in WALLETS.items():
        if not path.exists():
            print(f"WARNING: {path.name} not found, skipping {name}")
            continue
        df = load_wallet(name, path)
        before = len(df)
        df = us_filter(df)
        after = len(df)
        print(f"  {name}: {before} total → {after} US-accessible ({100*after/before:.0f}%)")
        frames.append(df)

    all_trades = pd.concat(frames, ignore_index=True)
    all_trades = all_trades.sort_values("exit_ts").reset_index(drop=True)

    # Limit to last 6 months from last exit date
    cutoff = all_trades["exit_ts"].max() - pd.Timedelta(days=183)
    all_trades = all_trades[all_trades["exit_ts"] >= cutoff].copy()

    all_trades["pnl"] = all_trades.apply(simulate_trade, axis=1)
    all_trades["cumulative_pnl"] = all_trades["pnl"].cumsum()
    all_trades["account_balance"] = STARTING_CAPITAL + all_trades["cumulative_pnl"]

    # Compounding simulation
    compound_balances, compound_bets = simulate_compounding(all_trades)
    all_trades["compound_balance"] = compound_balances
    all_trades["compound_pnl"]     = all_trades["compound_balance"].diff().fillna(
        all_trades["compound_balance"].iloc[0] - STARTING_CAPITAL)
    all_trades["week"] = all_trades["exit_ts"].dt.to_period("W").apply(lambda p: p.start_time)

    weekly = all_trades.groupby("week")["pnl"].sum().reset_index()
    weekly.columns = ["week", "weekly_pnl"]
    weekly_compound = all_trades.groupby("week")["compound_pnl"].sum().reset_index()
    weekly_compound.columns = ["week", "weekly_pnl_compound"]
    weekly = weekly.merge(weekly_compound, on="week")

    n_trades        = len(all_trades)
    total_pnl       = all_trades["pnl"].sum()
    total_compound  = all_trades["compound_balance"].iloc[-1] - STARTING_CAPITAL
    win_rate        = (all_trades["pnl"] > 0).mean()
    avg_win         = all_trades.loc[all_trades["pnl"] > 0, "pnl"].mean()
    avg_loss        = all_trades.loc[all_trades["pnl"] <= 0, "pnl"].mean()
    avg_week        = weekly["weekly_pnl"].mean()
    median_week     = weekly["weekly_pnl"].median()
    best_week       = weekly["weekly_pnl"].max()
    worst_week      = weekly["weekly_pnl"].min()
    weeks_pos       = (weekly["weekly_pnl"] > 0).sum()
    n_weeks         = len(weekly)
    avg_week_cmp    = weekly["weekly_pnl_compound"].mean()

    # Max drawdown on cumulative PnL
    cum = all_trades["cumulative_pnl"]
    rolling_max = cum.cummax()
    drawdown = cum - rolling_max
    max_dd = drawdown.min()

    summary = f"""
=======================================================
  US PORTFOLIO BACKTEST — last 6 months @ ${BET_SIZE}/trade
=======================================================
  Wallets : {', '.join(WALLETS.keys())}
  Period  : {all_trades['exit_ts'].min().date()} → {all_trades['exit_ts'].max().date()}
  Trades  : {n_trades}
  Win rate: {win_rate:.1%}
  Starting capital: ${STARTING_CAPITAL:,}
  Flat $75/trade → ending balance : ${STARTING_CAPITAL + total_pnl:,.0f}
  Compounding     → ending balance : ${STARTING_CAPITAL + total_compound:,.0f}
  Total PnL (flat): ${total_pnl:,.0f}

  --- Weekly ---
  Avg/week   : ${avg_week:,.0f}
  Median/week: ${median_week:,.0f}
  Best week  : ${best_week:,.0f}
  Worst week : ${worst_week:,.0f}
  Positive weeks: {weeks_pos}/{n_weeks}

  --- Risk ---
  Avg win per trade : ${avg_win:,.0f}
  Avg loss per trade: ${avg_loss:,.0f}
  Max drawdown      : ${max_dd:,.0f}
=======================================================
"""
    print(summary)
    (BASE / "backtest_us_portfolio.txt").write_text(summary)

    # ── Chart ─────────────────────────────────────────────────────
    fig = plt.figure(figsize=(13, 8), facecolor="#0d1117")
    gs = GridSpec(2, 1, figure=fig, hspace=0.45, height_ratios=[3, 2])

    ax1 = fig.add_subplot(gs[0])
    ax2 = fig.add_subplot(gs[1])

    # colour palette
    bg    = "#0d1117"
    green = "#3fb950"
    red   = "#f85149"
    blue  = "#58a6ff"
    gray  = "#8b949e"
    white = "#e6edf3"

    for ax in (ax1, ax2):
        ax.set_facecolor("#161b22")
        for spine in ax.spines.values():
            spine.set_edgecolor("#30363d")
        ax.tick_params(colors=gray, labelsize=9)
        ax.xaxis.label.set_color(gray)
        ax.yaxis.label.set_color(gray)

    # Account balance curves
    dates = pd.to_datetime(all_trades["exit_ts"])
    flat_vals     = all_trades["account_balance"].values
    compound_vals = all_trades["compound_balance"].values

    # Flat curve (dimmed)
    ax1.plot(dates, flat_vals, color=gray, linewidth=1.2, zorder=4,
             linestyle="--", label=f"Flat ${BET_SIZE}/trade → ${flat_vals[-1]:,.0f}")

    # Compounding curve (highlighted)
    ax1.fill_between(dates, compound_vals, STARTING_CAPITAL,
                     where=(compound_vals >= STARTING_CAPITAL), color=green, alpha=0.12)
    ax1.plot(dates, compound_vals, color=green, linewidth=2.0, zorder=5,
             label=f"Compounding (3.75% of balance) → ${compound_vals[-1]:,.0f}")

    ax1.axhline(STARTING_CAPITAL, color=gray, linewidth=0.5, linestyle=":")
    ax1.set_title(f"Account Balance — $2k start, weekly rebalance, ${MAX_BET} max bet (6-month backtest)",
                  color=white, fontsize=11, pad=10)
    ax1.set_ylabel("Account Balance ($)", color=gray)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax1.xaxis.set_major_locator(mdates.MonthLocator())
    ax1.legend(facecolor="#161b22", edgecolor="#30363d", labelcolor=white, fontsize=8)

    # Annotate compounding final
    ax1.annotate(f"${compound_vals[-1]:,.0f}",
                 xy=(dates.iloc[-1], compound_vals[-1]),
                 xytext=(10, 0), textcoords="offset points",
                 color=green, fontsize=10, fontweight="bold")

    # Weekly bar chart — compounding
    bar_colors = [green if v >= 0 else red for v in weekly["weekly_pnl_compound"]]
    ax2.bar(weekly["week"], weekly["weekly_pnl_compound"],
            width=5, color=bar_colors, alpha=0.85)
    ax2.axhline(0, color=gray, linewidth=0.6, linestyle="--")
    ax2.axhline(250, color=blue, linewidth=0.8, linestyle="--", alpha=0.7,
                label="$250 target")
    ax2.set_title(f"Weekly PnL (compounding)  |  avg ${avg_week_cmp:,.0f}/wk  |  {win_rate:.0%} win rate  |  {n_trades} trades",
                  color=white, fontsize=10, pad=8)
    ax2.set_ylabel("PnL ($)", color=gray)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator())
    ax2.legend(facecolor="#161b22", edgecolor="#30363d",
               labelcolor=blue, fontsize=8)

    plt.savefig(BASE / "backtest_us_portfolio.png",
                dpi=150, bbox_inches="tight", facecolor=bg)
    print(f"Chart saved → {BASE / 'backtest_us_portfolio.png'}")


if __name__ == "__main__":
    main()
