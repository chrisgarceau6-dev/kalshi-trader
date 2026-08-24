# LIVE_SPEC — what the trader actually does

Derived 2026-08-24 from `late_certainty_trader.py` @ `ee5cadd4` and from live
behaviour (settlements API, fills API, the `live-state` run artifact). CLAUDE.md,
the module docstring and the workflow header comment were treated as untrusted and
are **not** sources for anything below; where they disagree with the code that is
recorded in §7.

Every claim carries a command. Run from `~/pm`.

---

## 1. Deployment

| Fact | Value | Command |
|---|---|---|
| Entrypoint | `python late_certainty_trader.py --daemon --duration 900 --interval 15` | `grep 'run: python late_certainty' .github/workflows/late_certainty.yml` |
| Scans per job | 60 (`900/15`) | as above |
| Schedule | two crons (`*/5`, `2-59/5`) **plus** a self-dispatch at job end | `grep -A3 'schedule:' .github/workflows/late_certainty.yml` |
| Concurrency | group `certainty-trader`, `cancel-in-progress: false` | `grep -A3 'concurrency:' .github/workflows/late_certainty.yml` |
| Job timeout | 20 min | `grep timeout-minutes .github/workflows/late_certainty.yml` |
| State | `actions/cache` key `certainty-state-<run_id>`, restore-prefix `certainty-state-`; also uploaded as artifact `live-state` | `grep -B2 -A6 'actions/cache' .github/workflows/late_certainty.yml` |
| Local `certainty_state.json` | always empty — never written by the live path | `python3 -c "import json;print(json.load(open('certainty_state.json')))"` |
| Pre-flight | `py_compile` + `unittest test_order_safety.py`; job fails if either fails | `grep -A6 'Order-safety checks' .github/workflows/late_certainty.yml` |
| Observed cadence | 15.2 min | `python3 scripts/kstat.py` |

`STRATEGY_VERSION = "v5.17"`. Changing it **wipes** `stats` and `recent_results`
(`load_state`, l.406-415) — the lifetime counter is version-scoped, not lifetime.
Confirmed live: the artifact reports `stats.trades == 1` against 500 retained
positions.

```
python3 -c "import json,sys;s=json.load(open(sys.argv[1]));print(s['strategy_version'],s['stats'],len(s['positions']))" <state.json>
```

## 2. Series

```
python3 -c "import ast;t=ast.parse(open('late_certainty_trader.py').read());\
print([ast.literal_eval(n.value) for n in t.body if isinstance(n,ast.Assign) and \
getattr(n.targets[0],'id','') in ('SERIES_LIST','SHADOW_SERIES')])"
```

* **Traded (6):** `KXBTC15M KXETH15M KXSOL15M KXDOGE15M KXBNB15M KXXRP15M`
* **Shadow-scanned, never ordered (7):** `KXHYPE15M KXBTCD KXETHD KXWTIH KXWTI15M KXGOLD15M KXSILVER15M`

Confirmed against live fills: no settlement with a close day ≥ 2026-08-20 sits
outside the six. `KXWTI15M` settlements exist up to 2026-08-19 (paused mid-day).

## 3. Sizing

| Constant | Value | Note |
|---|---|---|
| `FLAT_BET_DOLLARS` | **25** | `compute_bet_dollars()` ignores balance and returns this |
| `contracts_for_risk` | `max(1, int(bet / (limit/100)))` | sized off the **limit** price, not the fill price |
| `LIMIT_BUFFER` | 2 | `limit = min(93, book_best_offer + 2)` |
| `MAX_CONCURRENT_POSITIONS` | 2 | counts state-open ∪ live Kalshi positions **+** every resting order |

Live confirmation — the deployed bet is $25, not $50 and not $75:

```
python3 - <<'P'
import json,datetime as D
S=json.load(open('<settlements.json>'))
def cd(t):
    n=D.datetime.strptime(t.split("-")[1].upper(),"%y%b%d%H%M")
    return (n+D.timedelta(hours=4)).strftime("%Y-%m-%d")
R=[s for s in S if cd(s["ticker"])>="2026-08-23"]
c=[float(s["yes_total_cost_dollars"])+float(s["no_total_cost_dollars"]) for s in R]
print(f"n={len(c)} min={min(c):.2f} avg={sum(c)/len(c):.2f} max={max(c):.2f}")
P
```
→ `n=230 min=23.48 avg=24.00 max=24.96`. The $75→$50→$25 steps land on
2026-08-19 and 2026-08-22 (per-day table in `METRICS.md` §4).

