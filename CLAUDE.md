# Kalshi Trading — Auto-Context

Claude Code reads this file automatically when opened in `/Users/chrisgarceau/pm/`.

**How to read this file.** It is organised by how fast things rot. Invariants are
structural and safe to rely on. Config is current and verifiable. Dated observations
are perishable. The kill-list at the bottom is *the least reliable section in this
file* — treat it as leads, not verdicts.

## Who / What

- Chris Garceau — UMass freshman. Real money on Kalshi. No encouragement without
  evidence, no emojis.
- **RESPONSE FORMAT — critical.** Report only what he needs to stay informed and in
  control. Numbers and tables, not prose. Cut ~90% of what you would naturally write:
  no method narration, no restating his question, no describing work in progress, no
  listing what you considered and discarded unless a discarded thing changes a
  decision. Structure: **what changed → what it means → what's next / what needs his
  call.** Findings go in tables. State caveats in one line, not a paragraph. If a
  detail does not change a decision he would make, leave it out.
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

1. **You risk $50 to win ~$4.35.** Break-even is ~92% WR (90.6% at 90¢ → 93.5% at
   93¢). **The entire edge is the gap above break-even**, harvested over thousands of
   bets. No single trade is good.

   **The realised gap is far smaller than this file used to claim.** It said "you run
   ~94%", and the win rate is indeed 94.10% since Aug 1 (1,482W/93L on 1,575 trades) —
   but that has delivered **+$82.14 total, or +$0.05/trade**, against the +$1.11/trade
   that 94.10% implies at flat $50. Do not quote the win rate as if it were the edge.
   Two separate reasons, and they must not be conflated:
   - **Aug 1-11 is a sizing artifact** (§7): bet ran $2.79 → $74 inside the window, so
     most wins were banked at $0.20 while losses landed at $45-74.
   - **Aug 12-21 is not.** Sizing was $50-75 throughout and the win rate was
     **92.12%** (538W/584) — only **0.62pp** above break-even, worth **+$0.056/trade**
     realised. The split from Aug 1-11's 95.26% is -3.13pp, z=2.55, p≈0.011
     unclustered (clustering widens it; treat as suggestive, and note it is confounded
     by the NO side returning Aug 17 and WTI pausing Aug 19).

   Practical consequence: at 92% the strategy is roughly break-even, and ordinary
   variance is the entire experience. Any projection built on +$0.54-0.80/trade — the
   harness figure — is an upper bound the live account has not delivered since Aug 11.
   Check `daily_summary.py` before trusting a per-trade number in this file.
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
| `FLAT_BET_DOLLARS` | **25** | cut 50 -> 25 in #151 (2026-08-22); balance-gated, see §4 |
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
GOLD/SILVER are shadow-logged and archived too, so all three carry the same evidence
when they are judged together. The archive is not a substitute: it sees every candle,
the shadow log only what the live poller could have caught (Invariant 6).

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

**Daily loss limit ($300, rolling 24h) — validated, do NOT loosen.** The formula is
`max(300, bet x 4)` as of 2026-08-19: a bare `bet x 4` meant cutting the bet to $50
silently retightened the threshold to **$200**, a level nothing has tested, and it
held the trader halted for most of 2026-08-19. The $300 floor is the level the
evidence below actually refers to. Note halting does not pause data collection —
`archive_candles.py` runs regardless; only live fill evidence stops. Fires ~7 days in
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
| `scripts/missed_pnl.py` | Prices what a halt cost — replays live gates on public candles |
| `scripts/reconcile.py` | Splits the live-vs-model gap into capture / selection / execution |
| `scripts/gate_replay.py` | Scores any version on what the bot ACTUALLY SAW (not an upper bound) |
| `scripts/calibration.py` · `scripts/entry_timing.py` | Edge by price / by time left |
| `research/kalshi_incentives/` | Incentive-program investigation (see §8) |

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

