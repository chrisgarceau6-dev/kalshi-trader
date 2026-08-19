# Kalshi incentive programs — research in progress

Reproduce: `python3 fetch_programs.py` → `analyze_corpus.py` → `volume_math.py` → `scan_live.py`.
All endpoints are **public/unauthenticated**: `GET /trade-api/v2/incentive_programs`
(filters: `status=all|active|upcoming|closed|paid_out`, `type=all|liquidity|volume`).

## Established so far (all from live API, 2026-08-19)

- **145,145 programs** published, 2025-09-18 → 2026-09-05. `liquidity` $9.01M, `volume` $0.91M.
  Blended **$28,145/day** exchange-wide. `period_reward` is **centi-cents** (1/10,000 $).
- **The bot's universe is almost entirely uncovered.** SOL/DOGE/BNB/XRP/HYPE/NEAR: *zero*
  programs, ever. BTC15M/ETH15M had `volume` programs but they **ended 2026-05-12**, and
  **zero volume programs are active exchange-wide right now**. WTI15M has liquidity programs.
- **Volume incentive math** (`volume_math.py`): pool $20 vs median market volume 1.68M
  contracts (BTC) → **$0.00001/contract**; the $0.005/contract cap never binds because
  dilution binds first. Worth **$0.41/day** to the bot *if the programs still existed*. They don't.
- **LIP formula** (help.kalshi.com/en/articles/13823851): reward = (your score ÷ all scores)
  × pool × (counted snapshots ÷ all). Raw score = size × `discount_factor^ticks_below_reference`.
  Reference price = walking down from best bid, first level where cumulative size ≥ target/5.
  **Both sides must independently meet target size or the snapshot pays nobody.**
- **3,516 liquidity programs are live right now**; top 140 carry **$322/hour** of pool.
- **Two regimes found.** Most inspected markets are genuine two-sided books. But a subset are
  *dead* markets — e.g. `KXIDPOTATO-26NOV10-T138.38`: **95¢-wide spread, zero volume, zero OI,
  4,325 contracts resting at 2¢ and 1,533 at 3¢ on the other side.** Because the reference
  price walks down to the first level holding target/5, those penny orders take **full credit**.
  Capital is trivial and fills are near-impossible (a 2¢ YES + 3¢ NO pair would be a 95¢
  arbitrage for anyone selling into it), so someone is already farming these.

## Not yet established (see chat for next steps)
Regime sizing at scale, competition stability over time, and whether this is permitted —
Kalshi's terms reserve the right to revoke for "abusive behavior and fake trading".
