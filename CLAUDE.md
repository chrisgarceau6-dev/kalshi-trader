# Kalshi Trading — Auto-Context

Claude Code reads this file automatically when opened in `/Users/chrisgarceau/pm/`.

**How to read this file.** It is organised by how fast things rot. Invariants are
structural and safe to rely on. Config is current and verifiable. Dated observations
are perishable. The kill-list at the bottom is *the least reliable section in this
file* — treat it as leads, not verdicts.

## Who / What

- Chris Garceau — UMass freshman. Real money on Kalshi. Wants concise responses, no
  encouragement without evidence, no emojis. Lead with numbers, then takeaways.
- Repo: `/Users/chrisgarceau/pm/` → GitHub `chrisgarceau6-dev/kalshi-trader` (PUBLIC)
  Renamed from `polymarket-monitor2` on 2026-08-18. **The Render service kept the old
  name**, so the dashboard URL is still `polymarket-monitor2.onrender.com` — that
  mismatch is expected, not stale. Renaming the service would change the URL with no
  redirect. Workflow self-dispatch uses `$GITHUB_REPOSITORY`, so it survives renames.
- Live entrypoint: `late_certainty_trader.py` on `origin/main`, workflow
  `.github/workflows/late_certainty.yml`
- **Primary trigger:** self-dispatch — each run sleeps ~30s then dispatches itself via
  `GH_DISPATCH_TOKEN`. **Backup cron:** `*/5` and `2-59/5` staggered.

---

# 1. Check claims, don't trust them

**Every strategy claim in this file has a command. Run it.**

```bash
python3 scripts/backtest.py                       # live config, full history
python3 scripts/backtest.py --compare yes_only=1  # baseline vs variant + bootstrap CI
python3 scripts/backtest.py --sweep max_ask 92 93 94 95 96
python3 scripts/backtest.py --slip 1              # one tick of adverse fill
python3 scripts/backtest.py --since 2026-08-13    # holdout window
```

Defaults are read out of `late_certainty_trader.py` by AST, so the harness cannot
drift from what is actually running. Data is `data/candles/*.csv.gz`.

**Why this matters.** This file used to record conclusions as prose with numbers
attached — "NO side is -EV", "two NOs per cluster is -$1,211", "C1 is -$7.25/trade".
None could be re-run, so none could be challenged, and on 2026-08-17 all three turned
out to be wrong or badly overstated after months of steering decisions. **A number
with no command behind it is an assertion, not evidence.** If you cannot reproduce a
figure with the harness, treat it as unverified no matter who wrote it down.

---

# 2. Invariants — these do not rot

1. **You risk $75 to win ~$6.50.** Break-even is ~92% WR (90.6% at 90¢ → 93.5% at
   93¢). You run ~94%. **The entire edge is that ~2pp gap**, harvested over thousands
   of bets. No single trade is good.
2. **One cent ≈ half your edge.** A 1¢ move in entry price shifts break-even by ~1pp.
   One tick removes ~69% of all profit (`--slip 1`). Execution quality dominates
   every strategy parameter. Guard it; it is already near-optimal so there is little
   to gain, and a lot to lose.
3. **The 7 series settle simultaneously.** They are one correlated bet, not seven.
   Any statistic must resample by *close cluster*, never by trade — the harness does
   this. Per-trade CIs are wrong here and will overstate significance badly.
4. **Kalshi retains settled markets ~67 days.** This is the binding constraint on all
   validation. See §6.
5. **These markets are fair coin flips.** 49.8% settle YES (n=27,908); spot is above
   the strike in 49.5% of markets. Neither side is structurally favoured, and there is
   no drift or strike-placement effect to exploit.
6. **Signals are transient — the backtest sees more of them than the bot can.**
   70% of qualifying signals are in-band for exactly ONE 1-min candle (median 1 of
   ~8 possible). The ask passes *through* 90-93¢ on its way to 100¢ as certainty
   resolves; it does not rest there. So **every backtest figure is an upper bound**
   — it evaluates every candle, while the live bot only sees the ask at poll
   instants. Before v5.16 the bot polled every ~48s and captured **27%** of the
   entries the backtest takes (23 of 86 on 2026-08-16). It now polls every ~15s
   inside a 240s job (~14-16 scans/job, ~18s effective cadence).
   **When live P&L runs below backtest, check capture rate before suspecting edge
   decay.** That gap explained a 3x shortfall that had been misread as decay.
   The missed signals are not worse: transient 93.30% WR / +$0.58 per trade vs
   persistent 93.27% / +$0.77.