## 4. The entry path, in execution order

`run_once()` → discovery → slot sort → `try_trade()` per candidate.

### 4.0 Pre-entry, once per cycle
| # | Gate | Effect on failure |
|---|---|---|
| 0.1 | `fetch_balance()` returns None | skip the whole cycle |
| 0.2 | `check_outcomes()` settles anything resolved | — |
| 0.3 | `state["execution_halt_reason"]` set | **halt** (persistent, fail-closed) |
| 0.4 | `balance <= STOP_BALANCE (650)` | halt |
| 0.5 | `daily_pnl(trailing 24h) <= -compute_daily_loss_limit()` | halt. Limit = `max(300, bet*4)` = **$300** at $25 |
| 0.6 | `consec_losses >= CONSEC_LOSS_LIMIT (9)` and last loss < 3600s ago | halt 60 min |
| 0.7 | rolling 50-trade WR < 0.84 | halt, auto-clears after 7200s if `consec_losses < 3` |
| 0.8 | `fetch_live_position_tickers()` or `fetch_resting_order_tickers()` returns None | skip the cycle |

### 4.1 Discovery
`open_markets_near_close()` over all 6 series **concurrently** (`ThreadPoolExecutor`),
`/markets?status=open&limit=10`, keeping `150 <= secs_left <= 600`.
`BLACKOUT_HOURS = set()` — **no ET hour is blocked.**

### 4.2 Slot allocation
`candidates.sort(key=(-secs_left, md5(f"{cluster}|{series}")))` — earliest-first,
ties broken by a stable hash of (close cluster, series). Matches
`scripts/backtest.py::simulate`'s `sorted(..., key=lambda r: -r[5])[:max_conc]`
by construction.

### 4.3 `try_trade` gates, in order

| # | Gate | Source | Fails |
|---|---|---|---|
| 1 | ticker already in `state["positions"]` | state | skip |
| 2 | ticker in live positions **or** resting orders | API | skip |
| 3 | **side select** from the `/markets` **listing** quote, widened by `LISTING_QUOTE_TOLERANCE = 3`: YES if `85 <= yes_ask <= 96`, else NO if `87 <= no_ask <= 96`. YES wins ties. | listing | skip if neither |
| 4 | heat check: `len(state_open ∪ live) + len(resting) >= 2` | state+API | skip |
| 5 | `fresh_ask = _fresh_ask_cents()` — refetch `/markets/{ticker}` | API | skip if None |
| 6 | `fresh_ask < _band_min(side)` → **88 for YES, 90 for NO** | API | skip |
| 7 | `fresh_ask > MAX_ASK_CENTS (93)` | API | skip |
| 8 | prior-candle gate: last `PRIOR_LOOKBACK = 2` completed 1-min candles both `>= PRIOR_MIN_CENTS = 75` | candlesticks | **fails closed** on API error |
| 9 | low-ask gate: if `fresh_ask <= 91`, the 3rd prior must be `>= 80` | candlesticks | skip |
| 10 | C1 quarantine: `series == KXSOL15M` and `75 <= prior_2 <= 79` | — | skip (**both sides**, see §7.4) |
| 11 | book read `_book_last_look(ticker, side)`; if `depth is None and best_offer is None` | orderbook | **fails closed** |
| 12 | depth gate: `depth < min_book_depth()` where the value is `max(ceil(int(25/0.93)*1.5), 25) = ` **39** | orderbook | skip |
| 13 | **last look**: `not (MIN_ASK_CENTS <= best_offer <= MAX_ASK_CENTS)` → **hardcoded `90`, not `_band_min(side)`** | orderbook | skip |
| 14 | order: `limit = min(93, best_offer + 2)`, `contracts = int(25/(limit/100))`, GTC with `expiration_time = now+4s`, `client_order_id` set | — | — |

Not gates but on the path: `[QUOTE-DRIFT]` logging (3), `[SHADOW:C5-HIGH-P1P3]`,
`[SHADOW:ET08]`, `[SHADOW:ET13]` — all log-only.

