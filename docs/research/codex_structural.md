# Kalshi structural/mechanical strategy audit

**As of:** 2026-08-25
**Mandate:** Find a second mechanism that can run beside the late-certainty trader, on Kalshi, with at most $2,000 shared capital and roughly $250/week as the target.
**Safety:** Research only. No orders were placed. Any pilot that can place orders requires Chris's explicit approval.

**Research scope:** This review did not bulk-pull Kalshi. It used the current official fee schedule, API/FIX documentation, rulebook, contract terms, regulatory filings, and help-center program terms. Five isolated public-API reads were made (four series lookups and one unpaginated active-incentive lookup); there was no pagination, market loop, historical download, authenticated trading call, or order action. The local 118-series census was inspected without refreshing it.

## Executive decision

The best genuinely different mechanism is **liquidity-incentive market making**: maintain bona fide, two-sided, post-only quotes in selected reward markets and earn a share of Kalshi's posted liquidity pool, with spread capture as secondary revenue. This is not a forecast/mispricing strategy and not late-certainty trading; the exchange pays for continuously supplying useful resting liquidity even when an order never fills.

The $250/week target is **plausible but not established**. The governing equation is simple: weekly reward revenue is the sum of `(our qualifying score share × reward pool)`. For illustration, a 36% average share of one $100/day pool is $252/week before fills, fees, and adverse selection. Whether that share can be obtained with $2,000 depends on current competition, quote distance, target size, and inventory losses. Those inputs must be paper-recorded before approval of any live pilot.

Two related conclusions:

1. A fee-free, maker-only complete-set ladder can be a true arbitrage **after every leg has filled**, but Kalshi offers no atomic cross-market order. Before the last fill it is a directional inventory trade. With 20–200 strikes, this is an alert/opportunistic module, not the primary strategy.
2. Settlement-source latency is real and some official/canonical feeds are observable before Kalshi determines a market, but trading around a nearly known source value is a variant of the existing late-certainty mechanism. It is therefore excluded as Strategy 2.

## 1. Fees: current rules and edge cases

### 1.1 Current formulas