7. **Kalshi opens a 15M market only ~600s before its close.** `MAX_SECS_LEFT=600`
   is therefore already at the structural ceiling — there is nothing earlier to
   capture, and raising it gains nothing.
8. **Post-hoc filter discovery has a terrible track record here.** H4, near-strike,
   ET13, UTC blackouts, C1, and a "dispersion filter" built on 2026-08-17 all looked
   convincing in-sample; the dispersion filter *inverted* out-of-sample. With ~2pp of
   edge and ~68 days of data, filter-hunting mostly fits noise. **The wins have come
   from removing restrictions, not adding them.**

---

# 3. Current strategy — v5.16 (2026-08-17)

Buy **either side** at ask [90,93]¢ with 150-600s left, provided the prior 2 same-side
1-min candles are all ≥75¢. If ask ≤91¢, also requires a 3rd prior candle ≥80¢. Hold
to settlement.

| Parameter | Value | Re-check |
|---|---|---|
| `FLAT_BET_DOLLARS` | 50 | balance-gated, see §4 |
| `MIN_ASK_CENTS` / `MAX_ASK_CENTS` | 90 / 93 | `--sweep min_ask 88 89 90 91` |
| `MIN_SECS_LEFT` / `MAX_SECS_LEFT` | 150 / 600 | `--sweep min_secs 100 150 200 250` |
| `PRIOR_MIN_CENTS` / `PRIOR_LOOKBACK` | 75 / 2 | `--sweep prior_min 70 75 80 85` |
| `YES_ONLY` | **False** | `--compare yes_only=1` |
| `MAX_CONCURRENT_POSITIONS` | 2 | `--sweep max_conc 1 2 3 4 --slip 0.105` |
| `MIN_BOOK_DEPTH` | 60 | not in harness (needs live book) |
| `CRASH_FILL_TOLERANCE` | 3 | fills this far under the band are logged, not emailed |
| `SURVIVOR_*` | shadow | logs 92-93¢→94-96¢ survivors; trades nothing |
| `STOP_BALANCE` | 650 | — |
| `CONSEC_LOSS_LIMIT` | 9 | never fires in 68d either way |
| poll cadence | 240s job / 15s interval | ~14-16 scans per CI job |
| `EDGE_DEGRADE_THRESHOLD` | 0.84 | catastrophic breaker only |
| `BLACKOUT_HOURS` | `set()` | — |

**Series:** KXBTC15M, KXETH15M, KXSOL15M, KXDOGE15M, KXBNB15M, KXXRP15M.
**KXWTI15M paused 2026-08-19** — shadow-logged, still archived, see §7.

**Order flow:** live-position refetch → fresh-ask refetch → prior-candle gates →
**book last look** (best offer + depth, one read, side-aware) → GTC limit at
`min(93, book_best+2)` → sleep 3 → cancel → query fills by `order_id`.

**The book read is the last call before the order, and the order is priced off it.**
The `/markets` quote is refetched *before* the candle gates, which cost 2-4 more API
calls, so by order time it is ~1s stale — that is the whole crash-fill mechanism (§7).
A buy limit is a **ceiling, not a floor**: a marketable order sweeps the book upward
from the best offer, so a crashed book gets bought at crash prices no matter what
limit is sent. The only guard that works is refusing to send the order when the book
is outside `[MIN_ASK, MAX_ASK]`. Orders log `book_age=Xms` — the residual race.

---

# 4. Why the config is what it is

**`YES_ONLY = False` (v5.16).** The NO side was suspended on live YES 46W/1L vs NO
74W/8L. That is **not significant**: z=1.64, two-sided p=0.102, on n=47 YES trades —
about five coin flips of luck, worth roughly the entire $210 P&L gap. Over the full
68 days NO wins **93.65%** and YES **93.79%**; the cluster-bootstrapped difference is
+0.75pp [-0.36, +1.86] in-sample and **-1.59pp** [-4.54, +1.35] on holdout — both
include zero and the sign flips. `--compare yes_only=1` reproduces it; the Aug 13-17
holdout independently confirms YES-only is worse (delta -$1,037, CI excludes zero).

