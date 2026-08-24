# Live specification reconstructed from code

Snapshot: repository `HEAD` on 2026-08-24. The specification below starts at the active GitHub Actions entrypoint and follows only invoked code. `CLAUDE.md` is used only in the final disagreement table.

## Reproduction commands

```bash
git rev-parse HEAD
nl -ba .github/workflows/late_certainty.yml
rg -n '^[A-Z][A-Z0-9_]*(?::[^=]+)?\s*=|def (_band_min|_in_band|compute_daily_loss_limit|min_book_depth|contracts_for_risk)|3600|fresh_ask <= 91|ext_priors\[2\] < 80|prior_asks\[1\]|Decimal\("0.9"\)|Decimal\("0.01"\)|range\(pages\)|limit":' late_certainty_trader.py
nl -ba late_certainty_trader.py | sed -n '1080,1598p'
nl -ba kalshi_auth.py | sed -n '24,156p'
python3 - <<'PY'
import late_certainty_trader as t
for side in ('yes', 'no'):
    print(side, t._band_min(side), t.MAX_ASK_CENTS)
for limit in (88, 89, 90, 91, 92, 93):
    print(limit, t.contracts_for_risk(t.FLAT_BET_DOLLARS, limit),
          t.min_book_depth(t.FLAT_BET_DOLLARS, limit))
print('daily loss', t.compute_daily_loss_limit(t.FLAT_BET_DOLLARS))
PY
rg -n 'MIN_ASK|LOW_BAND|MAX_ASK|PRIOR_|MAX_CONCURRENT|FLAT_BET|MIN_BOOK_DEPTH' scripts/backtest.py test_order_safety.py
```

The GitHub workflow and production-log claims are reproducible with:

```bash
gh run list --workflow late_certainty.yml --limit 20 \
  --json databaseId,event,status,conclusion,headSha,createdAt,updatedAt
gh run view 32742473985 --log | rg 'KXSOL15M|outside|crashed'
gh run view 32744023345 --log | rg 'KXETH15M|outside|crashed'
gh run view 32739329589 --log | rg 'KXDOGE15M|outside|crashed|TRADE'
```

## Invoked program

The active workflow is `.github/workflows/late_certainty.yml`. It has two cron triggers, `*/5 * * * *` and `2-59/5 * * * *`, plus `workflow_dispatch`. It serializes runs in concurrency group `certainty-trader` with `cancel-in-progress: false`, has a twenty-minute job timeout, and checks out the triggering revision on Ubuntu. It installs Python 3.11, `requests`, and `cryptography`; restores `certainty_state.json`; compiles the live modules; runs `python -m unittest -v test_order_safety.py`; then invokes:

```text
python late_certainty_trader.py --daemon --duration 900 --interval 15
```

A failing compile, safety test, or trader command prevents the remaining workflow steps. The state file is then uploaded as `live-state` with one-day retention and saved in the Actions cache. After a five-second delay, the workflow uses `GH_DISPATCH_TOKEN` to dispatch its next run. The shell receives `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY`, `COPY_EMAIL_FROM`, `COPY_EMAIL_TO`, and `COPY_EMAIL_PASSWORD` from Actions secrets.

## Effective parameters

### Trading strategy