~~Possibly still pending: `gh auth refresh -s workflow`.~~ **Resolved 2026-08-20** —
a push touching `.github/workflows/archive_candles.yml` went through (PR #139), so the
keychain token does carry `workflow` scope. No action needed.

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
**Adding a series needs a FORCED backfill.** `--backfill N` skips any date whose file
already exists, so a new series captures nothing historical without it. The workflow
takes a `force` input: `gh workflow run archive_candles.yml -f backfill=N -f force=true`.
Costs ~3 min/day of runtime. Used 2026-08-19 to capture Gold/Silver back to Aug 1.

**Every archived day is untouched out-of-sample data for every hypothesis formed after
it.** Backfill with `--backfill N`. Never delete this directory. If the workflow has
been failing, fixing it outranks whatever research prompted the check — a day not
archived is validation capacity permanently destroyed.

---

# 7. Dated observations — perishable

Each needs re-checking; none is settled.

| Observation | Date | Status |
|---|---|---|
| **#152 MEASUREMENT RESULT — SPLIT verdict, decision NOT yet made** | Aug 24 | The pre-registered clean-day test ran on 2026-08-23. **Capture 45.1% -> 65.3% (+20.2pp)**, the highest of any day measured. **EXTRA did NOT fall**: 25.3% -> 46.3% per opportunity, count 38/day -> 56. #152's own commit message says: *"If EXTRA does not fall, the mid-candle-noise explanation is wrong and this should be reverted."* **By the letter of the pre-registration, revert.** BUT the metric was a proxy and the thing it proxied for inverted: **EXTRA went from -$3.503/tr (-$563.98 over Aug 18-22) to +$0.153/tr (+$8.58)**, WR 85.71% -> 92.86%. Live total -$497.79 -> **+$98.11**. Confound checked and rejected: Aug 20 was also a strongly positive day (+$122.56 live) and EXTRA still lost there (-$0.464/tr), so this is not a good-day artifact — per-day EXTRA $/tr runs -9.004, -0.464, -5.989, -2.418, **+0.153**. **The tension is real and is a judgement call for Chris, not for whoever reads this next.** Note the honest risk: this is exactly the "rescue it with a new story" trap the pre-registration was written to prevent, and one day at n=56 EXTRA cannot distinguish a fixed selection problem from a lucky one. A `git revert e6912024` **CONFLICTS** — #157/#161-166 touched the same region — so reverting is a manual edit of the scan-phase block, not a one-click operation. |
| **Depth gate is NOT too strict — the $48/day was an artifact** | Aug 23 | `depth==0` is **50.6%** of all gate-log rows and **88% of every block**, and it does not mean "thin book" — it means **nobody is offering at the price the signal fired on**. For depth-0 rows the real best price is **+1.30c worse** (median, 85% of cases); for depth 1-59 it is +1.15c worse. Only depth>=60 fills **better** than the signalled ask (-1.15c), which is exactly why execution measures as an ASSET (+$77.72). The backtest scored the blocked population as fills at the candle ask, so the "~$48/day" was pricing trades that were never purchasable. Arithmetic: at 92c break-even is 92.0% against ~93.5% WR (+1.5pp); pay 1.3c more and break-even is 93.3% -> **+0.2pp, gone**. `MIN_BOOK_DEPTH=60` is a near-perfect availability detector. Do not loosen it. |
| **88-89c YES-only, run as a SEPARATE book — the one survivor** | Aug 23 | Best new candidate found in the Aug 23 search. Disjoint from live by construction (below the band, YES only): **n=1,833, WR 90.73%, +$0.39/tr, +$9.88/day** at 0.105c slip, bootstrap **CI [-35,+1435], P(>0)=0.970**, and critically **84% executable** (depth>=60) versus 11-15% for the 94-96c candidates. Survives every split: slippage 0->0.105->0.5->1 tick = +0.421/+0.393/+0.287/+0.155 (never inverts), all three time-thirds positive (+0.267/+0.831/+0.109), 5 of 6 series positive (DOGE -0.267 the exception). **Side asymmetry is the whole point** — at 88-89c YES makes +$0.39/tr while NO loses **-$0.42/tr**, so a symmetric sweep cancels it to -$2.07/day, which is why `MIN_ASK=89` never showed up as significant. **Only additive with its OWN concurrency slots**; on the existing two it displaces live trades — the same reason `max_ask=94` reads P=0.65 as a config widening and P=0.998 as a separate book. Realistic after executability and the ~47% live capture: **+$4-5/day, ~+20%, NOT a doubling.** Caveats: CI touches zero, and the latest third is the weakest. |
| ask-94 as a separate book — killed by executability | Aug 23 | Model looks strong (n=3,628 disjoint, WR 95.48%, +$14.28/day, **P(>0)=0.998**, holds across halves and 6/6 series) but **only 11-15% of 94-96c signals have depth>=60** versus 47% in the live 90-93c band. Realistic value is ~15% of the model, **~+$2/day**. Notable structural oddity worth remembering: **BNB (+0.535) and SOL (+0.464) are BEST at 94c while being worst in the live band** (+0.03, -0.11) — whatever drives edge at 94c is not what drives it at 91c. |
| **Weather is CLOSED — three independent methods** | Aug 23 | 2.04M vol/24h (2nd largest complex on Kalshi, 40+ temperature series incl. international) and **none of it is usable**. (1) *Forecast edge fails:* Brier **model 0.1499 vs market 0.0932 on n=1,886, market better in 6 of 6 cities**, edge -0.0567 against a +0.0100 gate; the threshold rule loses **-$4,003 over 1,160 trades**. Open-meteo ensembles are public, so they are already in the price. (2) *No mispricing to harvest:* T-24h calibration **n=291, mean_p 0.170 vs actual 0.175 — a 0.5pp gap**, i.e. correctly priced (crypto is +1.42pp at 88c). (3) *The population does not exist:* across **1,184 quote observations**, only **0.9%** sit in 88-96c — **0% inside 6h of close**. Do not reopen this on the strength of the volume figure; the volume is real and irrelevant. |
| **Archetype filter — which markets CAN host this strategy** | Aug 23 | Generalises the weather result. The late-certainty edge requires a **continuously-priced underlying with a fixed short deadline**: the underlying sits near the strike and each passing minute mechanically walks P through 88 -> 91 -> 93 -> 96, creating the band. **Event markets — weather, sports, politics — resolve in JUMPS**: once the afternoon high is in, price goes straight to 100 and never spends time at 92. They structurally cannot host this strategy at any liquidity. Applying the filter to everything with real volume leaves: crypto 15M (live), **KXSILVER15M (the lead)**, KXGOLD15M (-$0.42/tr, tested), KXWTI15M (paused), INX/NDQ 15M (zero volume), and **FX/index hourly (KXINXU 9,617, KXEURUSDH) — the one genuinely untested corner**. For this archetype the space is close to exhausted. |
| Doubling P&L is a slots decision, not a search result | Aug 23 | Every candidate shares one bottleneck: the same six underlyings, the same **2 concurrency slots**, the same fee floor. A second strategy on the existing slots **displaces** live trades rather than adding to them. The Aug 23 search closed weather, the depth gate and ask-94, and returned exactly one candidate worth ~+20%. More P&L therefore comes from `MAX_CONCURRENT` (swept but unestablished: `max_conc=4 +$437 P=0.65`) or a genuinely uncorrelated venue (Silver, ~late Sep) — the first is a risk decision about simultaneous exposure and a shared daily loss limit, and no backtest settles it. |
| **Silver ≠ Gold — the bundle was hiding a lead** | Aug 23 | Scoring the archive per series (Aug 1-22, live gates, $25 flat) ranks **KXSILVER15M 4th of 9 at 93.52% WR, +$0.32/tr, +$7.04/day** — above XRP (+$0.17), BNB (+$0.03) and SOL (-$0.11), all of which are LIVE. Gold is genuinely bad (**-$0.42/tr, worst of nine**), so averaging the two as "metals" carried opposite signs and buried Silver. Survives slippage where the live marginals do not: +0.32 -> **+0.29** at the measured 0.105c -> **+0.07** at one full tick, while BNB and SOL both invert. **NOT established:** bootstrap 95% CI **[-$155, +$415], P(>0)=0.842**, n=463. The ~1,000-trade bar lands **~late Sep** at ~29 trades/trading-day (metals are weekdays only) — it is already archiving, so this needs no work, only the calendar. Do not act before then, and note this is the SAME strategy on an uncorrelated underlying (diversification, not a second edge). Re-run: `python3 scripts/backtest.py --since 2026-08-01` then split by `r[0]` via `backtest.load`/`simulate`/`summary`. |
| Searchable space is 76 series, not 13,389 | Aug 23 | Kalshi lists **13,389 series**, but validating an edge needs ~1,300 comparable trades, so frequency is the binding filter: `fifteen_min` **19**, `hourly` **57**, `daily` 250, and **12,433 (93%) are annual/one_off/custom** which can never accumulate the sample. Of the 76 high-frequency series, **37 carry real volume/OI**. The ~3% historical hit rate across ~30 tested ideas is therefore not idea scarcity — it is that direction-neutral structures all die to the same fee load (§ fees below), 11 for 11. **Field-name trap:** the markets endpoint uses `volume_fp` / `volume_24h_fp` / `open_interest_fp` / `yes_bid_dollars`, NOT `volume`/`open_interest`/`yes_bid`; querying the latter returns 0 for every series **including ones the bot is actively filling**, which reads as "market is dead". |
| Fees are 0.539pp of break-even — put them INSIDE it | Aug 23 | Break-even is `(cost + fees)/contracts`, never `cost/contracts`. The fee-blind form is a bar a winner clears while the account still loses: the dashboard showed **+0.34pp green on a since-Aug-1 window that had lost $64.97**, and 2026-08-19 reads **+0.06pp margin against -$18.68 realised**. At ~0.539c/contract the omitted term is larger than the entire margin being reported. Fixed in #174; across all seven settled windows the corrected margin sign now matches the P&L sign, where the old form disagreed on one. This is also why every direction-neutral structure in §"Direction-neutral structures" died. |
| Flat sizing closed the dollar-weighting drag | Aug 23 | The 1.204 loser/winner size ratio that dragged the effective rate ~1.2pp is **gone**: at flat $25 the ratio is **0.998** and dollar-weighted WR now equals trade-weighted (94.38% both, since #151+audit fixes). Mechanical, not luck. That window ran **+2.52% on 160 trades**, but p=0.0497 against prior with a bootstrap CI of **[-1.64%, +6.02%]** — consistent with a real edge, not evidence of one. |
| **The edge is fragile, not broken** | Aug 21 | The most important frame in this file. Break-even 91.5%; a **1.4pp** win-rate wobble — routine, and undetectable at n=584 (z=1.34, p=0.18) — is the difference between **+$0.62/tr and +$0.07/tr**. Live dollars exactly match live WR, so there is no hidden P&L leak; the whole question is always "is this WR real or noise", and you need **~1,300 trades (~2 weeks)** before a 1.4pp gap is even 2σ. Nobody can tell unlucky from degraded faster than that. Do not act on shorter windows. |
| Strategy is NOT decaying | Aug 21 | Model edge by fortnight: Jun 11-24 **+$0.84**, Jun 25-Jul 8 **+$0.14**, Jul 9-22 +$0.54, Jul 23-Aug 5 +$0.54, **Aug 6-20 +$0.91** — the most recent fortnight is the best in the archive. Jun 25-Jul 8 shows the model itself running near break-even for two weeks and recovering. `python3 scripts/backtest.py --since X --until Y` |
| Capture rate — **disputed, do not quote a number** | Aug 21 | `audit2.py` reports 75.2% for Aug 20 and 41.4% for Aug 19. `scripts/reconcile.py`, built later the same day and also restricted to `SERIES_LIST`, reports **38.0%** and **17.9%** — a consistent ~2x on both days, which points at a denominator difference (log-derived entry *attempts* vs settled *fills*) rather than a data disagreement. Until one is reconciled against the other, capture is an open question and the "capture is fine, no WebSocket needed" conclusion does not stand on it. `python3 scripts/reconcile.py --since 2026-08-20 --until 2026-08-20` |
| Selection IS costing money — **reverses the entry below** | Aug 21 | Full reconciliation over Aug 12-20 (n=533 live, 1170 model): live-only trades run **205 at 90.24% WR, −$250.56**, below the ~92% break-even. Execution is an *asset* (+$77.72; fills land 0.239c BETTER than the modelled ask, and matched-trade WR equals model WR to the decimal). Capture costs the most in absolute terms (−$506.37) but missed trades win at 93.47% vs 93.90% taken — same quality, so it is a volume leak, not a selection leak. `python3 scripts/reconcile.py --since 2026-08-12 --until 2026-08-20` |
| ~~Aug 19 "selection leak" was a halt artifact~~ — **superseded, see above** | Aug 21 | Aug 19 showed bot-only extras at 76.2% WR / −$8.65/tr and looked like a slot-allocation defect. Aug 20 (clean): same population ran **93.8% WR / +$0.91/tr**. Random series order is not costing anything; do not "fix" slot allocation. |
| Live per-side, first real read | Aug 21 | Aug 20 settlements by side: **NO 37tr 97.30% +$2.68/tr**, YES 54tr 92.59% +$0.30/tr, all 91tr 94.51% **+$1.27/tr** — which *beat* the model's +$0.62 for that window. NO-side execution was the prime suspect for the live-vs-model gap; this points the other way. n=37, one day. |
| ~~Thin-book gate may be too strict~~ — **REFUTED Aug 23, see top of table** | Aug 21 | `MIN_BOOK_DEPTH=60` blocked 13 entries worth **+$50.24** (Aug 19) and 12 worth **+$46.79** (Aug 20) — consistent ~$48/day. **Upper bound only**: the model assumes a fill at the candle ask and knows nothing about what a thin book does to the fill. `[EXEC]` now logs `depth` beside `avg_fill`, so this becomes measurable rather than speculative in ~2 weeks. ~~Best open lead.~~ |
| `[EXEC]` fill records now logged | Aug 21 | Every fill emits `side / scan / fresh / book / depth / book_age_ms / limit / contracts / cost / fee / avg_fill / attempts`. Compare `avg_fill` to `book` **by side, over distributions** — never per-fill against a candle (+0.85¢ artifact, §4). ~500 NO fills ≈ **12-16 days**. Harvest: `grep '\[EXEC\]'` over run logs. |
| MOM3 live rate is far below forecast | Aug 21 | Research predicted the veto bucket at ~8% of volume (8-10/day). First live day: **2/day** at m3>+0.50, 1/day at >+1.00, median m3 **−0.58** (n=62). At that rate 500 blocked trades is **~250 days**, not 6-8 weeks. If it holds a week, MOM3-as-veto is dead on timeline alone and only survives as a sizing input. |
| Loss cooldowns and size-up-after-loss | Aug 21 | Both refuted. Losses are **not** clustered: lag-1 lift +1.91pp, permutation **p=0.095**; lags 2/3/8 negative. Every cooldown loses money and the blocked trades were profitable (+$0.31 to +$0.56/tr). No post-loss edge either (+$0.16/tr, P(>0)=0.61), and sizing up after a loss would *loosen* the daily limit via `max(300, bet×4)`. `research/loss_cooldown/` |
| $200 daily-limit threshold, now measured | Aug 21 | The level the `bet×4` bug created is **worse than having no limit at all** (+$4,676 vs +$4,737); $300 is the best in the sweep (+$5,178). Retroactively confirms PR #134. Directional — the rolling-P&L sim approximates `daily_pnl` rather than reproducing it. |
| 94% WR with ~zero P&L is a SIZING artifact | Aug 19 | Win rate counts trades; P&L counts dollars. Bet size ran **$2.79 → $74 (26x)** inside the Aug 1-18 window, so most wins were banked when a win paid **$0.20** while the losses landed at $45-74. One $74 loss erases ~370 early wins. Dashboard showed 94.5% WR and **+$0.16/trade** against a +$1.00 backtest at flat $75. Now that sizing is flat $50 this distortion is gone — and it means the historical figure *understates* the edge. |
| Kalshi incentives: not worth pursuing | Aug 19 | Public endpoint `GET /trade-api/v2/incentive_programs` (filters `status`, `type`). 145,145 programs, $9.0M liquidity / $0.9M volume. **SOL/DOGE/BNB/XRP/HYPE/NEAR: never incentivized.** BTC/ETH 15M had volume programs that **ended 2026-05-12**, and zero volume programs are active exchange-wide. Even live they paid $20 pool ÷ 1.68M contracts = **$0.00001/contract** — the $0.005/contract cap never binds. Liquidity programs pay real money but the exploitable pattern is parking unfillable penny walls in dead markets, which risks the "abusive behavior and fake trading" clause. Full write-up + scripts: `research/kalshi_incentives/README.md`. |
| WTI paused; Gold/Silver still out — **Silver refined Aug 23, see top of table** | Aug 19 | All three launched **2026-07-31** (verified: 0 markets pre-July, 24 on Jul 31). WTI was added v5.8 on a 13-day backtest (+$1.75/tr OOS); over its whole life it measures **-$0.33/tr on 290 trades** — the justifying evidence inverted. Silver (+$0.31/tr) was *better* than WTI while excluded, so trading one and not the others was an accident of timing. Paused, not condemned: -$0.33 is ~1.1 SE from break-even. Revisit all three at ~1,000 trades each, together, one standard. |
| SOL is fine — August was noise | Aug 19 | Full archive: **1,810 trades, 93.15% WR, +$0.41/tr**. August alone reads -$0.12/tr. Picking any 18-day window makes some series look broken; this is exactly what Invariant 8 warns about. Do not act on single-window series stats. |
| Gold/Silver first real read | Aug 19 | 15 trading days, live gates, no slippage, no concurrency cap: **GOLD 357 trades 89.92% WR -$1.28/tr** (worst in the book), **SILVER 369 trades 92.95% WR +$0.31/tr**. Neither is established (~1.1-1.6 SE). Metals trade weekdays only — ~5/7 the days of crypto, so per-day comparisons mislead. Archived from 2026-08-01 onward. |
| Edge by price: dies at 95¢ | Aug 18 | 88¢ **+1.42pp**, 91¢ +0.98, 92¢ +0.70, 93¢ +0.66, 94¢ +0.95 (all significant); 95-96¢ **+0.07pp — gone**. n=83,337 obs / 6,402 clusters. Independent support for the `MIN_ASK=89` lead. `python3 scripts/calibration.py` |
| Entry timing: earlier is better | Aug 18 | Edge by time left: 100-150s **-1.26pp**, 150-240s -0.57, 360-480s **+1.27**, 480-600s +0.87. The 60s-average settlement is priced, possibly over-priced — late entries are not safer. `python3 scripts/calibration.py` |
| Waiting for a better price loses | Aug 18 | A 90-91¢ contract at 8-10 min is **gone from the 88-96¢ band 85.8% of the time** by 3-4 min. Buying what is *still* 90-93¢ late: -3.31pp, **-$2.71/trade** — adversely selected. Buying early: +$0.58/trade. `python3 scripts/entry_timing.py` |
| Survivor re-entry (92-93¢ → 94-96¢) | Aug 18 | +3.95pp in-sample, +4.88pp holdout — but 41 holdout obs with ~zero losses, so the rule-of-three floor (WR≥92.7%) sits **below** the 95.2¢ break-even. **Shadow-logged only** (`[SHADOW:SURVIVOR94]`), revisit at n≈500. |
| BRTI runs rich vs Coinbase | Aug 18 | Strike (a BRTI print) is **+0.96bp above** Coinbase at the same minute, sd 2.41bp, \|basis\|>10bp in 0.5% of windows. ~12% of a typical 15-min move — it biases every near-strike call the bot makes on Coinbase data. `python3 scripts/calibration.py` |
| Volume is no longer the constraint | Aug 18 | Cumulative counter ran 7 → 138 across Aug 18: **~140 trades/day live vs 121/day modelled**. The 27% capture rate in Invariant 6 is pre-v5.16 and stale; live now trades *more* than the backtest universe, at lower WR — suspect the marginal extra trades. |
| Config sweeps: nothing established | Aug 18 | `min_ask`=89 +$481 P=0.79 · `max_conc`=4 +$437 P=0.65 · `max_ask`=94 +$427 P=0.67 (at 0.105¢ slip) · `min_secs` flat. Four independent levers, none significant — consistent with Invariant 2 that the config is near-optimal. |
| 15M vs hourly parity — still untested | Aug 20 | Both settle on the identical BRTI print at the top of the hour, so the KXBTCD ladder interpolated at the 15M strike is a second price for the same event. They only coexist in the final ~10 min, so it needs a sampler firing at :50. **Trading the hourly ladder on its own merits is now refuted (row above); the parity/relative-value question is separate and still unmeasured.** |
| Crash fills are **+EV so far**, not the leak | Aug 18 | 12 fills landed below the band on Aug 18 and settled **11W/1L, +$90.63** (avg +$7.55/trade vs the ~$6.50 target). The two deep ones netted -$6.48 (-$47.48 DOGE @57.6¢, +$41.00 BTC @47¢). Cheaper entry pays more when it wins. Do **not** auto-exit them on instinct; n=12. |
| ~~The actual leak is the core~~ — Aug 18 was just a bad day | Aug 20 | **Downgraded from "unexplained leak".** v5.16's 120/135 = 88.9% WR / -$236.20 read as edge decay. It was not: the archive says **Aug 18 was the single worst day in 68 days** — the modelled universe at live gates lost **-$338 (-$2.01/tr, 88.69% WR)** that day, and live actually *beat* it (-$7 on 139 trades). Nothing to investigate. `python3 scripts/backtest.py --since 2026-08-18 --until 2026-08-18` |
| Adverse spot momentum predicts losses | Aug 20 | **Best-supported edge lead in the file; shadow-logged as `[SHADOW:MOM3]` on 2026-08-20, not gated.** Spot drifting toward the strike in the 3 min before entry: `m3 = -sign*ln(S/S₋₃ₘ)/(σ√3)`, σ = sd of trailing 60 one-min returns. Blocked bucket (m3 > +0.50) ran **-$1.56/tr on n=569**, difference vs kept **CI [-3.95, -0.75], P(worse)=1.000**. Pre-registered, monotone in both windows, 5 of 6 series, both sides, all 3 months. Worth **+$10-14/day** at $50 flat (~+12%), blocking ~8% of volume. Critically it helps **more** at one tick of slippage, so it is not a fill artifact. m3 > +0.25 **fails OOS** — do not over-tighten. `python3 research/perp_overlay/s1_robustness.py` |
| Hourly crypto ladders (KXBTCD/KXETHD) — no edge | Aug 20 | **Refuted.** 45-day archive, live gates, $50 flat: **1,641 trades, -$0.03/tr, -$41** (-$129 at the measured 0.105¢ gap, -$868 at one tick), vs the 15M book at **+$85.90/day** over the same window. Win rate straddles the ~92.3% break-even and **flips sign between halves for both series** (BTCD -0.57→+0.35, ETHD -1.34→+1.15). The multi-strike "leverage" worry that kept it in shadow was **backwards**: 310 of 392 stacked closes are a YES below spot + a NO above spot, which cannot both lose — 0 all-lose events in 45 days. Real finding: **100% of hourly entries settle on the same BRTI print as the :00 15M close on the same underlying**, and `MAX_CONCURRENT` does not see them as related. `python3 research/hourly_crypto/analyze_hourly.py` |
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

*Perp hedging (pre-registered, 2026-08-20):* **dead, and it fails at ZERO fees.** Hedging
each position with an opposing perp was tested at fixed notional, at the digital's true
per-trade delta (`C·φ(z)/(σ√τ)`, median **$7,842** per $50 bet), netted to one BTC leg per
settlement cluster, and dynamically on an adverse strike crossing. Best case — correct
delta sizing, **0bp** — earns **+$0.32/tr against a +$0.52 baseline** and cuts return/sd
from 2.70% to 1.82%: worse on both axes. A perp's expected return is zero, so the overlay
can only reshape variance and pay fees. The Kelly escape (cut variance, size up) fails by
~100x: the 8.3% sd cut buys a 1.19x size increase worth **+$0.098/trade**, needing a
round-trip cost under **~0.1bp** against a real 4-12bp. Cluster-netting does not rescue it
— only **4%** of notional cancels, because concurrent positions nearly always point the
same way. **The position is already collateralised and loss-capped at the premium; there
is no tail to hedge.** Signal tests on the same spot data: H1 distance-to-strike, H3
vol regime, H4 cross-asset BTC, H9 slot ranking, H10 replacing the prior gate — all
refuted OOS. Only H2 (adverse momentum) survived; see §7. `research/perp_overlay/`

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
12. All timestamps ET (America/New_York). Archive filenames are UTC days. Report ET to
    Chris always — never make him convert UTC.
13. When results disagree with this file, **this file is probably the wrong one.**
14. **Update this file as you go, not at the end of a session.** Chris should never
    have to ask for it. Write the row the moment a result lands — a session can end
    without warning and an unrecorded finding is a finding that has to be re-derived
    from scratch. Update immediately when any of these happen:
    - a hypothesis is confirmed **or refuted** (refutations matter more — they stop
      the idea being re-proposed; §8 exists because that kept happening)
    - anything merges that touches `late_certainty_trader.py`
    - a number already in this file turns out to be wrong (fix it in place, strike
      through the old claim, say what replaced it — see Invariant 1)
    - a shadow experiment starts, or its expected timeline changes materially
    - an operational failure and its cause (outage, missed archive, halt bug)
    Batch trivia; never batch a finding. §10 is the running state — refresh it before
    the session ends so the next one starts where this one stopped.
15. **Response style.** See §Who/What. Terse, tables, numbers first, ~90% shorter than
    feels natural. What changed → what it means → what needs his call.

---

# 10. Running state — read this first, refresh it last

**Last updated: 2026-08-22 ~16:10 ET.** If this is more than a few days stale, verify
everything in it before relying on it.

## Where the account is

Balance **$1,177.13** at 11:01 ET Aug 21, down from ~$1,285 overnight — Aug 21 ran
51W/8L on 59 settled trades, **−$185.91**. Since Aug 1 the account is roughly flat.
Raise the bet to $75 only at balance ≥ $1,630. **Do not scale on the current realised
edge.** Headroom to the $650 stop is ~10 losses at $50 flat.

**The power numbers that govern every "is it working" question** (measured, sd =
$18.13/trade, cluster design effect 1.066, 95% conf / 80% power):

| test | detects | trades | days @90/d |
|---|---|---|---|
| win rate 94.0% vs 92.0% | 2.0pp | 1,360 | 15 |
| win rate 93.5% vs 92.0% | 1.5pp | 2,418 | **27** |
| P&L +$0.61/tr vs $0 | dollars | 7,383 | 82 |
| P&L at perfectly flat $50 | +$0.349/tr | 13,568 | 151 |

**Never judge this strategy on P&L over a short window** — it needs 3-5 months. Win
rate is the same question with ~3x the power, but only if fill quality is pinned
separately, which makes the `[EXEC]` monitor a prerequisite rather than a nicety. The
"~1,300 trades" figure in the kill-list is significance-only with no power term; the
honest number for 1.5pp is 2,418.

## Overfitting — settled, do not re-litigate

All **14** gate configurations recoverable from git history were replayed against the
archive at flat $50. **Every one is positive**, full archive and holdout, +$7 to
+$78/day. The edge does not depend on the parameter choices. v5.16 ties for best gross
(+$78/day), is best on holdout (+$138/day), and is one of the few that survives one
tick of slippage (+$13/day) where half the field goes negative — `max_conc=2` is what
buys that. The two highest-WR configs (96.6%, 96.4%) make the *least* money, +$17 and
+$7/day: win rate is not edge.

Replay versions as a **plateau check, never a leaderboard.** The apparent per-trade
leader (s240/yo1/mc6, +$0.79/tr) makes *less* money than v5.16 because it takes 36%
fewer trades, and the bootstrap cannot separate them: delta −$986, CI [−3700, +1828],
P(better)=0.212. Picking the max of 14 correlated estimates inflates the winner by
~1.7 SE, which here is most of the entire edge.

## What is live and healthy

v5.16, config unchanged for two weeks: ask 90-93¢, 150-600s, prior≥75×2, both sides,
max 2 concurrent, $50 flat, stop $650, daily limit $300. Runs land every ~4 min.
Nothing on Aug 21 changed a trading decision — every trader PR was logging only.

**Archive alerts can cry wolf.** A manual dispatch racing the cron on Aug 21 produced
two runs 43s apart; both archived Aug 20, the loser died in a binary rebase its retry
loop could not clear, and it sent `ARCHIVE FAILED`. No data was lost. Fixed with a
concurrency group and a push-first retry (#145, verified by firing two dispatches back
to back). **Before acting on that email, check whether the day is actually on main.**

## Execution work, 2026-08-22 — capture, not strategy

Nothing below changed a gate, threshold or bet-selection rule. All of it changes WHEN
and WHICH-OF the bot looks, plus what is measured.

| # | change | why |
|---|---|---|
| #152 | scan phase-locked to `:01,:16,:31,:46` | criteria are defined at 1-min candle CLOSES (every archived `secs_left` is a multiple of 60). Free-running at 15s put only ~1 look in 4 where the signal exists. Model-wanted fills sat within 10s of a boundary 43.8% of the time vs 20.9% for model-rejected ones — capture AND junk were one phase error |
| #153/#154 | slot allocation deterministic, tie-broken by `md5(cluster,series)` | was `random.sample(SERIES_LIST)` traded on sight, so the 2 slots went to whichever series the shuffle hit first while the harness takes the 2 with most time left. #153 alone was a BUG: all series close simultaneously, so `_secs_left` ties and a stable sort handed every slot to BTC+ETH — the two worst performers since Aug 5. Hash tie-break measures 16.2-17.1% of slots per series against a 16.67% fair share |
| #155/#158 | book depth logged; reason-coded funnel | `backtest.py` cannot model the depth check (its own docstring says so), so every capture % measured against the harness counted entries that were never executable. **Capture was an upper bound, not a miss rate.** Funnel now splits observation / gates / depth / concurrency / order stages, which have opposite fixes |
| #156 | concurrent series discovery | sequential discovery took **3.85s** across 6 series, so the 2 slots were allocated from 6 different moments. Only matters because #153 started ranking candidates against each other |
| #157 | jobs 240s -> 900s, `timeout-minutes: 20` | duty cycle is scan-time/cycle-time and the ~15s overhead is paid once per job: **94.1% -> 98.4%**, and cheaper in Actions minutes |
| #151 | bet $50 -> $25 | survival: $329 above the $650 stop was 6.6 losses; now 13.2 |

**Measured, and worth not re-deriving:**
- Duty cycle 94.4% (pre-#157), median blind gap 13s. Gaps land **uniformly** across the
  minute — boundary capture 94.2% vs 94.4% wall-clock, so downtime is not
  boundary-biased. A plausible worry, checked and false.
- Only **50% of wall-clock has any tradeable market**: eligible window is
  close-600s..close-150s = 450s, and closes are 900s apart. Idle scans are structural.
- Slot-cap exclusions are worth only **$1.29/day**. `max_conc=2` is not the constraint.
- True misses (nothing in the way) were worth **+$19.59/day** over Aug 12-21.

## External audit, 2026-08-22 — every checkable claim reproduced

An independent audit found defects that invalidate numbers recorded earlier in this
file. Fixed in #161-#166. **Treat any capture or fill-quality figure written before
2026-08-22 as unreliable.**

| # | defect | why it mattered |
|---|---|---|
| #161 | `reconcile.py` filtered fills on `action == "buy"` | Kalshi books an opening NO as `action="sell", side="no"`. 117 of 200 recent fills are NO, so **every fill-quality figure was YES-only**. "0.239c better than modelled" -> **-0.193c on n=231** |
| #161 | `MIN_BOOK_DEPTH=60` was a constant | calibrated at $75 (~81 contracts). At $25 (26 contracts) it had become 2.3x — a gate that *tightened* every time the bet was cut. Now 1.5x the order |
| #162 | entry gate ran on the `/markets` listing quote | refetched only once the listing was already in band. Median listing-vs-real gap 1.8c, band membership disagreed 7 times in 96. Markets whose listing read 94c but whose real ask was 92c were **structurally invisible**. Now a pre-filter widened 3c; true band still enforced on the refetch |
| #162 | unreadable book failed **open** | ordered with neither depth nor last-look verified — the exact failure behind the 47c/57c/83c fills on Aug 18. Now fails closed when both are None |
| #163 | archive rounded sub-cent to integer cents | a real 93.30c ask became a false 93c candidate **inside** [90,93]. 18.4% of identities changed. **This corrupted the denominator of every capture figure ever quoted here.** Now exact `Decimal`; old integer files still parse through `float()` |
| #164 | heat check ran before price eligibility | every market in the window logged a heat skip. The claim "heat check is the dominant blocker, 16 vs 1 trade" was an artifact of ordering |
| #164 | gate log deduped per market lifetime | markets entering the band later produced no row. Now one row per `(ticker, side, candle)` |
| #165 | daemon swallowed every cycle error | a fully broken daemon exited 0 and Actions reported green. Now fails on 10 consecutive or >34% failed cycles |
| #166 | ambiguous order POST assumed failure | a timed-out POST may have been ACCEPTED, leaving a live untracked order. Now reconciled by `client_order_id`, three-valued: found / absent / **lookup failed -> halt** |

**Method note worth keeping:** the depth gate was dismissed earlier that day on
*frequency* (2 skips in 14 runs) without pricing what it blocked. Frequency was the
wrong measure.

## Why a 93.7% win rate still loses money — the decomposition to reach for first

The monitor reports a TRADE-weighted win rate. P&L experiences a DOLLAR-weighted one,
and break-even moves with the price paid. Since Aug 1:

| | value |
|---|---|
| trade-weighted WR (what the monitor shows) | 93.56% |
| **dollar-weighted WR (what P&L feels)** | **92.35%** |
| blended break-even for the actual price/size mix | 92.50% |
| **margin** | **-0.15pp** -> **-$149.62** |

Average winner risked $37.32, average loser $44.93 — ratio **1.204**. Nothing selects
bigger bets for losses; the bet ramped over the window and losses landed later. That
alone drags the effective rate 1.2pp, which is most of the edge.

`python3 -c` over settlements: dollar-weighted WR = sum(cost of winners)/sum(all cost);
blended break-even = D/(D+U) where U is total upside if every trade won.

**Never compare a raw win rate to a flat 92%.** Since Aug 1 you risked $59,320 to win
$4,812 — $12.30 per $1, so one loss erases 12.3 wins and a 1.2pp weighting gap is not
a rounding error. This decomposition only holds inside one price regime; it breaks on
all-time, which mixes v5.16 with the pre-Aug-5 band.

## The measurement that decides whether 2026-08-22's work helped

2026-08-22 spans six code versions and is unmeasurable. The first clean day is
**2026-08-23**, archived 2026-08-24 03:30 UTC. Run then:

```bash
python3 scripts/reconcile.py --since 2026-08-23 --until 2026-08-23
python3 scripts/gate_replay.py --since 2026-08-23
```

**Pre-registered, and it is falsifiable:** capture should rise AND model-rejected
(EXTRA) trades should fall, *together*. That is the same prediction for both, because
they were one phase error. **If capture rises while EXTRA stays flat, the
mid-candle-noise explanation is wrong and #152 should be reverted.** Do not rescue it
with a new story.

Last measured capture was 44.6% (Aug 18-21) against a denominator since proven wrong
by the sub-cent rounding — treat it as a floor, not a baseline.

## WebSocket — specced and validated, deliberately not wired in

`docs/websocket_spec.md` carries the client contract plus an appendix of what was
verified against production. Delta reconstruction is confirmed: **14 consecutive exact
full-book matches** over 140s against `/markets/{ticker}/orderbook`.

Working artifacts live outside the repo at
`~/Documents/Codex/2026-08-20/i/outputs/` — `kalshi_orderbook_reconstructor.py`
(fail-closed, multi-market) and `kalshi_ws_orderbook_validator.py` (`--self-test`, or a
ticker plus `--duration`).

Two things that cost a session to learn: **`seq` is contiguous per `sid`, not per
ticker**, and **`new < 0` must GAP rather than pop the level**. One open question —
Kalshi sent a second snapshot on a live subscription unasked; likely market rollover,
unconfirmed. Probe a market away from close before running a client.

**Do not wire it in before the 2026-08-23 measurement.** If the residual loss turns out
to be depth exclusions or the concurrency cap, a streaming feed fixes nothing.

**Do not chase 100% capture as a headline number.** Three of the funnel's exclusion
categories — concurrency cap, book depth, already-holding — are the risk policy working
and must never go to zero. Only observation misses and fetch failures are defects.

## Collecting right now — do not disturb

| Experiment | Started | Decides at | Watch for |
|---|---|---|---|
| `[SHADOW:MOM3]` adverse momentum | Aug 21 00:04 ET (data before that is invalid — partial-candle bug) | ~500 blocked trades | Live rate is 2/day vs 8-10 forecast. Re-check after a week; if it holds, dead on timeline |
| `[EXEC]` fill quality by side | Aug 21 ~00:15 ET | ~500 NO fills, 12-16 days | `avg_fill` vs `book` by side; also prices the thin-book gate via `depth` |
| `[QUOTE-DRIFT]` listing vs fresh ask | Aug 22 | ~1 week | Every band disagreement, marked RECOVERED or correctly-skipped. Two RECOVERED in the first 15-min job (ETH listing 89c, real ask 90.0c and 91.8c). Tunes LISTING_QUOTE_TOLERANCE=3c on our data instead of the audit's sample |
| `[SHADOW:GATE]` poll-level gate inputs | Aug 21 12:09 ET | ~2 weeks | Scores ANY version on what the bot actually saw, unlike archive replay which is an upper bound. Ask is logged as a **float** — Kalshi quotes sub-cent (96.6000c seen live); any parser must accept decimals. `scripts/gate_replay.py` |

## Next actions, in order

1. **Wait.** The edge is 2pp; nothing is measurable on a shorter horizon than the two
   experiments above. Resist config changes — each one is now an unmeasurable coin flip.
2. Re-check the MOM3 blocked-trade rate after ~7 days.
3. Once `[EXEC]` has ~500 NO fills: measure fill quality by side, then price the
   thin-book gate (best open lead, ~$48/day upper bound).
4. Standing leads, unchanged: `MIN_ASK=89`, time-weighted sizing (exploratory only —
   needs 4-9 months, see below).

## Open threads and loose ends

- **Time-weighted sizing** — +15.6% OOS but CIs include zero, the seven weight
  functions are ~1.26 effective dimensions, and it needs **122-279 days** of fresh data.
  Exploratory. The free paired shadow test (log weighted−flat per cluster) has not been
  started. `research/top5/`
- **Live-vs-model gap** — Aug 12-21 live trailed the model by 1.37pp, but Aug 20 alone
  *beat* it (+$1.27 vs +$0.62). The gap may be an artifact of a window containing two
  known-broken days (Aug 17 outage, Aug 19 halt bug), both since fixed. Strip those and
  re-measure before treating it as real.
- **Monitoring hole** — `if: failure()` cannot catch a workflow that never *runs*. The
  archive silently skipped its cron on Aug 21 and had to be triggered by hand. The
  `daily_summary` staleness line is the only backstop and it fires ~22h late.
- `stash@{0}` ("auto-stash diag") — `.claude-flow` churn plus the always-empty
  `certainty_state.json`. Safe to drop; left alone pending Chris's call.
- Commit `9fdf8722` was pushed **directly to main**, bypassing the PR flow (rule 8).
  Research-only, revertable, disclosed.

## Working with Chris

Terse, tables, numbers first — see §Who/What and rule 15. He checks in often and reacts
to daily P&L; the honest answer is almost always "that is noise, here is the horizon at
which it stops being noise." He is right to push back, and did tonight: challenging a
claim of mine is what surfaced the Invariant 1 error. **Verify before reassuring.**