**Execution gap = +0.105¢ (measured 2026-08-17).** Actual contract-weighted fill
91.968¢ vs backtest-predicted entry 91.863¢, over 1,486 fills / 19,245 contracts.
Fills are essentially at the quote. **Method matters:** compare *distributions*, not
each fill against a candle — per-fill comparison yields +0.85¢ that is pure artifact,
because the 1-min reference is stale 47% of the time and produces textbook regression
to the mean. Re-measure after NO accumulates settlements; we have **no NO-side fill
data at all**, and thin NO books are the main live risk of v5.16.

**`MAX_CONCURRENT_POSITIONS = 2`.** Caps a settlement cluster at $150 **regardless of
side** — worst observed cluster is -$150 with or without NO. 3-4 backtest better at
perfect fills and clearly worse at one tick, and raise worst-cluster loss to -$225.

**`MAX_ASK_CENTS = 93` is a slippage bet, not a validated edge.** 94-96¢ backtest
higher at quoted fills and worse at one tick: at 96¢ you win 4¢, so a 1¢ slip costs
25% of gross profit versus 10% at 90¢. Widening spends your best asset for a backtest
number. **Do not raise it without re-measuring fill quality first.**

**Position sizing — $50 as of 2026-08-19 (was $75).** The rule below is symmetric and
it cut, rather than raised: bet size must be ≤4.6% of balance. After a -$345 stretch the
balance was $1,088.87, where $75 is **6.9%** — above the design ratio — and $50 is 4.6%
exactly. Room to the $650 stop went from 6 losses to 9. Cost: the backtest edge scales
linearly, +$1.00/trade → +$0.67. Side effect worth knowing: `compute_daily_loss_limit`
is `bet x 4`, so the daily halt tightened from $300 to **$200** automatically. Raise back
to $75 when balance ≥ $1,630 (the ratio that held at $1,631), not before, and never on a
win-rate argument.

**Original sizing note — $75 until balance ≥ $2,200.** Bet size is a risk decision, never a
win-rate decision. Rule: raise only when the new size is ≤4.6% of balance (the ratio
$75 held at $1,631). Block bootstrap over 60d: $75 → 13.3% chance of touching the $650
stop; $100 → 24.6%. $100 buys +$372 of profit for -$386 of extra drawdown. Rejected.
The old "200 settlements ≥93% WR" gate was met at exactly 93.0% but tested the wrong
quantity — break-even is ~92%, so it authorised leverage on a 1pp margin.

**Daily loss limit ($300, rolling 24h) — validated, do NOT loosen.** Fires ~7 days in
68 at full capture. The trades it blocks are genuinely bad: **90.51% WR, -$1.62/trade
over 495 trades**, against ~92% break-even — blocking them is worth **+$802** over 68
days. Mechanism: the 7 series settle together, so a -$300 day is a whipsaw regime and
it persists for hours. Contrast with the polling gap, where missed trades won 93.30%
vs 93.27% for captured ones — that was a genuine leak; this is a filter that works.
More halts at higher volume is the control doing its job.

**All other halts check out too.** `CONSEC_LOSS_LIMIT=9` never fires in 68 days at
either volume. `MAX_CONCURRENT=3` is *worse* than 2 at measured fill quality
(+$108 vs +$112/day) and collapses under one tick (+$6 vs +$29/day) while doubling
per-cluster exposure. Nothing here is too conservative — verify with
`--sweep max_conc 1 2 3 4 --slip 0.105` before revisiting.

---

# 5. Operational

**Key files**

| File | Purpose |
|---|---|
| `late_certainty_trader.py` | THE live trader — surgical edits only (real money) |
| `scripts/backtest.py` | Canonical harness — every claim goes through this |
| `scripts/archive_candles.py` | Nightly archival (§6) |
| `data/candles/YYYY-MM-DD.csv.gz` | Canonical dataset, ~13 KB/day, from 2026-06-11 |
| `daily_summary.py` | Ground-truth P&L from settlements API (never trust state P&L) |
| `kalshi_auth.py` | RSA-PSS-SHA256 auth + `cancel_order()` |
| `kalshi_dashboard.py` | Render dashboard entrypoint |
| `test_order_safety.py` | Order-safety + NO-path regression tests |