### 4.4 Order lifecycle
Up to `ORDER_MAX_ATTEMPTS = 3` bounded top-ups while
`remaining_budget >= ORDER_MIN_TOPUP_DOLLARS (5)`. Each attempt: place GTC →
`sleep(ORDER_FILL_WAIT_SECONDS = 3)` → cancel → `reconcile_terminal_order` (poll to
`ORDER_RECONCILE_SECONDS = 8`, else **raise + persistent halt + email**).
Ambiguous POST → `find_order_by_client_id` (3 pages); `None` → halt.
Cumulative principal over budget + $0.01 → halt.
`avg_fill < 90` sets `outside_safe_zone`; shortfall `> CRASH_FILL_TOLERANCE (3)` emails.

**Top-up gates are not the same as first-attempt gates** — see §7.3.

## 5. Every live constant

```
python3 -c "
import ast;t=ast.parse(open('late_certainty_trader.py').read())
for n in t.body:
  if isinstance(n,ast.Assign) and getattr(n.targets[0],'id','').isupper():
    try: print(f'{n.targets[0].id:<28}{ast.literal_eval(n.value)!r}')
    except Exception: print(f'{n.targets[0].id:<28}<expr>')"
```

| Constant | Value | Reaches an order? |
|---|---|---|
| `MIN_ASK_CENTS` | 90 | **yes** — gates 13, top-up, `outside_safe_zone` |
| `MAX_ASK_CENTS` | 93 | yes |
| `LOW_BAND_MIN_CENTS` | 88 | **no — inert, see §6** |
| `PRIOR_MIN_CENTS` | 75 | yes |
| `PRIOR_LOOKBACK` | 2 | yes |
| `YES_ONLY` | False | yes |
| `MIN_SECS_LEFT` / `MAX_SECS_LEFT` | 150 / 600 | yes |
| `BLACKOUT_HOURS` | `set()` | yes (no-op) |
| `LIMIT_BUFFER` | 2 | yes |
| `LISTING_QUOTE_TOLERANCE` | 3 | yes (pre-filter only) |
| `MIN_BOOK_DEPTH_MULTIPLE` / `_FLOOR` | 1.5 / 25 | yes → effective **39** |
| `MIN_BOOK_DEPTH` | 60 | **only** in the top-up gate and the gate-log line (§7.3, §7.5) |
| `FLAT_BET_DOLLARS` | 25 | yes |
| `MAX_CONCURRENT_POSITIONS` | 2 | yes |
| `STOP_BALANCE` | 650 | yes |
| `CONSEC_LOSS_LIMIT` | 9 | yes |
| `EDGE_DEGRADE_WINDOW` / `_THRESHOLD` / `_COOLDOWN` | 50 / 0.84 / 7200 | yes |
| `ORDER_TTL_SECONDS` … `ORDER_MIN_TOPUP_DOLLARS` | 4/8/3/3/5 | yes |
| `CRASH_FILL_TOLERANCE` | 3 | alerting only |
| `MAX_POSITIONS_STATE` | 500 | state pruning — **caps measurable history** |
| `LONGSHOT_*` (7 constants) | — | **no — dead code, see §7.2** |
| `SURVIVOR_*`, `MOMENTUM_*`, `GATELOG_*` | — | shadow logging only |
| `SERIES_BET_MULTIPLIER` | `{}` | no-op |

## 6. FINDING C1 — no order can ever be priced below 90c, so the 88-89c band is inert

`LOW_BAND_MIN_CENTS = 88` and `_band_min()` are consulted at gates 3 and 6 only.
Gate 13, the last look, is hardcoded to `MIN_ASK_CENTS = 90`:

```python
if best_offer is not None and not (MIN_ASK_CENTS <= best_offer <= MAX_ASK_CENTS):
    return                                        # l.1320
entry_ask = best_offer if best_offer is not None else fresh_ask   # l.1329
```

Two facts combine, and the second is the one that matters:

1. `best_offer` is `None` only when the book read fails — and gate 11 already fails
   closed on that. An empty book returns `(None, 0.0)`, which gate 12 rejects at
   `depth < 39`. **So `entry_ask` is always `best_offer`, never `fresh_ask`.**
2. Gate 13 forces `best_offer ∈ [90, 93]`.