| parameter | code value | effective meaning |
|---|---|---|
| strategy version | `v5.17` | A version mismatch preserves open positions but resets strategy stats and recent results. |
| live series | `KXBTC15M`, `KXETH15M`, `KXSOL15M`, `KXDOGE15M`, `KXBNB15M`, `KXXRP15M` | These six series alone enter candidate discovery. |
| side selection | `YES_ONLY=False`; YES checked first | Both outcomes may trade. YES wins the prefilter if both somehow qualify. |
| declared side band | YES `88–93¢`; NO `90–93¢` | `_band_min()` controls the listing prefilter and first fresh-quote check. |
| **effective executable band** | **YES `90–93¢`; NO `90–93¢`** | The downstream book last-look and retry checks use `MIN_ASK_CENTS=90` for both sides. This is a defect, detailed below. |
| listing tolerance | `3¢` | Listing prefilter admits YES `85–96¢` and NO `87–96¢`; the individual-market quote must then pass the declared band. |
| primary prior gate | two preceding one-minute same-side asks, each at least `75¢` | Missing/unparseable candles fail closed. Candle request ends twenty seconds before now. |
| low-ask gate | if fresh ask is at most `91¢`, third prior must be at least `80¢` | Applies to both sides and therefore to the intended YES extension. |
| SOL quarantine | skip `KXSOL15M` when the second returned prior, integer-truncated, is `75–79¢` | Hardcoded live veto. |
| time window | `150–600` seconds to close, inclusive | No blackout hours: `BLACKOUT_HOURS=set()`. |
| candidate order | descending seconds-to-close, then first eight hex digits of MD5(`cluster|series`) | Allocates scarce slots deterministically without fixed series priority. |
| stake | flat `$25` principal per position attempt sequence | Fees are additional. Balance does not change size. |
| order price | `min(93¢, book_or_fresh_ask + 2¢)` | Whole contracts are `floor(25 / limit_dollars)`, at least one. At a `93¢` limit this is twenty-six contracts. |
| first-attempt depth | `max(ceil(contracts × 1.5), 25)` | At the live stake and maximum limit this is thirty-nine contracts at offers no greater than `93¢`. |
| retry depth | `60` contracts | A stale legacy constant is still used for top-ups, rather than the dynamic first-attempt requirement. |
| maximum exposure count | two | Count is the union of open state and live positions plus every separate resting order. |

### Risk, state, and daemon controls

| parameter | value / behavior |
|---|---|
| cash stop | halt at balance at or below `$650` |
| realized-loss window | trailing `86,400` seconds; legacy rows without a settlement timestamp fall back to the current ET date |
| loss limit | `max(300, bet × 4)`, therefore `$300` at the live stake |
| consecutive-loss cooldown | after at least nine losses, halt until the last loss is at least `3,600` seconds old |
| edge breaker | with at least fifty current-version outcomes, halt if the last fifty have win rate below `0.84` |
| edge recovery | after `7,200` seconds and fewer than three consecutive losses, clear the breaker and its evidence window |
| execution ambiguity | persistent `execution_halt_reason`; manual reconciliation required |
| state retention | at most five hundred settled positions, plus all unsettled positions; recent results retained to twice the edge window |
| daemon failure policy | raise on ten consecutive failed cycles or a final failed-cycle share strictly above `0.34` |
| poll job | nine hundred-second duration and fifteen-second interval from workflow, overriding CLI defaults |
| files | state `certainty_state.json`; append log `certainty.log`; state save uses a same-directory temporary file followed by atomic replace |

### Execution and API controls

| parameter | value / behavior |
|---|---|
| API root | `https://api.elections.kalshi.com/trade-api/v2` |
| signing | RSA-PSS SHA-256 over timestamp, uppercase method, `/trade-api/v2`, and path |
| HTTP timeout | twenty seconds for signed Requests calls |
| market listing page | ten open markets per live series; discovery fans out with six worker threads |
| positions/orders pagination | two hundred rows per page until cursor exhaustion |
| ambiguous POST search | up to three order pages, two hundred rows per page, matched by client order ID |
| fills query | up to one thousand rows, filtered by ticker, outcome side, and order ID |
| order type | V2 limit order; YES is a YES-side `bid`; buying NO is a YES-side `ask` at `1 - no_price` |
| order lifetime | GTC, server expiration four seconds after submission, `cancel_order_on_pause=true` |
| self-trade prevention | `taker_at_cross` |
| local resting interval | three seconds, then DELETE cancellation |
| accepted cancellation responses | HTTP `200`, `204`, or `404`; other responses rely on server expiry and reconciliation |
| terminal reconciliation | poll authoritative order for up to eight seconds at half-second intervals; fills endpoint is fallback when terminal order cost has not propagated |
| top-ups | at most three total attempts; stop when unused principal is below `$5` |
| principal tripwire | persistent halt if cumulative principal exceeds target by more than `$0.01` |
| partial-fill log threshold | filled contracts below ninety percent of attempted contracts |
| crash classification | any average fill below hardcoded `90¢` is marked outside band; more than `3¢` below sends email and ends top-ups |
| credentials | workflow decodes `KALSHI_PRIVATE_KEY` to `/tmp/kalshi_certainty_key.pem` mode `0600` unless a key path already exists |
| alert transport | Gmail SMTP at port `587`, STARTTLS; absent email variables silently disable alerts |