**Auth.** Key ID in GitHub Secret `KALSHI_API_KEY_ID`; private key at
`~/.kalshi/private_key.pem` (NOT `pm/kalshi_key.pem`). Signature must include the
`/trade-api/v2` prefix. **CI passes the key as base64 PEM *content* in
`KALSHI_PRIVATE_KEY`, but `load_private_key()` only reads a file path** — any script
running in Actions needs an `_ensure_key()` helper (see `daily_summary.py` /
`scripts/archive_candles.py`) or it fails on every run. The GitHub remote URL contains
a PAT — flagged for rotation.

**PAT rotation — DONE 2026-08-19.** A fresh classic PAT (`repo`, `workflow`) is in the
`GH_DISPATCH_TOKEN` secret and the old token is revoked. Verified in the only way that
actually proves it: `workflow_dispatch` runs kept landing every ~4.2 min *after* the
revocation (02:34Z, 02:38Z), so the chain is running on the new token. The PAT is also
gone from `.git/config` (remote is a plain URL; git authenticates via `gh` +
osxkeychain) and from `.env`.

Order matters if this is ever redone: new PAT → set secret → confirm a self-dispatched
run succeeds → *then* revoke. Revoking first drops the trader to the `*/5` backup cron
and halves poll cadence. Note a successful dispatch alone does not prove *which* token
is in the secret — only a dispatch that survives the revocation does. GitHub exposes no
API listing personal access tokens, so this check cannot be automated.

Possibly still pending: `gh auth refresh -s workflow` — the keychain token lacked
`workflow` scope, so pushes touching `.github/workflows/` get rejected. Unverified.

**Push flow.** Never push to main.
1. `git fetch origin && git checkout -b <branch> origin/main`
2. `python3 -m py_compile late_certainty_trader.py` — must pass
3. `python3 -m pytest test_order_safety.py` — all must pass
4. `git push origin <branch>`
5. `TOKEN=$(git remote get-url origin | sed 's|https://\([^@]*\)@.*|\1|')`
6. `GH_TOKEN=$TOKEN gh pr create ...` then `gh pr merge <n> --squash`
7. **After merging, confirm the next Actions run actually succeeds.** Verifying that
   `main` has the right code checks the wrong thing. On 2026-08-17 a deleted test
   file left a stale reference in `late_certainty.yml`'s pre-flight step; the trader
   failed on every dispatch for **2h46m** and nothing surfaced it.
   Note runs created within ~1 min of the merge may still use the OLD workflow file —
   check a run whose `createdAt` is clearly after `mergedAt`.
8. **Before deleting any file, grep `.github/workflows/` for it.** The workflows
   hardcode filenames.

**Never `git add -A`** — the working tree holds ~730k lines of untracked research
CSVs. Stage explicitly. Resolve `.claude-flow/` conflicts with `git checkout --theirs`.

**Kalshi API**
- Candles: `/series/{s}/markets/{t}/candlesticks`
- Orderbook: `/markets/{t}/orderbook` → `orderbook_fp.no_dollars` (NO bids) and
  `yes_dollars` (YES bids). **A YES buyer lifts NO bids; a NO buyer lifts YES bids.**
  Getting this backwards was a real bug fixed in v5.16.
- Fills: `/portfolio/fills` — `fee_cost` has the exact fee. Note the account has
  non-late-certainty fills on it (pre-v5.6.4 94-99¢ entries, and sub-88¢ crash-through
  fills); filter to 88-93¢ for strategy analysis.
- Settlements `/portfolio/settlements`, positions `/portfolio/positions`,
  deposits `/portfolio/deposits`
- Spot: Coinbase `api.exchange.coinbase.com` (Binance geo-blocked)

**Dashboard.** https://polymarket-monitor2.onrender.com — Render free tier, ~30s cold
start. P&L / Balance toggle, auto-refresh 30s, ranges floor at
Aug 1. The P&L tab shows the range's **time-weighted return** — each settlement chained
against the balance it was earned on. Never show a percent on the Balance tab: that
line moves on deposits, so a $100 deposit into a $400 account would read as +25%. Env vars are
`KALSHI_API_KEY_ID` and `KALSHI_PRIVATE_KEY` (raw PEM, not base64), plus `DASH_TOKEN`.

**`DASH_TOKEN` is required on Render.** Until 2026-08-18 the dashboard served live
balance, full deposit history and open positions to anyone — unauthenticated, at a URL
published in this public file. An unauthenticated HTML request now renders a **login
screen**; submit the token there. `/?t=<DASH_TOKEN>` still works and redirects to a
clean `/` after setting a 90-day cookie, so the token does not linger in history.
`/api/*` returns JSON 401. A hosted instance with no `DASH_TOKEN` returns 503 rather
than failing open; local runs bind to 127.0.0.1 and stay open.