Therefore **every order is priced from a book offer of 90c or better. The bot cannot
buy at 88c or 89c.** The trade the +$0.39/tr research measured — a fill *at* the
88-89c ask — is not reachable.

The subtlety worth stating, because it is where the two auditors are most likely to
diverge: a *stale 88-89c listing quote* does not always cause a skip. When the listing
reads 88-89c but the book is 90-93c, gates 6 and 13 both pass and the bot trades — at
90-93c. That is a normal in-band entry with a stale quote, not an 88-89c entry.
Measured on the live gate log (`data/gatelog/*.csv`, 615 rows with a book read):

| quoted ask | side | n | best ∈ [90,93] | best < 90 | best > 93 |
|---|---|---|---|---|---|
| 88-89c | yes | 70 | 34.3% | 55.7% | 10.0% |
| 88-89c | no | 72 | 36.1% | 48.6% | 15.3% |
| 90-93c | yes | 112 | 36.6% | 29.5% | 33.9% |
| 90-93c | no | 117 | 41.9% | 25.6% | 32.5% |

```
python3 -c "
import csv,glob
d=[r for c in sorted(glob.glob('data/gatelog/*.csv')) for r in csv.DictReader(open(c))
   if r.get('best') not in ('','None')]
g=[r for r in d if 88<=float(r['ask'])<=89.999 and r['side']=='yes']
print(sum(1 for r in g if 90<=float(r['best'])<=93), 'of', len(g))"
```

Had gate 13 used `_band_min(side)`, **52.9% of those 70 rows (37) would have passed**
at a genuine 88-89c book price. Today that number is zero.

**Proof — the real `try_trade` executed against a stubbed API, no file edits:**
`docs/audit/claude/probe_v517.py`

```
python3 docs/audit/claude/probe_v517.py
```
```
 ask  ordered?  first blocking log line
  87        no  SKIP ... yes ask crashed to 87.00c (< 88c)
  88        no  SKIP ... last look: best yes offer 88.00c is outside [90,93]
  89        no  SKIP ... last look: best yes offer 89.00c is outside [90,93]
  90       YES
  91       YES
  92       YES
  93       YES
  94        no  SKIP ... yes ask jumped to 94.00c (> 93c)
```

Corroborated live — no fill below 90c exists in the 500-position artifact:

```
python3 -c "
import json,sys,collections;s=json.load(open(sys.argv[1]))
print(sorted(collections.Counter(int(p['book_at_entry']) for p in s['positions'].values()).items()))" <state.json>
```
→ `[(90, 171), (91, 169), (92, 155), (93, 5)]`

**The effective live band is `[90, 93]` for both sides.** v5.17 changed nothing
except the version string — which reset the stats counter.

### Why it survived deployment: the four tests pin the constant, not the behaviour

CLAUDE.md §7 states v5.17 is "Pinned by 4 tests in
`test_order_safety.py::BandAsymmetryTests` so a future symmetric 'cleanup' cannot
silently reintroduce the -EV NO side." All four pass:

```
python3 -m unittest test_order_safety.BandAsymmetryTests -v
```
→ `Ran 4 tests ... OK`

Every one of them calls only `_band_min()` and `_in_band()` (l.704-725) — the two
helpers the deciding gate never consults. They assert that the *constant* is 88 and
that the *helper* answers "yes" at 88c. Neither statement is about whether an order
can be placed. The suite is green, the pre-registration is written, the change is
inert, and nothing in the pipeline could have said so.

`scripts/verify.py --check deadband` is the missing test: it runs the real `try_trade`
against a stubbed API and asserts the reachable band equals the declared band.

## 7. Where the written record is wrong

### 7.1 Stale prose (no live effect, but it is what a reader believes)

| Location | Says | Actually |
|---|---|---|
| module docstring l.2 | `v5.12` | `v5.17` |
| docstring l.11-13 | "Flat $75 principal-risk budget" | $25 |
| docstring l.6 | "ET hour 13 is excluded" | `BLACKOUT_HOURS = set()` |
| docstring l.5-8 | "Buy YES … YES-only" | `YES_ONLY = False`, NO is ~49% of volume |
| docstring l.17 | "trailing-24h loss limit = 8x bet ($600)" | `max(300, bet*4)` = $300 |
| docstring l.18 | "5 consecutive losses" | `CONSEC_LOSS_LIMIT = 9` |
| `MAX_CONCURRENT_POSITIONS` comment | "2×$75=$150" | 2×$25=$50 |
| workflow header | "$75", "plus KXWTI15M", "90-93c" | $25, WTI paused, band claimed as 88-93 |