Kalshi's current V2 documentation agrees with the code's create/cancel paths, GTC expiration field, side mapping, and authoritative order lookup: [Create Order V2](https://docs.kalshi.com/api-reference/orders/create-order-v2), [Cancel Order V2](https://docs.kalshi.com/api-reference/orders/cancel-order-v2), and [Get Order](https://docs.kalshi.com/api-reference/orders/get-order).

### Live shadow telemetry (does not gate orders)

The same process scans excluded series `KXHYPE15M`, `KXBTCD`, `KXETHD`, `KXWTIH`, `KXWTI15M`, `KXGOLD15M`, and `KXSILVER15M`. It also logs survivor windows (`94–96¢` now, `92–93¢` at `480–600` seconds), three-minute adverse spot momentum normalized over sixty one-minute returns, gate inputs over ask union `88–99¢` and time union `150–900` seconds with a per-process fetch ceiling of one hundred eighty, ET-hour eight/thirteen observations, and live-series `600–700` second candidates. These paths log only. The longshot functions/constants are dead code: neither `open_markets_longshot` nor `try_longshot_trade` has a caller, so they are excluded from the effective strategy.

Reproduce dead-code call counts and shadow configuration with:

```bash
rg -n 'open_markets_longshot|try_longshot_trade|SHADOW_SERIES|SURVIVOR_|MOMENTUM_|GATELOG_|600 < secs' late_certainty_trader.py
```

## Entry gates in exact runtime order

1. **Workflow gate:** checkout, dependency install, byte-compile, and the entire safety test suite must succeed before the trader starts.
2. **State gate:** existing state must parse. An unreadable state file raises and fails the cycle closed. A strategy-version mismatch resets stats/recent results but carries open positions forward.
3. **Balance gate:** inability to fetch balance skips the cycle.
4. **Settlement pass:** known positions are checked for settled/finalized YES/NO results before risk gates run.
5. **Persistent execution halt:** any prior unresolved order state halts first.
6. **Cash floor:** balance at or below `$650` halts.
7. **Trailing loss:** realized P&L at or below negative `$300` halts at the live stake.
8. **Loss streak:** nine or more losses halt for one hour from the last recorded loss.
9. **Edge breaker:** once fifty current-version results exist, win rate below eighty-four percent halts, subject to the two-hour/fewer-than-three-loss recovery rule.
10. **External exposure proof:** all unsettled positions and resting orders are paged. If either query is unknown, the cycle skips.
11. **Discovery:** each live series requests open markets; candidates outside the inclusive `150–600` second window or a configured blackout are discarded. The blackout set is empty.
12. **Allocation order:** all candidates are sorted by most time remaining, then deterministic cluster/series hash.
13. **Duplicate ticker:** skip if the ticker exists in state, a live unsettled position, or any resting order.
14. **Listing prefilter and side:** YES is tested against `85–96¢`; otherwise NO is tested against `87–96¢`. A market with neither is ignored.
15. **Heat:** combined state/live/resting exposure must be below two.
16. **Fresh exact quote:** the individual-market side ask must exist and pass YES `88–93¢` or NO `90–93¢`.
17. **Primary priors:** both same-side preceding one-minute asks must be available and at least `75¢`.
18. **Low-ask prior:** when fresh ask is no greater than `91¢`, the third prior must exist and be at least `80¢`.
19. **SOL quarantine:** SOL is skipped if the integer-truncated second prior is `75–79¢`.
20. **Book availability and depth:** unreadable best/depth pair fails closed. Known depth must satisfy the dynamic requirement; at the live maximum limit that is thirty-nine contracts.
21. **Book last look:** best offer must be `90–93¢` for either side. **This wrongly overrides the YES `88–89¢` extension.**
22. **Sizing and credential:** compute capped limit and whole-contract count; dry run returns; absent key ID returns without ordering.
23. **Each attempt:** remaining principal must be at least `$5`. For top-ups, refetch quote and priors, rerun the low-ask condition, reread book, require hardcoded `90–93¢`, and—if known—require sixty contracts of depth.
24. **Submit and identify:** POST the short-lived GTC order. A non-confirmed response is resolved by client ID. Confirmed absence ends attempts; unknown status or accepted response without order ID persists an execution halt, alerts, and raises.
25. **Cancel and reconcile:** wait three seconds, cancel, then prove a terminal order and exact exposure. Failure persists a halt, alerts, and raises.
26. **Budget:** cumulative principal may not exceed `$25.01`; violation persists a halt and raises.
27. **Fill-zone/top-up decision:** a fill below `90¢` is classified outside band. More than three cents low alerts; any below-`90¢` fill stops further attempts. Otherwise another bounded top-up may occur.
28. **Persist:** if total fill is positive, record execution and position, increment current-version trade count, and atomically save state. Settlement remains hold-to-resolution.

## Flagged live bug: v5.17 extension is blocked downstream

The initial side-aware checks correctly accept an `88–89¢` YES ask (`_band_min`, lines 164–170 and fresh quote, lines 1209–1217). The mandatory last book look then checks `MIN_ASK_CENTS <= best_offer`, not `_band_min(side)` (lines 1320–1323). The same hardcoded lower bound appears in the top-up quote and book checks (lines 1367–1369 and 1384–1386) and in fill classification (lines 1511–1514).

Because an entirely unreadable book fails closed and an empty book has insufficient depth, every executable first attempt must have a known best offer; an `88–89¢` YES best offer is therefore rejected. The effective live band remains symmetric `90–93¢`. This is an active missed-opportunity defect, not evidence that the process submits negative-EV orders, so the read-only audit continued.

Production logs independently reproduce the defect:

- run `32742473985`: SOL fresh YES `88¢` is logged as having crashed below `90¢`;
- run `32744023345`: ETH fresh YES `89¢` is logged as having crashed below `90¢`;
- run `32739329589`: DOGE fresh YES `91.6¢` reaches the book check, where best offer `89¢` is rejected as outside `[90,93]`.

The safety suite only pins `_band_min()`/`_in_band()` asymmetry. It does not execute the complete entry path. The canonical `scripts/backtest.py` reads `MIN_ASK_CENTS` and `MAX_ASK_CENTS` but not `LOW_BAND_MIN_CENTS`; its `qualifies()` applies one symmetric band to both sides. Thus neither the tests nor the canonical harness would catch or reproduce this defect.

## Code/document disagreements

Reproduce this table with:

```bash
rg -n 'v5\.1[267]|90-93|88-89|\$25|\$50|\$75|KXWTI15M|MIN_BOOK_DEPTH|duration|interval|240|900|ET hour 13|consecutive|trailing' \
  CLAUDE.md .github/workflows/late_certainty.yml late_certainty_trader.py scripts/backtest.py
```

| subject | code / effective truth | conflicting documentation |
|---|---|---|
| version | code is `v5.17` | workflow banner says `v5.16`; trader module docstring says `v5.12`; CLAUDE has both “Current Strategy — v5.16” and v5.17 material |
| entry band | declared YES `88–93¢`, NO `90–93¢`; effective downstream band `90–93¢` both | workflow banner and substantial CLAUDE prose say `90–93¢` both sides; the v5.17 CLAUDE section says YES extends to `88–89¢` |
| stake | flat `$25` | workflow banner says `$75`; trader docstring says `$75`; CLAUDE contains current `$25` plus stale `$50` prose |
| daily loss | `$300` at current sizing (`max(300, bet×4)`) | trader docstring says eight times stake and `$600`; historical CLAUDE passages describe different active levels |
| loss streak | nine losses, one-hour cooldown | trader docstring says five losses |
| blackout | empty set | trader docstring says ET hour thirteen excluded |
| series | six crypto series; WTI shadow-only | workflow banner says WTI is live |
| depth | first attempt dynamic, thirty-nine at the live maximum limit; retry still sixty | workflow/CLAUDE describe a flat sixty-contract minimum |
| job cadence | nine hundred-second daemon at fifteen-second interval; follow-up dispatch sleeps five seconds | stale docs mention a two-hundred-forty-second job and roughly thirty-second handoff |

## UNKNOWN

- The manifest and workflow expose only secret names; actual credential/email values were neither requested nor read.
- Runtime account balance, current open positions, and resting orders are deliberately not frozen into this specification because they are state, not parameters.
- Whether Kalshi's eventually consistent order/fill fields always satisfy the code's eight-second reconciliation assumption requires live API latency evidence not present in code.