**Cookies are per-context.** An iOS home-screen app keeps its own jar separate from
Safari, so each device *and* each installed app logs in once. That is why the login
form exists rather than a token-in-URL flow.

**UI (rebuilt 2026-08-18).** Hand-rolled SVG chart — **no Chart.js, no CDN, the page
has zero external dependencies.** Time-indexed x-axis, drag-to-scrub with crosshair
and haptics, tweened hero, skeleton shimmer while loading, PWA meta so it runs
chromeless from the home screen. Responsive: 620px phone column, 1280px desktop with
6-across stats and positions/trades side by side. Any chart work happens in the
`HTML` string in `kalshi_dashboard.py`; keep the phone breakpoint untouched.

**Balance and percent semantics — do not regress these.** The hero and the balance
line are anchored on **equity (cash + open position cost)**, not `/portfolio/balance`.
Cash excludes money tied up in open positions, and since the curve is reconstructed
backwards from the live figure, anchoring on cash back-dated that hole across the whole
range: implied starting capital came out low by exactly the open cost and every chained
return was inflated (61.02% vs a true 48.16% on the regression scenario). The balance
line also counts **every** settlement, not just 15M ones — non-strategy rows still moved
cash, and dropping them shifted the curve by their total. Strategy stats (WR, trades,
P&L tiles) filter on the `strat` flag instead. The percent is **return on the capital actually
at work** — range P&L over the opening balance plus each in-range deposit weighted by
how long it was invested (modified Dietz) — and is **hidden entirely** when the
reconstruction cannot be trusted — a settlement with no
capital before it, or a negative implied start (a withdrawal, or history older than
`SETTLEMENT_FLOOR`). The ALL range floors at Aug 1, so it is labelled "Since Aug 1"; it
is not all-time and must not be called that.

**The balance curve is anchored at the present and walked backwards.** The balance at
any past instant is today's equity minus everything that has happened since. The
earlier forward-sum needed a `drift` term to reconcile with the live balance, which
silently assumed the event feed reached back to the account's first day — it does not.
Settlements stop at `SETTLEMENT_FLOOR` while deposits go back months (Feb 2026 on this
account), so drift absorbed every missing trade and smeared it across the curve; with
real data it went negative and suppressed the percent entirely. Walking backwards needs
the feed complete only from the range start forward, which it is. Verified: truncating
months of early settlements does not move the reported return by 1e-9.

**Never chain per-trade returns here.** A time-weighted return chained across ~1,400
trades depends almost entirely on the balance the reconstruction believes existed at
the range start, so a bad anchor is amplified rather than damped: on 2026-08-18 the
page read **+169.42% next to $221.98 of P&L**. Ground truth for that window, from three
independent sources: the trader logged **$367.06** at 2026-07-31T23:59Z (Actions run
30674337025) one minute before the Aug 1 floor; `/api/data` showed $1,380.54 cash with
an empty positions list; the two in-range deposits were $300.86 and $490.00. Those give
P&L = 1380.54 − 367.06 − 790.86 = **$222.62**, against **$221.98** summed independently
from the settlement feed nine minutes later — so the feed is complete and there were no
withdrawals. Correct answer: **+31.05%** on $715.01 of capital at work. The reconstruction
reproduces the $367.06 anchor to the cent; `scratchpad/real_check.js` pattern is worth
rebuilding if this is ever touched again.

**Reconciliation banner.** Nothing in the API proves that deposits + settlements
explain every dollar the account moved: withdrawals have no feed, and history older
than `SETTLEMENT_FLOOR` is simply absent. Both vanish into `drift`, the term the whole
curve is anchored on. So the page samples equity into `localStorage` each refresh and
compares the change against what the event feed says should have changed; a gap beyond
`max($2, 0.25%)` raises an amber banner naming the amount and the time. Entry fees are
tracked explicitly (charged at fill, booked at settlement) rather than absorbed into the
tolerance. The banner also fires whenever the percent is suppressed. It is per-device —
localStorage — so a first visit on a new device establishes a baseline and reports
nothing.