### 7.2 `try_longshot_trade` / `open_markets_longshot` are unreachable
```
grep -n "try_longshot_trade\|open_markets_longshot" late_certainty_trader.py
```
→ definitions only, no call site. 7 constants and ~110 lines are dead. `check_outcomes`
still branches on `pos["strategy"] == "longshot"` for legacy state.

### 7.3 Top-up gates are looser than first-attempt gates
Gate 12 uses `min_book_depth()` = **39**. The top-up path (l.1388) uses the legacy
`MIN_BOOK_DEPTH` = **60**. Same order, two different depth requirements. The
top-up also re-checks `fresh_ask` against `MIN_ASK_CENTS` rather than `_band_min`.

### 7.4 C1 quarantine is wider live than in the harness
Trader (l.1240): `if series == "KXSOL15M" and 75 <= prior_asks[1] <= 79` — no side
test, so it quarantines **NO** entries too.
`scripts/backtest.py::qualifies`: `if cfg["c1"] and side == "yes" and ...` — YES only,
with the comment "matching the live trader". It does not match.

### 7.5 The gate log records a depth threshold the trader does not use
`shadow_gate_inputs` (l.1075) writes `min_depth={MIN_BOOK_DEPTH}` = 60 while the live
gate needs 39. Any replay that reconstructs "would depth have blocked this?" from the
gate log's own field gets the wrong answer, in the direction of over-counting blocks.

### 7.6 Empty-book NO priors disagree with the archive
Trader `_prior_k_candle_asks` (l.865): `yes_bid == 0` → NO prior ask = **100c**, which
passes the `>= 75` gate.
`scripts/archive_candles.py::candle_price` (l.106): `yes_bid == 0` → **None**, written
as blank, parsed by `backtest.load` as `-1.0`, which **fails** the gate.
Same class as the `depth==0` bug in the charter: zero means "not on offer", not a price.

Measured frequency (exact-cent era only, 2026-08-22 onward):
```
python3 - <<'P'
import gzip,csv,glob,os,collections
t=collections.Counter(); b=collections.Counter()
for p in sorted(glob.glob("data/candles/*.csv.gz")):
    if os.path.basename(p)[:10] < "2026-08-22": continue
    for r in csv.DictReader(gzip.open(p,"rt")):
        t[r["side"]]+=1
        if any(r[f"prior_{i}"] in ("","None") for i in (1,2)): b[r["side"]]+=1
for s in ("yes","no"): print(f"{s}: {b[s]}/{t[s]} = {100*b[s]/t[s]:.2f}% blank prior_1/2")
P
```
→ `yes: 4/1287 = 0.31%`, `no: 8/1276 = 0.63%`. Real but small; it puts a handful of
live NO entries permanently in reconcile's EXTRA bucket by construction.

## 8. Things that were checked and are correct

Recorded so the diff can tell "verified" from "not looked at".

| Claim | Verdict | Command |
|---|---|---|
| `MAX_CONCURRENT_POSITIONS=2` is honoured per close cluster | **holds** — 0 of 298 clusters exceed 2 | `METRICS.md` §6 |
| Settlement `revenue` is cents, `fee_cost` is dollars | **holds** — `fee_cost 0.1325` vs `0.07·26·0.921·0.079 = 0.1324` | `METRICS.md` §2 |
| Archive `candle_idx ± k` really is ±k minutes | **holds** — 1249/1249 consecutive pairs are exactly 60s apart | `METRICS.md` §7 |
| The v5.17 arithmetic (+$0.299 → +$0.338/tr) | **reproduces exactly** | `METRICS.md` §8 |
| Live win rate is below the model's | **not established** — 91.49% vs 93.54%, z = −1.81 | `METRICS.md` §5 |
| YES beats NO in the clean $25 window | **not established** — +1.97pp, z = 0.73 | `METRICS.md` §4 |
| Live fills are inside the band | **holds** — 100% of 500 in [90, 93] | §6 above |
| Slot allocation matches the harness | **holds by construction** — both sort `-secs_left` | §4.2 |