The [current fee schedule (effective July 7, 2026)](https://kalshi.com/docs/kalshi-fee-schedule.pdf) gives event-contract fees as:

```text
taker fee = round_up(M_t × 0.07   × C × P × (1-P))
maker fee = round_up(M_m × 0.0175 × C × P × (1-P))
```

`P` is the contract price in dollars, `C` is contract count, and `M` is the applicable multiplier. The default is `M_t = 1` and `M_m = 0`. Thus most maker fills are free, but **maker fills are not universally free**. Both curves peak at 50c. Before rounding, the per-contract maxima are 1.75c for a default taker fill and 0.4375c for a maker fill with multiplier 1.

The schedule's non-standard table currently contains three economically distinct profiles:

- **Maker 1 / taker 1:** many named sports, macro, awards, and championship series, including `KXCPI`, `KXFED`, `KXGDP`, `KXATPMATCH`, `KXMLBGAME`, and `KXNFLGAME`. These charge the quadratic maker fee.
- **Maker 0 / taker 0:** named zero-fee series including `KXBTCY`, `KXETHY`, `KXCITRINI`, `KXDOED`, `KXELECTIRAN`, `KXGAMBLINGREPEAL`, `KXGREENLAND`, `KXIRANDEMOCRACY`, `KXLAYOFFSYINFO`, and `KXPAHLAVIHEAD`.
- **Maker 2 / taker 1:** `KXMVE` combos, excluding uncorrelated NFL combos. The maker coefficient is therefore `0.035`, twice the ordinary maker-fee coefficient.

Two of the isolated, unauthenticated fact checks against the official public API confirm the metadata representation: [`KXCPI`](https://api.elections.kalshi.com/trade-api/v2/series/KXCPI) reports `fee_type=quadratic_with_maker_fees, fee_multiplier=1`, while [`KXBTCY`](https://api.elections.kalshi.com/trade-api/v2/series/KXBTCY) reports `fee_type=quadratic, fee_multiplier=0`. The local 118-series searchable census remains all quadratic x1 for taker arithmetic, but it is not a census of all 13,448 series. A full API census was deliberately not attempted under tonight's rate-contention constraint. The official fee schedule nevertheless proves that exceptions exist outside the 118-series subset.

There is no evidence in the current prediction-market schedule of a volume tier or administrative fee cap. The only volume-tier table in that PDF applies to **perpetual futures**, not event contracts. The parabolic formula has a mathematical maximum at 50c but that is not a per-order cap. No economic maker rebate is listed. The [fee-rounding documentation](https://docs.kalshi.com/getting_started/fee_rounding) does describe a whole-cent **rounding rebate**, but it merely returns accumulated rounding overpayment; it is not maker compensation.

There is also no safe static fee table. Kalshi exposes [scheduled series fee changes](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes), and [event-level fee changes](https://docs.kalshi.com/api-reference/events/get-event-fee-changes) override the parent series. An execution gate must read the series fee type/multiplier, any event override, and the current fee schedule immediately before quoting. Historical February 2026 index discounts should not be assumed to survive the July 2026 schedule.

### 1.2 What exactly makes a fill “maker”

Kalshi's definition is based on the fill's role, not merely on using a limit order. The [fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf) and [limit-order guidance](https://help.kalshi.com/en/articles/13823811-limit-orders) say an order is maker when it does not match immediately, rests on the book, and is later executed by another order. An aggressive limit order that crosses existing liquidity is taker for those immediate fills. If its remainder rests and fills later, one order can contain both taker and maker fills; Kalshi's [fee accumulator](https://docs.kalshi.com/getting_started/fee_rounding) explicitly carries across both roles.

Use `post_only=true` to prevent an accidental cross. The [Kalshi Pro order-panel documentation](https://kalshi.com/pro/help/the-order-panel) says post-only keeps the order from crossing and filling immediately, and FIX reports `POST_ONLY_CROSS` when it would do so. Do not infer role after the fact: REST and WebSocket fills expose `is_taker`, and the [order record](https://docs.kalshi.com/api-reference/orders/get-order) separately totals maker/taker fill cost and fees.

## 2. Executable order mechanics

The current [V2 create-order schema](https://docs.kalshi.com/api-reference/orders/create-order-v2) and [FIX order-entry specification](https://docs.kalshi.com/fix/order-entry) establish the following:

| Mechanic | Supported | Important behavior |
|---|---:|---|
| Limit order | Yes | The documented event-contract order type; FIX `OrdType` supports limit only. |
| GTC | Yes | True GTC without an expiration. REST uses GTC plus `expiration_time` for a timed order. |
| IOC | Yes | Fills available quantity immediately and cancels the rest. |
| FOK | Yes | Cancels unless the whole single-market order can fill. |
| Day / GTD | FIX; UI equivalents | FIX supports Day and GTD; REST represents timed expiry as GTC plus a Unix expiration. |
| Post-only | Yes | A crossing post-only order is canceled rather than taking. |
| Reduce-only | Yes | Caps placement by the current position. |
| Cancel on pause | Yes | Cancels a resting order if exchange trading is paused. |
| Amend / decrease | Yes | Increasing size at the same price forfeits queue position; price can be changed. |
| Order groups | Yes | Server-side kill switch: cancel the group's resting orders after a rolling 15-second fill limit is exceeded. |
| Self-trade prevention | Yes | `taker_at_cross` cancels the incoming order; `maker` cancels the resting self order and continues matching. Partial fills before the self-cross remain executed. |
| Iceberg / hidden quantity | No documented support | Neither the REST nor FIX entry schema has display-size or hidden-quantity fields. |
| Native multi-market atomic order | No | Batch creation returns a result per child order; FOK applies to one order/market, not a ladder. |

Order groups are especially useful for a quoting strategy. Kalshi's [order-group documentation](https://docs.kalshi.com/getting_started/order_groups) tracks fills in a rolling 15-second window and, when the limit is breached, cancels all remaining orders in the group and blocks new ones until reset. This is a fill-rate circuit breaker, not a dollar-loss or inventory limit, so the client still needs position and collateral gates.

The [batch-create response](https://docs.kalshi.com/api-reference/orders/batch-create-orders-v2) reports independent fill and remaining counts for every child. It is transport batching, not an all-or-none transaction. This is the key mechanical obstacle to complete-set execution.

## 3. Settlement mechanics and source observability

### 3.1 Lifecycle, early close, and disputes

The [market-lifecycle documentation](https://docs.kalshi.com/getting_started/market_lifecycle) distinguishes `closed`, `determined`, `disputed`, `amended`, and `finalized`. After determination, `settlement_timer_seconds` runs; the result may be disputed, an amended determination restarts the timer, and only finalization pays positions. Close time can be moved earlier when `can_close_early` is true. If a paused market is reactivated, all resting orders are canceled. After close, order operations—including cancellation—are rejected, while the exchange cancels resting orders shortly afterward.

Binary winners normally receive $1 and settlement fees are zero under the [settlement documentation](https://docs.kalshi.com/getting_started/market_settlement) and [July fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf). Scalar settlement can pay between $0 and $1, and sub-cent payout rounding may appear as a settlement fee/adjustment.

### 3.2 “Void,” ties, and indeterminate outcomes are contract-specific

There is no universal “void means refund entry price” rule. Current [Kalshi Rulebook v1.24, Rule 6.3](https://kalshi-public-docs.s3.amazonaws.com/regulatory/rulebook/Kalshi%20Rulebook%20v1.24.pdf) provides that when the payout criterion cannot be determined, Kalshi may use the last traded price; if that is unavailable or not fair, the Outcome Review Committee determines a binding fair allocation. A trade rejected by the clearing house is void *ab initio*, which is different from a listed market later becoming indeterminate.

Contract terms can be more specific. For example, the PDF currently returned by Kalshi's [`TENNISMATCH` contract-terms URL](https://assets.kalshi.com/contract_terms/TENNISMATCH.pdf)—whose body is titled “Achievements”—provides that an outright cancellation can settle eligible participants at the last traded/fair price, or an equal allocation if fair value cannot be determined. Multiple official winners divide the $1 payout: two winners produce 50c Yes and 50c No payouts in each affected market. That URL/title mismatch is itself a reason to archive and validate the exact terms attached to each live market. The [sports/combo FAQ](https://help.kalshi.com/en/articles/13823821-market-faqs) likewise says DNP provisions may settle at fair value and a combo pays the product of all scalar component values. Therefore, ladder “guarantees” must exclude any event whose rules permit scalar/fair-value settlement unless that payoff is modeled exactly.

Revisions are also rule-specific. The [CPI terms](https://assets.kalshi.com/contract_terms/CPI.pdf), for example, say revisions after expiration are ignored. Every strategy must archive the full contract terms, not only the UI rules summary, before first quote.

### 3.3 Can the source be seen before Kalshi resolves?

Yes, often—but observability is not the same as a tradable post-source window.

- Kalshi publishes a real-time authenticated [CF Benchmarks WebSocket feed](https://docs.kalshi.com/websockets/cfbenchmarks-value) with raw upstream frames and the final-minute 15-minute average; it also offers an entitled [CF Benchmarks REST passthrough](https://docs.kalshi.com/cfbenchmarks/rest-passthrough).
- It publishes real-time [Pyth value updates](https://docs.kalshi.com/websockets/pyth-value) and a public [canonical minute-resolution weather index](https://docs.kalshi.com/api-reference/live-data/get-weather-index) behind hourly temperature markets.
- The generic [event live-data endpoint](https://docs.kalshi.com/api-reference/live-data/get-event-live-data) serves crypto, commodity, and weather time series.
- Public source agencies can publish official results before Kalshi's manual determination. However, contracts often stop trading just before publication: the [CPI terms](https://assets.kalshi.com/contract_terms/CPI.pdf) close at 8:29 AM ET for the scheduled 8:30 AM release. Hourly weather settlement is normally processed 25–35 minutes after close, according to Kalshi's [weather guide](https://help.kalshi.com/en/articles/13823837-weather-markets), so the later official value is observable but no longer executable.

If a market remains open after the outcome appears known, Kalshi warns that this does not itself signal whether criteria were met; the specified official source and rules control. Exploiting a genuine open interval after an authoritative observation is late-certainty trading and belongs to the existing strategy, not this one.

## 4. Incentive programs: compliant and non-compliant uses

Kalshi now has three mechanisms that should not be conflated.

### 4.1 Retail liquidity incentive: the recommended mechanism

The current [Liquidity Incentive Program](https://help.kalshi.com/en/articles/13823851-liquidity-incentive-program) is available to most direct U.S. members but excludes affiliates, members with a Market Maker Agreement, IBs/FCMs and their customers, and international users. Kalshi samples the book once per second at a random instant. Qualifying score depends on resting size and distance from the reference price; both sides must have sufficient qualifying depth for the snapshot to count. Orders need not execute.

The [CFTC-filed terms](https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Copy%20of%20Liquidity%20Incentive%20Program%20September%208%202025%20Backup%20Filing%20%281%29.pdf) make the intended behavior explicit: improve central-limit-order-book liquidity with resting orders. They cap target size between 100 and 20,000 contracts and rewards between $10 and $1,000 per calendar day encompassed by a period. Kalshi can revoke status for abusive participation. The current help page further states that snapshots lacking two-sided target depth are excluded and the headline reward is scaled down by the fraction of non-excluded snapshots.

A compliant implementation therefore:

- posts real, executable, post-only quotes on both sides;
- intends to accept any resulting fills and manages the resulting inventory;
- never coordinates fills, crosses with itself, or trades merely to manufacture volume;
- cancels stale quotes because fair value or source state changed, not to spoof snapshots;
- uses the published scoring rule, just as any market maker optimizes spread, size, and uptime.

This is materially safer than “reward farming” through executions because the program explicitly pays for useful resting liquidity even without fills.

### 4.2 Volume incentive: do not farm

The current [Volume Incentive Program](https://help.kalshi.com/en/articles/13823850-what-is-the-kalshi-volume-incentive-program) pays proportional to eligible central-book volume but caps earnings at $0.005 per contract. Kalshi expressly monitors abusive behavior and fake trading. The [filed terms](https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Volume%20Incentive%20Program%20-%20August%2030,%202025.pdf) apply Chapter 5 prohibitions against fraudulent, non-competitive, unfair, or abusive practices.

Compliant use is passive: collect the reward on trades that the strategy would make anyway. It is not a standalone edge because a 0.5c/contract maximum is easily overwhelmed by taker fees, spread, and adverse selection. Self-crossing, coordinated offsetting, or uneconomic churn to create eligible volume is out.

### 4.3 Designated Liquidity Provider program: possible later, not tonight

The separate [Liquidity Provider Program](https://help.kalshi.com/en/articles/15410219-liquidity-provider-program) requires a Market Maker Agreement and selection through periodic auctions. Requirements can include maximum spread, minimum size, uptime, and market coverage; [filed terms](https://kalshi-public-docs.s3.amazonaws.com/regulatory/notices/Kalshi%20-%20Liquidity%20Provider%20Program%20-%20Cover%20Letter%20and%20Appx%20A%20%28Modified%2C%20Clean%29.pdf) cap a series reward at $50,000/week. This could create strong economics, but it may be incompatible with $2,000 capital and it would make Chris ineligible for the retail liquidity program. Do not email, bid, or sign an agreement without Chris's explicit approval and a side-by-side economic review.

## 5. Multi-strike ladders and complete sets

For an event with `N` mutually exclusive **and exhaustive** binary buckets:

```text
buy one YES in every bucket:
  guaranteed terminal payout = $1
  maker-complete-set edge     = $1 - sum(executed YES prices) - maker fees

buy one NO in every bucket:
  guaranteed terminal payout = $(N-1)
  maker-complete-set edge     = (N-1) - sum(executed NO prices) - maker fees
```

With default fee-free maker fills, the taker-fee problem disappears. If all YES legs execute as maker for 98c total, the completed set earns 2c. On maker-fee series, add the maker parabola for every leg; on event overrides, use the override. Scalar/fair-value contingencies can change the terminal payoff and must be excluded or modeled.

The fatal word is **completed**. Kalshi has no atomic cross-market FOK. Batch placement is independently processed, a post-only order may never fill, and filling only the stale/wrong legs creates adverse selection. A 50-leg set with 49 fills is not an arbitrage. Consequently:

- scan and stage only events whose full rules prove exclusivity and exhaustiveness;
- require every executed fill to report `is_taker=false`;
- keep a live completed-set ledger using executed prices and actual fees, not displayed quotes;
- cap incomplete-set worst-case loss and time-to-expiry;
- treat collateral return as capital efficiency, not a guarantee.

Kalshi's [collateral-return documentation](https://help.kalshi.com/en/articles/13823816-collateral-return) confirms netting for mutually exclusive and directional groups. It can reduce cash tied up in hedged positions, but enabling is locked for an event when the first order is placed—even an unfilled/canceled order—and returned collateral can restrict later selling. That setting needs an explicit pre-trade decision.

For nested directional strikes, use set inclusion rather than an exhaustive sum. If `B` is a stricter event contained in `A`, then `YES(A) + NO(B)` pays at least $1 in every state. It is an arbitrage only when the two fully executed costs plus fees are below $1. Again, two legs are non-atomic.

**Verdict:** maker complete sets survive algebraically; they do not survive as automatically riskless execution. Build an alert and inventory-accounting module alongside the incentive strategy, not a $250/week forecast.

## 6. Proposed Strategy 2 and approval gates

### Strategy: reward-aware, two-sided passive quoting

Select retail liquidity-incentive markets where the displayed reward per dollar of required qualifying depth is high and competition is low. Maintain post-only Yes and No bids around a conservative reference value, harvest qualifying snapshot share, and allow only inventory that can be bounded or hedged within the same event ladder. Revenue is:

```text
liquidity reward
+ maker spread / completed-set edge
- maker fees (where applicable)
- adverse-selection and inventory loss
- operational failures
```

This mechanism is distinct from the late-certainty trader because its primary edge is an exchange-funded reward for uptime, size, and quote placement—not superior prediction near resolution.

### Required paper phase (no real orders)

For at least 14 calendar days, once per second or from a compliant book recorder, calculate without submitting orders:

1. active program, reward, target size, discount factor, start/end, and competition;
2. hypothetical qualifying score on both sides under the published formula;
3. capital reserved at each proposed quote and total shared-account usage;
4. fills against historical public trades/order-book changes, with queue position modeled conservatively;
5. mark-to-settlement inventory P&L, fees using live series/event metadata, and reward share;
6. daily net P&L, worst drawdown, fill concentration, stale-quote loss, and uptime.

Advance only if the lower-confidence weekly net exceeds $250 while peak capital stays below $2,000 and no single event can lose more than a Chris-approved limit. A live pilot would additionally require:

- Chris's explicit approval;
- a dedicated subaccount if available;
- `post_only=true`, self-trade prevention, cancel-on-pause, expirations, and order-group kill switches;
- hard account-wide capital reservation that includes the live late-certainty trader;
- immediate stop on fee/rule/settlement-source change or any unexplained scalar outcome;
- no volume farming and no order designed to trade with another Chris-controlled order.

## Bottom line

Proceed to a **paper-only liquidity-incentive market-making study**. It is the only reviewed structural mechanism with a credible route to recurring $250/week at this capital level. Keep maker complete-set detection as a secondary opportunistic module. Reject volume farming on both economics and compliance grounds, and classify source-observation latency under the existing late-certainty strategy.