**Position-card semantics — do not regress these.** "If win" is *profit*
(`contracts x (1 - entry) - fee`, ~$6.50), **not** gross settlement (`contracts x $1`,
~$81) — that bug overstated upside >10x. "At risk" is cost basis from fills. The card
quotes the side actually held; showing the YES book for a NO position displays ~9c
against a 91c entry, and v5.16 trades NO about half the time. Entry price is not on
the positions endpoint — `get_fills_basis()` derives it from fills.

**Trader health pill.** The dashboard reads the Actions API
(`/actions/workflows/late_certainty.yml/runs`, unauthenticated, 90s cache) and shows
last *successful* run plus consecutive-failure count. Staleness alone would not have
caught 2026-08-17 — cron kept creating runs every 5 min while every one failed, so the
failure count is the fast detector. Successful runs land every ~4.2 min (median, n=34),
so a healthy last-success age cycles 4-9 min; amber >15 min, red >25 min or 2+ failures.
`cancelled` is routine (cron collides with self-dispatch) and never counts as a failure.

**The blackout banner is read from the trader by AST**, like `scripts/backtest.py`.
It was hardcoded to `[13]` while the trader ran `BLACKOUT_HOURS = set()` — every day at
1pm ET the dashboard claimed the strategy was paused while it traded through. Never
hardcode a strategy constant into the dashboard.

---

# 6. Data archival — read before proposing any research

**Kalshi retains settled markets only ~67 days.** This one fact blocked every strategy
question this project ever asked: by the time a hypothesis exists, the only data that
exists is the data it was formed on. The NO question sat unresolved for months because
its gate demanded 90 days of clean holdout — on 2026-08-17 the entire available
holdout was **7.6 days**.

`.github/workflows/archive_candles.yml` runs nightly and commits
`data/candles/YYYY-MM-DD.csv.gz` (all series, both sides, ask 88-96¢, 100-800s —
deliberately wider than the live gates). Backfilled to 2026-06-11, no gaps.

**Every archived day is untouched out-of-sample data for every hypothesis formed after
it.** Backfill with `--backfill N`. Never delete this directory. If the workflow has
been failing, fixing it outranks whatever research prompted the check — a day not
archived is validation capacity permanently destroyed.

---

# 7. Dated observations — perishable

Each needs re-checking; none is settled.

