#!/usr/bin/env python3
"""Build the actual portfolio recommendation from rewards_universe.csv.

Filters:
  - has real book depth on both sides (excludes phantom 100%-share entries)
  - excludes short-fused news markets (Iran, WTI, Fed decisions near event)
  - excludes markets resolving in <14 days
  - keeps meaningful share (>= 2%) and meaningful daily reward (>= $10)
"""
import argparse
import pandas as pd
import re

# categories with high news-shock / correlated-move risk
DANGER_PATTERNS = re.compile(
    r"iran|israel|houthi|hormuz|yemen|iraq|lebanon|russia|ukraine|"
    r"wti|crude oil|natural gas|silver|gold|s&p|spy|nasdaq|"
    r"fed |federal reserve|rate hike|rate cut|inflation|jobs report|"
    r"lebron|kawhi|kevin durant|nba trade|nfl trade|"
    r"opec|barrel|copper|amzn|tesla|nvda|apple|"
    r"trump meet|netanyahu|putin",
    re.I
)


def is_safe(question):
    return not DANGER_PATTERNS.search(str(question))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv",         default="rewards_universe.csv")
    p.add_argument("--min-dtr",     type=float, default=14.0)
    p.add_argument("--min-share",   type=float, default=2.0)
    p.add_argument("--min-daily",   type=float, default=5.0)
    p.add_argument("--min-pool",    type=float, default=20.0)
    p.add_argument("--n",           type=int,   default=25,
                   help="target portfolio size")
    p.add_argument("--allow-danger",action="store_true")
    p.add_argument("--out",         default="portfolio.csv")
    a = p.parse_args()

    R = pd.read_csv(a.csv)
    print(f"universe: {len(R)} reward-paying markets")

    # depth guard: require both sides to have actual liquidity (drop phantoms)
    R = R[(R.bid_qual.fillna(0) > 0) & (R.ask_qual.fillna(0) > 0)]
    print(f"  after real-depth filter:      {len(R)}")

    R = R[R.dtr.fillna(0) >= a.min_dtr]
    print(f"  after dtr >= {a.min_dtr}d:            {len(R)}")

    R = R[R.share_pct >= a.min_share]
    print(f"  after share >= {a.min_share}%:          {len(R)}")

    R = R[R["daily_$"] >= a.min_daily]
    print(f"  after daily_$ >= ${a.min_daily}:         {len(R)}")

    R = R[R.daily_pool >= a.min_pool]
    print(f"  after pool >= ${a.min_pool}/day:        {len(R)}")

    if not a.allow_danger:
        R = R[R.question.apply(is_safe)]
        print(f"  after news-shock filter:      {len(R)}")

    # dedupe same market posted twice
    R = R.drop_duplicates(subset=["question", "mid"])
    print(f"  after dedupe:                 {len(R)}")

    R = R.sort_values("weekly_$", ascending=False).head(a.n)
    R.to_csv(a.out, index=False)

    print(f"\n=== RECOMMENDED PORTFOLIO ({len(R)} markets) ===")
    cols = ["question", "daily_pool", "mid", "min_size", "dtr",
            "share_pct", "daily_$", "weekly_$", "cap_used"]
    with pd.option_context("display.width", 260, "display.max_colwidth", 55):
        print(R[cols].to_string(index=False))

    print(f"\n=== TOTALS ===")
    print(f"  total capital:   ${R.cap_used.sum():,.2f}")
    print(f"  weekly gross:    ${R['weekly_$'].sum():,.2f}")
    print(f"  weekly net (assuming 30% adverse-fill haircut): "
          f"${R['weekly_$'].sum() * 0.7:,.2f}")
    print(f"  vs. $250/week goal: {R['weekly_$'].sum()*0.7/250*100:.0f}%")
    print(f"\nsaved -> {a.out}")


if __name__ == "__main__":
    main()