| Observation | Date | Status |
|---|---|---|
| WTI paused; Gold/Silver still out | Aug 19 | All three launched **2026-07-31** (verified: 0 markets pre-July, 24 on Jul 31). WTI was added v5.8 on a 13-day backtest (+$1.75/tr OOS); over its whole life it measures **-$0.33/tr on 290 trades** — the justifying evidence inverted. Silver (+$0.31/tr) was *better* than WTI while excluded, so trading one and not the others was an accident of timing. Paused, not condemned: -$0.33 is ~1.1 SE from break-even. Revisit all three at ~1,000 trades each, together, one standard. |
| SOL is fine — August was noise | Aug 19 | Full archive: **1,810 trades, 93.15% WR, +$0.41/tr**. August alone reads -$0.12/tr. Picking any 18-day window makes some series look broken; this is exactly what Invariant 8 warns about. Do not act on single-window series stats. |
| Gold/Silver first real read | Aug 19 | 15 trading days, live gates, no slippage, no concurrency cap: **GOLD 357 trades 89.92% WR -$1.28/tr** (worst in the book), **SILVER 369 trades 92.95% WR +$0.31/tr**. Neither is established (~1.1-1.6 SE). Metals trade weekdays only — ~5/7 the days of crypto, so per-day comparisons mislead. Archived from 2026-08-01 onward. |
| Edge by price: dies at 95¢ | Aug 18 | 88¢ **+1.42pp**, 91¢ +0.98, 92¢ +0.70, 93¢ +0.66, 94¢ +0.95 (all significant); 95-96¢ **+0.07pp — gone**. n=83,337 obs / 6,402 clusters. Independent support for the `MIN_ASK=89` lead. `python3 scripts/calibration.py` |
| Entry timing: earlier is better | Aug 18 | Edge by time left: 100-150s **-1.26pp**, 150-240s -0.57, 360-480s **+1.27**, 480-600s +0.87. The 60s-average settlement is priced, possibly over-priced — late entries are not safer. `python3 scripts/calibration.py` |
| Waiting for a better price loses | Aug 18 | A 90-91¢ contract at 8-10 min is **gone from the 88-96¢ band 85.8% of the time** by 3-4 min. Buying what is *still* 90-93¢ late: -3.31pp, **-$2.71/trade** — adversely selected. Buying early: +$0.58/trade. `python3 scripts/entry_timing.py` |
| Survivor re-entry (92-93¢ → 94-96¢) | Aug 18 | +3.95pp in-sample, +4.88pp holdout — but 41 holdout obs with ~zero losses, so the rule-of-three floor (WR≥92.7%) sits **below** the 95.2¢ break-even. **Shadow-logged only** (`[SHADOW:SURVIVOR94]`), revisit at n≈500. |
| BRTI runs rich vs Coinbase | Aug 18 | Strike (a BRTI print) is **+0.96bp above** Coinbase at the same minute, sd 2.41bp, \|basis\|>10bp in 0.5% of windows. ~12% of a typical 15-min move — it biases every near-strike call the bot makes on Coinbase data. `python3 scripts/calibration.py` |
| Volume is no longer the constraint | Aug 18 | Cumulative counter ran 7 → 138 across Aug 18: **~140 trades/day live vs 121/day modelled**. The 27% capture rate in Invariant 6 is pre-v5.16 and stale; live now trades *more* than the backtest universe, at lower WR — suspect the marginal extra trades. |
| Config sweeps: nothing established | Aug 18 | `min_ask`=89 +$481 P=0.79 · `max_conc`=4 +$437 P=0.65 · `max_ask`=94 +$427 P=0.67 (at 0.105¢ slip) · `min_secs` flat. Four independent levers, none significant — consistent with Invariant 2 that the config is near-optimal. |
| 15M vs hourly parity — untested | Aug 18 | Both settle on the identical BRTI print at the top of the hour, so the KXBTCD ladder interpolated at the 15M strike is a second price for the same event. They only coexist in the final ~10 min, so it needs a sampler firing at :50. Nothing measured yet. |
| Crash fills are **+EV so far**, not the leak | Aug 18 | 12 fills landed below the band on Aug 18 and settled **11W/1L, +$90.63** (avg +$7.55/trade vs the ~$6.50 target). The two deep ones netted -$6.48 (-$47.48 DOGE @57.6¢, +$41.00 BTC @47¢). Cheaper entry pays more when it wins. Do **not** auto-exit them on instinct; n=12. |
| The actual leak is the core, not the fills | Aug 18 | v5.16 sits at **120/135 = 88.9% WR, -$236.20** against a ~92% break-even. Aug 18 alone: about **-$164 from in-band fills** while crash fills added +$90.63. Unexplained — needs its own investigation before any parameter is touched. |
| C1 quarantine (SOL + prior2 75-79¢) | Aug 17 | Worth **~$260 over 68 days** — noise, not the "-$7.25/tr" originally recorded. Quarantined on 60+80 trades, violating the 500-trade bar. Left in place as harmless; do not cite as precedent. `--compare c1=0` |
| `MIN_ASK = 89` may beat 90 | Aug 17 | Better at *both* slippage levels — the only parameter that didn't flip. Worth real work. `--sweep min_ask 88 89 90 91` |
| ET08 hour | — | -$2.39/tr, unconfirmed across periods. Needs 500+ trades. |
| BNB exclusion | — | 92.2% WR, +$0.06/tr — borderline. |
| Thursday blackout | — | 349 trades at -$1.20/tr — suggestive, underpowered. |
| C5 (prior1≥95 + prior3≥95) | — | 54 OOS trades — not blockable. |
| $100/trade bump | Aug 17 | Hold until balance ≥ $2,200. |
| Window-based consecutive loss | — | Group by expiry timestamp, not individual trades. |

**Crash fills (was "DANGER FILL", renamed Aug 18).** A fill below `MIN_ASK` means the
order swept a book that had already collapsed — not that a limit was breached. Aug 18's
worst: 80 BTC NO at **47¢ average on a 92.5¢ quote**, and 80 DOGE YES at **57.6¢ on a
92.8¢ quote**. The exchange fee confirms the price is real (`0.07·C·P·(1-P)` came to
$1.40, which only solves near P≈0.47) — this is not an accounting artifact.

Two regimes, and they must not be conflated:
- **within 3¢ of the band** — the book moving inside the order's flight time. Benign;
  logged, never emailed. Six of Aug 18's twelve.
- **deeper** — a genuinely different bet: a ~50¢ contract is a coin flip with ±$40
  swings, against a strategy sized to risk $75 to win $6.50. Emails as `CRASH FILL`.

Counting them: `gh run list --workflow=late_certainty.yml --limit 400 --json databaseId,createdAt`
then grep run logs for `SETTLED <ticker>`; the settlement line for a market appears in
the run that was already in flight at its close, so search from `close-5min`. Alert
history is in Gmail (`subject:"CRASH FILL"`, `subject:"DANGER FILL"` before Aug 18).

---

# 8. Not currently pursued

**Calibration warning:** this section was previously titled "tested and rejected" and
every row said "Dead". On 2026-08-17, two of its NO-related claims failed to
replicate, and the headline "NO is -EV" verdict was overturned entirely. These are
**leads about where effort went**, so you don't redo expensive work — they are not
verdicts, and none of them can be reproduced from anything in this repo. If a
question matters, re-run it with `scripts/backtest.py` against current data.

*Volatility / dispersion filter (pre-registered, 2026-08-18):* **refuted.** Bucketing
entries by prior-candle price range (`max-min` of ask,p1,p2,p3) gives a *non-monotonic*
WR — MID is the worst bucket in both windows — and the cluster-bootstrapped HIGH-LOW
delta includes zero in-sample and out-of-sample. Decisive point: HIGH-dispersion entries
are **66% of volume and 77% of holdout profit**, so excluding them cuts holdout P&L from
+$4,242 to +$992. High dispersion is the ask *transiting* 90-93c toward 100c as certainty
resolves (Invariant 6) — the strategy working, not a danger signal. Note the prior-candle
gate (`PRIOR_MIN=75`) is already a volatility filter; this tested for residual signal after
it. Third attempt at this idea (`backtest_vol_filter.py` Aug 9, dispersion filter Aug 17
which inverted OOS). `python3 scripts/vol_bucket_test.py`

*Directional / entry variants:* longshot crash-reversal · cross-asset lag · candle
acceleration · stuck-market breakout · per-series WR kill switch · early-window entry
(600-800s) · spot-Kalshi dislocation scalp · oracle-lag final 0-150s ·
KXETH15M exclusion (would have **cost $978** — ETH is the best series; do not re-raise
on a losing streak).

*Direction-neutral structures (Aug 15-17, all negative after fees):* complete-set
accumulator · matched-pair maker · cross-crypto relative value · market-neutral pairs ·
vertical/cross-strike arb · all-taker catalog scan · maker-then-hedge · WTI ladder
maker · liquidity-reward farming · hourly range-pin · one-touch barriers.

*Other markets:* new 15M series (HYPE -$1.76/tr, NEAR, Gold, Silver) · weather
crossed-strike · cross-venue sports arb (**account is US-only** — this kills most
venue arb) · Kalshi vs sportsbook · sports-futures dominance · Fed complete set.

*Cross-listing 3-leg arb (range book vs threshold ladder):* **real and verified but
economically worthless** — 4 opportunities in 14 days worth **+$1.05 total** at 10
contracts, requiring a websocket service and non-atomic batch orders. Re-scan with
`PYTHONPATH=. python3 scripts/xlist_arb.py BTC ETH` before ever reconsidering.

Raw 2026-08-15/17 work: `~/Documents/Codex/2026-08-12/i-ran-a-full-ablation-study/work/`

---

# 9. Rules

1. Read files before editing. **Surgical edits only to the live trader — real money.**
2. Never add "while I'm here" cleanups or abstractions to the live trader.
3. Never paste secrets into chat. Never commit `.env` or key files. Repo is public.
4. **Any claim you add to this file must carry the command that reproduces it.**
5. **Evidence standard:** 500+ trades per bucket OR a pre-registered hypothesis before
   treating a slice as confirmed. Post-hoc slices are hypothesis generation only.
   1,700+ 2-way combos were tested in the ablation — nothing survives without
   preregistration.
6. **Resample by close cluster, never by trade.** Seven series settle simultaneously.
7. Prefer removing restrictions to adding filters (see Invariant 6).
8. Local main is NOT live main — always branch from `origin/main`.
9. Live state is in the GitHub Actions cache, not local `certainty_state.json`.
10. Ground-truth P&L: `daily_summary.py` or Gmail `[Kalshi]`. Never state-derived P&L.
11. Never bump `STRATEGY_VERSION` unless strategy LOGIC changes (resets cumulative stats).
12. All timestamps ET (America/New_York). Archive filenames are UTC days.
13. When results disagree with this file, **this file is probably the wrong one.**
