# Kalshi Trading — Auto-Context

> **AUDIT AND FREEZE IN FORCE (from 2026-08-24).** Read `docs/audit/CHARTER.md`
> BEFORE doing anything in this repo. The trading path is READ-ONLY and **nothing
> live changes without Chris's explicit approval** — if you find a live bug, flag it
> and stop, do not fix it. Two agents (Claude Code, Codex) are auditing in parallel;
> each writes only to its own `docs/audit/<agent>/` directory and must not read the
> other's until the Session 2 diff. The bot keeps trading throughout.

Claude Code reads this file automatically when opened in `/Users/chrisgarceau/pm/`.

**How to read this file.** Three parts, by what you need.

- **PART I — OPERATING.** What is live right now. Short. Read this one.
- **PART II — EVIDENCE.** Why it is that way. Long. Every claim carries a command.
- **PART III — GRAVEYARD.** Refuted. Do not re-propose these.

Where PART I and PART II disagree, **PART I wins** — it is derived from the code,
and `python3 scripts/verify.py` re-derives it against the live API on demand.

---

# PART I — OPERATING

*What is live. If you read one section, read this one. Everything here was verified
against the code and the API on 2026-08-24 by two independent audits
(`docs/audit/`).*

## Current strategy — v5.17 + z-gate (2026-08-27)

Buy **either side** at ask [90,93]¢ with 150-600s left, provided the prior 2 same-side
1-min candles are all ≥75¢. If ask ≤91¢, also requires a 3rd prior candle ≥80¢. Hold
to settlement.

| Parameter | Value | Re-check |
|---|---|---|
| `FLAT_BET_DOLLARS` | **50** | 50->25 in #151 (2026-08-22); 25->35 on 2026-08-28 and **35->50 on 2026-09-01, both OVERRIDES of that row's own "not on a good week" bar** — see PART II. Two overrides of the same clause in five days; a third means deleting the clause, not overriding it |
| `MIN_ASK_CENTS` / `MAX_ASK_CENTS` | 90 / 93 | **YES reaches to 88c via `LOW_BAND_MIN_CENTS` (v5.17); NO does not** |
| `MIN_SECS_LEFT` / `MAX_SECS_LEFT` | 150 / 600 | `--sweep min_secs 100 150 200 250` |
| `PRIOR_MIN_CENTS` / `PRIOR_LOOKBACK` | 75 / 2 | `--sweep prior_min 70 75 80 85` |
| `YES_ONLY` | **False** | `--compare yes_only=1` |
| `MAX_CONCURRENT_POSITIONS` | 2 | `--sweep max_conc 1 2 3 4 --slip 0.105` |
| book depth | **80** = `max(ceil(contracts x 1.5), 25)` at $50 | dynamic since #166; the legacy `MIN_BOOK_DEPTH=60` is no longer a gate. Not in harness (needs live book). **56 -> 80 on 2026-09-01 purely from the bet change — a 43% tighter liquidity requirement that nobody chose. Watch fill rate: this is the one self-rescaling constant that can COST volume, and the z-gate needs volume to reach n>=200** |
| `CRASH_FILL_TOLERANCE` | 3 | fills this far under the band are logged, not emailed |
| `SURVIVOR_*` | shadow | logs 92-93¢→94-96¢ survivors; trades nothing |
| `STOP_BALANCE` | **400** | emergency floor. **Now 1.45x worst drawdown, not 1.5x** — 32.0 losses at $50 on a ~$2,000 balance, recovered from 1.32x at $35/$1,405 because the bankroll grew faster than the bet. Exactly compliant would be a $48.48 bet. Growth was **P&L, not a deposit** (confirmed 2026-09-01), so the revisit trigger did not fire and 400 stands; the headroom recovery is earned, not new money. Note the floor is now 20% of balance vs the ~33% it was set at — designed behaviour under a growing account, but a progressively weaker brake. Re-check the RATIO on any deposit/withdrawal |
| `CONSEC_LOSS_LIMIT` | 9 | never fires in 68d either way |
| trailing-24h limit | **bet x 20** ($1,000 at $50) | emergency brake; denominated in bets so a sizing change cannot disarm it. Held at 50% of balance across the $35 and $50 changes — unchanged ratio, not a new loosening |
| poll cadence | **900s job / 15s interval** | ~52 scans per CI job (measured, run 32776379439) |
| `EDGE_DEGRADE_THRESHOLD` | 0.84 | catastrophic breaker only |
| `BLACKOUT_HOURS` | `set()` | — |
| **`Z_GATE_ENABLED` / `Z_GATE_MIN`** | **True / 0.761** | **LIVE 2026-08-27.** Skips signals whose cushion is small vs remaining vol. Fails open. Revert = `Z_GATE_ENABLED=False`. Pre-registration + revert rule in PART II; monitor with `scripts/zgate_monitor.py` |

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


## Operational

**Key files**

| File | Purpose |
|---|---|
| `late_certainty_trader.py` | THE live trader — surgical edits only (real money) |
| `scripts/backtest.py` | Canonical harness — every claim goes through this |
| `scripts/archive_candles.py` | Nightly archival (§6) |
| `data/candles/YYYY-MM-DD.csv.gz` | Canonical dataset, ~13 KB/day, from 2026-06-11 |
| `daily_summary.py` | Ground-truth P&L from settlements API (never trust state P&L) |
| `kalshi_auth.py` | RSA-PSS-SHA256 auth + `cancel_order()` + shard routing (`AUTO_ROUTE_EXCHANGE`) |
| `kalshi_dashboard.py` | Render dashboard entrypoint |
| `test_order_safety.py` | Order-safety + NO-path regression tests |
| `scripts/missed_pnl.py` | Prices what a halt cost — replays live gates on public candles |
| `scripts/reconcile.py` | Splits the live-vs-model gap into capture / selection / execution |
| `scripts/gate_replay.py` | Scores any version on what the bot ACTUALLY SAW (not an upper bound) |
| `scripts/calibration.py` · `scripts/entry_timing.py` | Edge by price / by time left |
| `archive/research/refuted/kalshi_incentives/` | Incentive-program investigation (see §8) |

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


## Rules

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


## Running state

**Last verified 2026-08-24** against the code and the live API by two independent
audits. `python3 scripts/verify.py` re-derives all of it and names any two sources
that disagree — run that rather than trusting this text.

### What is live

v5.17: ask **YES 88-93c / NO 90-93c**, 150-600s, prior >=75c x2, `<=91c` needs a 3rd
prior >=80c, both sides, max 2 concurrent, **$25 flat**, stop **$400**, daily limit **bet x 20** ($500),
no blackout hours. Six crypto 15M series; WTI/GOLD/SILVER are shadow-only. Jobs run
900s at a 15s interval and land every ~15 min.

**The 88-89c YES band began trading 2026-08-24** and not before — it shipped inert on
2026-08-24 and was fixed the same day (PART II, audit findings). Its pre-registration
clock starts from that date: 200 88-89c YES trades, revert if that subset wins below
88.5%, no early reads.

### Exchange sharding — every order needs `exchange_index` (2026-08-25)

Kalshi sharded the exchange on **2026-08-24 12:00 ET**: new crypto events are created
on **shard 2**, tennis/baseball on shard 3, everything else stays on shard 0.
`exchange_index` defaults to **0 server-side when the field is absent**, so once the
pre-cutover events aged out, every order hit the wrong shard and came back **HTTP 404**.

**The outage: 00:05-10:40 ET on 2026-08-25, 162 order attempts, 162 rejected, 0 filled.**
Reads never broke — GETs auto-route, writes do not — so `balance`, `/markets` and
candlesticks all looked healthy and the gates kept selecting normally. The only symptom
was `order attempt 1 did not confirm (HTTP 404)` followed by `confirmed absent`.

`place_order()` and `cancel_order()` now send `exchange_index=-1` (route by ticker),
which stays correct on any shard and through any future re-shard. **Cancel matters as
much as create**: `cancel_order` is a DELETE with no body, takes `exchange_index` and
`market_ticker` as QUERY params, and the caller treats 404 as "already gone" — so an
unrouted cancel would silently leave a live GTC order resting.

**Second requirement — collateral is per-shard.** Kalshi returns `400 user_not_found`
for an order on a shard the account has no funds on: "programmatic traders must
preallocate collateral on a given exchange shard before order placement"
(`POST /portfolio/intra_exchange_instance_transfer`; amount in **centicents**,
measured at **10,000 per dollar**, not the 100 one doc page implies). **$400 was moved
to shard 2 on 2026-08-25**; the account total is unchanged and the funds are
recoverable by swapping the shard indexes.

Caught live inside run 32864270828: 11:20:25 and 11:21:00 returned `400`, the transfer
landed, and 11:21:54 and 11:24:34 were accepted — same code, same run.

**`fetch_balance()` returns `(total, shard)` and the two are not interchangeable.**
`STOP_BALANCE` is an account-destruction brake calibrated against the whole account,
so it reads the TOTAL — pointing it at the shard balance would halt the bot instantly
whenever the shard holds exactly `STOP_BALANCE` ($400 <= $400). Order collateral is
per-shard, so `check_halts` gets a separate, NON-sticky guard at
`MIN_SHARD_COLLATERAL_BETS x bet`. Without it a drained shard does not halt — orders
are simply rejected, which moves no metric except the fill count. A missing
`balance_breakdown` (subaccount-restricted key) fails OPEN to the total.

### Alerting — halts and a no-fill heartbeat (2026-08-25)

Risk halts used to be a log line and nothing else: `send_email` fired only for
order-safety EXECUTION HALTs inside `try_trade`, so a `check_halts` stop, a drained
shard, an edge-degrade halt or a consec-loss cooldown was invisible until someone ran
`kstat`. Both alerts go to `COPY_EMAIL_TO`, already wired into the trader step.

**Halt alert.** Fires on the halt TRANSITION, keyed by category, and re-sends every
`HALT_ALERT_REPEAT_SECONDS` (6h) while it stands. Dedup compares the KEY, never the
reason text — reasons embed live counters ("cooldown 43min") that change every cycle.
This matters: ~54 scans per job and a job every ~15 min means a naive per-cycle alert
is ~96 emails an hour, which gets muted within a day and is worse than no alert.
Recovery clears the key, so a recurrence pages immediately.

**No-fill heartbeat.** `NO_FILL_ALERT_SECONDS` = 3h. **This is the one that would have
caught 2026-08-25** — that outage produced NO halt at all, because orders were rejected
downstream, so halt alerting alone would have missed it entirely. The threshold is
measured, not chosen: across 500 fills (08-20..24) the median gap is 11.9 min, p99 is
59.9 min, and the largest gap ever observed is **105.1 min**. 3h is ~1.7x the worst real
quiet spell. Crypto 15M runs continuously, so no market-hours gating is needed.

`state["last_fill_ts"]` is stamped on both fill paths (main and longshot).

### Account P&L — quote a scope, never "lifetime"

No source here can produce a lifetime figure (settlements retain ~30 days, state caps
at 500 positions, `stats` resets on every version bump). Over the API window:

| scope | n | P&L |
|---|---|---|
| live series (6) | 1,946 | **-$320.91** |
| + retired 15M — what the dashboard counts | 2,253 | -$565.96 |
| + non-15M (`KXMLBTOTAL` is -$863.75 on 4 trades) | 2,258 | -$1,445.00 |

Bet sizing history, which any per-trade figure must respect: **$75 through Aug 18,
$50 Aug 19-21, transition Aug 22, $25 from Aug 23.** A $/trade number spanning
2026-08-22 is averaged across two bet sizes and is not an edge.

`kstat` prints this automatically as of 2026-08-25 — the `strategy` row is the six
live series from the settlements API, stamped with its own date span. It previously
printed `state["stats"]` under the label **lifetime**, which is the counter that resets
on a version bump: on 2026-08-25 it read `61 tr · +42.20` against a real record of
2,036 trades at -$212.37. The state counter is still shown when it disagrees, but only
ever labelled `since reset`. The row is computed outside the artifact-download block,
so it survives a GitHub blob failure that takes out the rest of the tool.

Raise the bet only on the ratio rule in PART II, never on a good week.

### Risk controls — emergency brakes, deliberately not tight

Both halts were re-sized on 2026-08-24 after the audit found each sitting AT or BELOW
normal variance, i.e. each would have fired during a drawdown the strategy has already
survived. That is the expensive failure: a tight limit blocks exactly the trades that
recover the stretch.

| control | value | headroom vs worst measured |
|---|---|---|
| `STOP_BALANCE` | **$400** | 33 losses vs a $543.79 worst drawdown = **1.5x** |
| trailing-24h limit | **bet x 20** = $500 at $25 | vs a -$309.25 worst 24h = **1.6x** |

**The limit is denominated in BETS, not dollars, and that is the point.** The worst
rolling-24h window is 21 losses at ANY bet size — loss count is set by win rate and
volume, not sizing. So a fixed dollar threshold is a different control at every size:
`max(300, bet*4)` was validated at $75 and, at $25, ended up nine dollars past the
worst 24h in the whole archive, firing once in 74 days and blocking nothing. Exact
mirror of the `MIN_BOOK_DEPTH` defect, where a fixed CONTRACT count silently tightened
as the bet fell. **Fixed constants do not survive a sizing change; ratios do.**

`STOP_BALANCE` is the exception and is still a constant, because it is a floor on the
account rather than on a trade. It is a ratio to the balance dressed as a constant —
**revisit it on any large deposit or withdrawal.** Set against a $1,227.48 balance.

Pinned by `EmergencyBrakeTests`, which assert the PROPERTY (clears the worst measured
day at every bet size, scales with the bet, leaves room for a normal drawdown) rather
than the numbers, so the next sizing change cannot silently disarm them.
Re-derive: `python3 docs/audit/claude/replay_loss_limit.py`.

### Still unexamined, deliberately

The edge breaker's 50/0.84, `ORDER_TTL_SECONDS`, `ORDER_RECONCILE_SECONDS`,
`ORDER_MIN_TOPUP_DOLLARS` and the 500-position state cap have no value-specific
evidence. None has caused a known problem; none has been tested either.

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


**Archive alerts can cry wolf.** A manual dispatch racing the cron on Aug 21 produced
two runs 43s apart; both archived Aug 20, the loser died in a binary rebase its retry
loop could not clear, and it sent `ARCHIVE FAILED`. No data was lost. Fixed with a
concurrency group and a push-first retry (#145, verified by firing two dispatches back
to back). **Before acting on that email, check whether the day is actually on main.**

## Collecting right now — do not disturb

| Experiment | Started | Decides at | Watch for |
|---|---|---|---|
| `[SHADOW:MOM3]` adverse momentum | Aug 21 00:04 ET (data before that is invalid — partial-candle bug) | ~500 blocked trades | Live rate is 2/day vs 8-10 forecast. Re-check after a week; if it holds, dead on timeline |
| `[EXEC]` fill quality by side | Aug 21 ~00:15 ET | ~500 NO fills, 12-16 days | `avg_fill` vs `book` by side; also prices the thin-book gate via `depth` |
| `[QUOTE-DRIFT]` listing vs fresh ask | Aug 22 | ~1 week | Every band disagreement, marked RECOVERED or correctly-skipped. Two RECOVERED in the first 15-min job (ETH listing 89c, real ask 90.0c and 91.8c). Tunes LISTING_QUOTE_TOLERANCE=3c on our data instead of the audit's sample |
| `[SHADOW:GATE]` poll-level gate inputs | Aug 21 12:09 ET | ~2 weeks | Scores ANY version on what the bot actually saw, unlike archive replay which is an upper bound. Ask is logged as a **float** — Kalshi quotes sub-cent (96.6000c seen live); any parser must accept decimals. `scripts/gate_replay.py` |
| `[SHADOW:Z]` vol-normalised distance to strike | **Aug 27 01:22 ET — CONFIRMED EMITTING** (run 33042315329, 3 lines, arithmetic hand-checked to 3dp on all three) | 1,000 in-band signals AND 400 clusters, ~5-8d | Gates NOTHING. Validates that the live Coinbase feed reproduces the backtested z against RTI settlement. Bottom-quintile WR below 90.5% AND top-bottom CI excluding zero are BOTH required to proceed. **DEDUP ON `(ticker, side)` WHEN READING, keeping the earliest** — `_Z_SEEN` is module-level in a short-lived process, so a market whose 150-600s window straddles two 900s jobs logs TWICE. `[SHADOW:MOM3]` has the same property. Counting raw lines against the 1,000 horizon overstates progress and would end Phase 1 early |

## Next actions, in order

*Rewritten 2026-08-24 after the audit. Items 3 and 4 of the old list are DONE — NO-side
fill quality is measured (they are indistinguishable from YES) and the thin-book gate
was priced and refuted.*

1. **Wait, and do not change config.** The edge is ~2pp against break-even; nothing is
   measurable on a shorter horizon than the experiments below. Every config change
   during a measurement window is an unmeasurable coin flip.
2. **Let the v5.17 clock run.** 200 88-89c YES trades from 2026-08-24. Do not read the
   subset early — that is the optional-stopping error #152 died of.
3. ~~Decide the two unexamined risk controls.~~ **DONE 2026-08-24** — both re-sized as
   emergency brakes (PART I, risk controls). Watch for the first time either fires:
   neither has fired on archive data, so the first live trip is information.
4. **Re-check MOM3's blocked-trade rate.** Live incidence ran ~2/day against an 8-10
   forecast; if that holds it is dead on timeline regardless of effect size.
5. **Start `[SHADOW:Z]` Phase 1** (pre-registered Aug 27, dated observations). Shadow
   only — it gates nothing and trades nothing, so it is compatible with item 1's
   freeze. It is cheap: `_spot_momentum()` already computes and caches the 60-min
   sigma, so the added cost is the strike and the spot level from a call already
   being made. Reconcile the sigma definition (RMS vs mean-centred sd) BEFORE
   logging, or live and modelled z measure different quantities.
6. Standing leads, unchanged and NOT proposals: `MIN_ASK=89`, the p3 inversion above
   91c, time-weighted sizing (exploratory, needs 4-9 months).

## Strategy-search criteria — rewritten 2026-08-25

*The old criteria were never written down; they were inherited from late-certainty's
shape and they made the search look exhaustive when it was narrow. Two searches ended
in "nothing survived" while testing one mechanism many ways. These replace them.*

**The criterion nobody had stated, and the one that was actually binding:**

> **A candidate must be KNOWABLE inside ~8 weeks.** Not profitable — knowable.

Late-certainty needs 2,418 trades / 27 days to resolve 1.5pp. The maker idea needed
75 days. The calm cell needs 10 weeks. Retention is 67 days. **A strategy that needs
longer than the data window to confirm can never be confirmed**, however good it
looks, so this is a screen to apply BEFORE testing, not a result to discover after.
It selects for many independent close clusters and low per-trade variance, and it
rules out long-dated markets on arithmetic — a daily series yields ONE independent
observation per day regardless of how many strike-minutes it contains (KXEURUSD: 46
clusters in 64 days).

| | rule |
|---|---|
| **Target** | **$75-100/week floor**, not $250. A verified small edge can be sized up; an unverified large one cannot. $250 was killing candidates for the wrong reason. |
| **Capital** | Separate account, ~$1,000. Capital has never been the constraint — the maker sim peaked at **$66** of a $2,000 ceiling. Slots and variance bind, not cash. |
| **Correlation** | Must be genuinely uncorrelated with late-certainty (daily P&L |r| < ~0.3). Otherwise a second account buys bookkeeping, not diversification. |
| **Must not compete** | No quoting into the same series/band/window the live bot is working, or the two race each other for the same fills. |
| **Mechanism first** | State the mechanism in ONE sentence before testing. The 2026-08-25 deep scan produced a survivor out of 836 cells that still has no explanation; that is how the 22-vs-21 coin flip happened. |

**Dropped — these bound the last search and none of them was ever a real requirement:**

- ~~Order-book-only.~~ Every strategy tested through 2026-08-25 reads nothing but
  price/bid/ask/path. **The entire external-information space is unsearched.** This is
  the single largest unexplored axis and it is not a corner, it is most of the map.
- ~~Short-deadline only.~~ Horizon is unconstrained. Almost nothing but 15-minute and
  hourly markets has been tested — though note this trades directly against the
  8-week knowability rule above, and knowability wins.
- ~~Minute-level flow required.~~ That screen is correct for a RESTING order and
  irrelevant for a taker. It silently excluded 84 series from taker consideration.
- ~~Buy-the-favourite only.~~ No structure has been tested where the payoff is
  anything other than "this side wins".
- ~~The $2,000 shared ceiling.~~ See capital, above.

**Kept, non-negotiable** — these are not obstacles, they are what keeps the account
solvent, and relaxing any of them manufactures confidence rather than finding edge:
cluster resampling (invariant 3) · pre-registration with a decision rule and horizon
fixed BEFORE money moves · the denominator checked, with a **fixed or random pull
order** (see §Strategy-2, the 6-of-6 that was really 8-of-12) · no live-path change
without Chris's explicit sign-off.

**Archive first, search second.** The nightly archive was nine series, ask 88-96c,
100-800s — shaped entirely around late-certainty, so it accumulated validation
capacity for one strategy and destroyed it for every other. `data/candles/wide/` now
runs alongside it (see §Data archival). This is the only constraint here that can
actually be defeated, and only slowly, so it has to be started before it is needed.

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

---

# PART II — EVIDENCE

*Why the config is what it is. Long by design. Nothing here is authoritative over
PART I — if the two disagree, the code won and this text rotted.*

## Check claims, don't trust them

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


## Audit findings that supersede this file (2026-08-24)

Two agents audited this repo in parallel. Where anything below §1b disagrees with
this section, **this section wins**. Full working in `docs/audit/claude/` and
`docs/audit/codex/`; `python3 scripts/verify.py` re-derives every number and names any
two sources that disagree.

**"Lifetime P&L" is not a quotable number.** No source on this machine can produce
one: `/portfolio/settlements` retains ~30 days, state caps at `MAX_POSITIONS_STATE=500`
positions, and `stats` resets on every `STRATEGY_VERSION` bump — it currently reads
**1 trade**. The **-$487** figure that circulated matches none of the three defensible
scopes. Always quote a scope and a window:

| scope | n | P&L |
|---|---|---|
| live series (6) | 1,946 | **-$320.91** |
| + retired 15M — what the dashboard counts | 2,253 | **-$565.96** |
| + non-15M (`KXMLBTOTAL` alone is **-$863.75 on 4 trades**) | 2,258 | **-$1,445.00** |

**The archive is rounded before 2026-08-22, and it matters more than recorded.** The
figures on file (18.4% of identities, 4.1% of rows) measure ROWS. Measured on the
trades the simulator actually *picks*, by running the two exact-cent days both ways:
**128 of 317 selections disagree (40.4%)**, volume **+13.5%**, $/trade **+0.023**, WR
**+0.28pp** — always optimistic. 72 of 74 archived days carry it, and days before
~2026-06-18 can no longer be re-archived. It compounds with the fill-quality error in
§4 rather than cancelling. `scripts/verify.py --check rounding`.

**v5.17 shipped inert and was fixed on 2026-08-24.** The 88-89c YES band could never
place an order: the last look compared the book against `MIN_ASK_CENTS` instead of
`_band_min(side)`, and three further sites mislabelled any such fill. Both auditors
found it independently. The four `BandAsymmetryTests` that were cited as pinning it
only ever exercised the helpers, which were correct — nothing drove the entry path.
`BandReachabilityTests` now does. **The pre-registration clock starts 2026-08-24**, on
its original terms (200 88-89c YES trades, revert below 88.5%, no early reads),
because zero such trades had ever been placed.

**Still open, deliberately not acted on:** `STOP_BALANCE=650` was set proportionally
to a $100 stake and never revalidated at $25; Codex finds the $300 daily limit no
longer establishes as optimal; the edge breaker's 50/0.84, `ORDER_TTL_SECONDS`,
`ORDER_RECONCILE_SECONDS` and the 500-position cap have no value-specific evidence.
The p3 signal inverts above 91c (p3<80 measures +0.315 vs +0.105) — the trader
correctly does not gate there, the CI includes zero, and Invariant 8 applies.


## Invariants — these do not rot

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


## Why the config is what it is

**`YES_ONLY = False` (v5.16).** The NO side was suspended on live YES 46W/1L vs NO
74W/8L. That is **not significant**: z=1.64, two-sided p=0.102, on n=47 YES trades —
about five coin flips of luck, worth roughly the entire $210 P&L gap. Over the full
68 days NO wins **93.65%** and YES **93.79%**; the cluster-bootstrapped difference is
+0.75pp [-0.36, +1.86] in-sample and **-1.59pp** [-4.54, +1.35] on holdout — both
include zero and the sign flips. `--compare yes_only=1` reproduces it; the Aug 13-17
holdout independently confirms YES-only is worse (delta -$1,037, CI excludes zero).

**Execution gap = +0.227¢ (re-measured 2026-08-24, n=500, both sides).** Live fills
land **0.227¢ ABOVE the book's best offer** at the last look, SE 0.018 — YES +0.253¢
(n=269), NO +0.196¢ (n=231), difference t=1.55 so **the two sides are
indistinguishable**. Median +0.097¢; only 5.8% of fills are worse than the book by
more than a cent, so this is a shifted centre, not a fat tail.

~~+0.105¢ (measured 2026-08-17)~~ was **YES-only and measured against a 1-min candle**,
which is stale 47% of the time. The correct comparison is against `book_at_entry`, the
book read ~128ms before the order, over distributions. At **t=+6.6** the old constant
is not stale-but-close; it is wrong by more than double, and every `--slip 0.105`
figure ever written in this file is optimistic by ~10% of the stated edge. Re-run them
at `--slip 0.227`. I re-ran every config sweep at both levels and **no ranking
inverts**, so this corrects magnitudes and not decisions.

This also closes the "we have **no NO-side fill data at all**" open question that was
the main stated risk of v5.16: NO fills as well as YES. `python3 scripts/verify.py
--check slippage` re-derives it; `docs/audit/claude/CLAIMS.md` §1 has the working.

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


## Data archival — read before proposing any research

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

**The WIDE archive — `data/candles/wide/`, added 2026-08-25.** The narrow archive
above is filtered by late-certainty's own entry gates, so a search for a different
mechanism cannot be run on it at all. The wide file keeps the FULL price path — no
band filter, no entry window (0-100c, 0-86400s) — for 24 series including all twelve
15M, plus entertainment and economics series that resolve on scheduled public data and
have never been deep-tested. **~291 KB/day, ~106 MB/year, and it is tracked** — flag
if that becomes a problem. Runs ~6 min after the narrow archive and is FAIL-SOFT: any
error is caught and logged, because the narrow file is what the live research depends
on and must never be blocked by this. `scripts/backtest.py` globs
`data/candles/*.csv.gz` NON-recursively so it cannot see these files, and the
workflow's `git add data/candles` picks the subdirectory up with **no change to
`.github/workflows/`**.

Three things learned building it, all of which cost real time:
- **`period_interval` accepts only 1 and 60.** Anything else returns HTTP 400, which
  reads exactly like "this market has no data" — the first working version silently
  archived nothing but the 15M series.
- **Retry only 429s and network exceptions.** Retrying every non-200 through an
  exponential backoff costs ~32s per market with no data; one entertainment series
  took **15 minutes to return zero rows**.
- **`/markets` accepts `min_close_ts` / `max_close_ts`.** Walking the settled cursor
  instead took **15 minutes to find 27 markets** for one day; the filter returns the
  same day in **0.2s and one page**. `fetch_markets()` (narrow path) still walks the
  cursor — retrofitting it is worth doing, on its own, with both compared on the same
  days, and NOT as a side effect of a research change.

**Every archived day is untouched out-of-sample data for every hypothesis formed after
it.** Backfill with `--backfill N`. Never delete this directory. If the workflow has
been failing, fixing it outranks whatever research prompted the check — a day not
archived is validation capacity permanently destroyed.

---


## Dated observations — perishable

Each needs re-checking; none is settled.

| Observation | Date | Status |
|---|---|---|
| **BET SIZE $35 -> $50 (2026-09-01). SECOND consecutive override of the same bar. Also converts z-gate revert rule 2 from a dollar constant to a ratio — a PRE-REGISTERED CONSTANT CHANGED MID-EXPERIMENT, which is the dated row its header demands** | Sep 1 | **What changed:** `FLAT_BET_DOLLARS` 35 -> 50 (2.5% of a ~$2,000 balance, unchanged from Aug 28; max concurrent exposure $70 -> $100 = 5.0% of balance, also unchanged — the bet grew WITH the bankroll, not ahead of it). `REVERT_PER_TRADE = 0.14` in `scripts/zgate_monitor.py` was REPLACED by `REVERT_RETURN_ON_WAGERED = 0.0041`, and `daily_summary.py` now prints `$/wagered` so the rule stays checkable. **Why a rescale was NOT sufficient this time, which is the whole reason this row exists:** Aug 28 rescaled 0.10 -> 0.14 and got away with it because it landed at n~0 — the entire rule-2 sample was in one unit. This change lands at **n~46 rejected with the n>=500 horizon still ahead**, so the sample will SPAN TWO BET SIZES. A pooled $/trade over mixed units sits between the $35 and $50 thresholds and is correctly compared to NEITHER; there is no single dollar number that is right for that sample. Only per-observation normalisation is, so the rule is now denominated in P&L over dollars actually wagered, which every trade carries individually. Same fix and same reason as `DAILY_LOSS_LIMIT_BETS` and `min_book_depth()` — **third time this repo has re-learned that fixed constants do not survive a sizing change; this was the last fixed dollar constant left.** **Proof it is a units change and not a new number** (both required before shipping): the conversion reproduces independently from two anchors — `0.14 / 34.04 (measured median cost at $35, #233) = 0.004112` and `0.10 / 24.31 (0.972 x $25) = 0.004114`, agreeing to 3 s.f.; and it is neutral at the new size — `0.0041 x 48.61 = $0.199/trade` against a naive rescale of `0.14 x 50/35 = $0.200`, identical to the cent. Approximation stated: the anchor is a MEDIAN cost proxying for the mean, since return-on-wagered is mean-cost weighted. **THE BAR WAS AGAIN NOT MET, on both clauses.** ~2,418 clean trades were asked for and were not counted before deciding; and the balance moved $1,405 -> ~$2,000 in four days, so this is once more a raise taken during a hot stretch — the precise circumstance *"not on a good week"* exists to prevent. Logged as an override. **Two overrides of the same clause in five days: if a third lands, DELETE the clause rather than override it — a bar that is never binding is worse than no bar, because it reads as a control to anyone auditing this file.** **What IMPROVED, honestly:** stop headroom went 1.32x -> **1.45x** the worst measured drawdown (32.0 losses at $50 vs 28.7 at $35), because the bankroll grew faster than the bet. Still under the 1.5x target — exactly compliant would have been $48.48, and $50 was taken as the round number, a deliberate 3% miss against the 12% miss accepted on Aug 28. **Balance provenance, RESOLVED before shipping:** the ~$595 gain is **trading P&L, not a deposit** (confirmed by Chris 2026-09-01; note the $2,000 itself is his recollection — `kstat`, `gh run list` and the dashboard API were all unreadable in this session, so no figure here was independently measured). Two consequences and they point opposite ways: (a) `STOP_BALANCE`'s revisit trigger is a large deposit/withdrawal, which did NOT fire, so 400 correctly stands and the 1.32x -> 1.45x headroom recovery is earned rather than an artefact of new money — the stop was deliberately not moved in this commit anyway, since changing the stop and the bet together makes a future halt unattributable to either; (b) it also confirms this is **raising into a hot streak, +42% in four days**, which is the literal circumstance the *"not on a good week"* clause exists to prevent — knowing it is P&L makes the override LARGER, not smaller. **Prediction recorded so it is not misread later: the next ordinary drawdown will feel materially worse than any previous one purely from the 1.43x size. Do not read that as edge decay.** **Side effect nobody chose, watch it:** `min_book_depth()` self-rescaled **56 -> 80** contracts, a 43% tighter liquidity requirement. This is the designed behaviour, not a bug, but it is the one self-rescaling constant that can COST fill rate — and the z-gate needs volume to reach n>=200 by ~Sep 15. If accrual drops below the ~11-15 rejected/day baseline, this is the first suspect. **Three constants deliberately did NOT move:** `DAILY_LOSS_LIMIT_BETS` stays 20 ($700 -> $1,000, still 50% of balance, an unchanged ratio); `STOP_BALANCE` stays 400 (see above); `EDGE_DEGRADE_THRESHOLD` is win-rate based and size-invariant. **Revert:** `FLAT_BET_DOLLARS = 35` and `REVERT_RETURN_ON_WAGERED` back to a $35-denominated `REVERT_PER_TRADE = 0.14` — though prefer keeping the ratio, which is correct at every size. 76 order-safety tests pass; the bet-size tripwire was updated with a dated line, not loosened. |
| **BET SIZE $25 -> $35 (2026-08-28). Recorded as an OVERRIDE of the pre-registered bar, NOT a satisfied condition** | Aug 28 | **What changed:** `FLAT_BET_DOLLARS` 25 -> 35 (1.8% -> 2.5% of a $1,405 balance; max exposure $50 -> $70) and `REVERT_PER_TRADE` 0.10 -> 0.14 in `scripts/zgate_monitor.py`. **That second change is a UNIT RESCALE, not a relaxation, and this row is the dated entry its header demands:** $/trade is absolute dollars, so a 1.4x bet inflates it 1.4x at an unchanged edge — leaving 0.10 would have silently disarmed z-gate revert rule 2, since a genuine collapse to $0.08/trade would print $0.11 and never trip. The pre-registered MEANING of the rule is unchanged; only its units moved. **THE BAR WAS NOT MET.** The comment above `FLAT_BET_DOLLARS` asked for ~2,418 clean trades or a reconciliation of the capture gap, and said *"not on a good week"*. There were **749** clean trades since the cut, and this was decided during the best week on record — the precise circumstance that clause exists to prevent. It is logged as an override so no future reader mistakes it for the condition being satisfied. **What was measured before deciding, because the alternative explanations are testable and were tested:** (1) **Jul 27-31 was a broken build**, not variance — 288 trades at 28-39% WR on ~92c contracts, -$620.13, margin -9.06pp. It alone makes the lifetime figure negative. Excluding it, **Aug 1-28 is +$566.57 over 2,099 trades / 1,294 clusters, margin +0.75pp, cluster-bootstrap CI [-0.70,+1.99]** — positive point estimate, still includes zero. Quoting a "lifetime" number that blends the broken build with the current strategy is the error this bullet exists to stop. (2) **"Crypto was just calmer" is REFUTED.** Realized vol across the six coins ran **13.04 bp/min over the good stretch against 6.11 prior (+113%)**, cross-checked against the bot's own live `sigma=12.96bp` on Aug 28. The profitable week was the roughest tape in eleven weeks, which is evidence FOR robustness, not against. (3) **The environment was only marginally easier.** Holding a fixed band (ask 90-93c, 150-600s) constant across **25,124 archived markets, traded or not**: env edge +0.68pp last 7d vs +0.03pp prior 71d. Weekly env edge averages +0.06pp with sd 0.61pp, so last week is **1.01 sd above average — ordinary**, and accounts for ~+0.65pp of a ~2.2pp swing (~29%). (4) **But the improvement is NOT statistically established:** daily margin Aug 1-21 +0.91pp (sd 3.33) vs Aug 22-28 +2.15pp (sd 2.43), difference +1.25pp at **t=1.06, not significant**. Seven days is smaller than one day of margin noise, and Aug 1-21 also had a positive mean while containing -5.57pp and -5.34pp days. **So the honest summary is: directionally real, demonstrably not environmental, not proven.** This was taken as a bankroll-scaling decision on those terms — still far inside any Kelly fraction for a 92c book — and explicitly not as a claim the edge is confirmed. **Cost accepted deliberately:** `STOP_BALANCE` headroom falls from 40 to 28.7 losses = **1.32x the worst measured drawdown, under the 1.5x design target**. The stop was NOT lowered to $260 to restore the ratio, because lowering a stop to justify a larger bet trades away the capital that funds a restart; letting the balance grow back into the ratio is the safer of the two. **Three constants deliberately did NOT move:** `DAILY_LOSS_LIMIT_BETS` stays **20** — it is denominated in bets precisely so a sizing change cannot disarm it, and a first pass at this change proposed cutting it to 14 to "hold the halt near $500", which would have re-created the exact fixed-dollar defect that constant's own comment documents (tightening the brake to ~1.1x normal variance, where it fires on ordinary bad days and blocks the trades that recover them — the error is preserved here because it is an easy one to repeat); `min_book_depth()` is already bet-relative through `contracts_for_risk` and rescales itself **39 -> 56**; `EDGE_DEGRADE_THRESHOLD` is win-rate based and size-invariant. **Revert:** `FLAT_BET_DOLLARS = 25` and `REVERT_PER_TRADE = 0.10` together — never one without the other. 64 order-safety tests pass, including the bet-size tripwire, which was updated with a dated line rather than loosened. |
| **z-GATE DEPLOYED LIVE 2026-08-27 — `Z_GATE_MIN=0.761`. SHIPPED AGAINST REVIEW ADVICE, at Chris's explicit direction. Read the revert rule before judging it** | Aug 27 | **What is live:** after every price/prior gate and BEFORE the book last look, a signal is skipped when `z = (spot-floor_strike)/spot / (sigma*sqrt(secs_left/60))`, signed toward the position, is **below 0.761**. `z_value()` is the ONE definition and both the gate and `[SHADOW:Z]` call it, so a logged z IS the number that gated. **FAILS OPEN** — an uncomputable z never blocks, so a Coinbase outage degrades to the old behaviour instead of halting the book, and the ~13% of unscoreable signals (BNB is 71% minute coverage) trade exactly as before. Disable with `Z_GATE_ENABLED=False`; nothing else needs touching. Pinned by 8 tests in `ZGateTests` + `ShadowZTests`, including one asserting the gate stays ahead of the book read. **Evidence:** walk-forward over all 76 archived days with the cut refit weekly on prior weeks only — **+$39.85/day -> +$76.63/day, 11/11 weeks positive**, cut stable at 0.746-0.776, all six series and all three months positive, **max drawdown -$526 -> -$211** and worst-cluster and max-exposure UNCHANGED at -$50/$50. Stratified permutation (z shuffled within week x series x side x ask-bucket x time-left-bucket, full pipeline rerun each draw): null mean **+$107**, real **+$2,667**, **0/500 exceeded, p=0.0020** which is the floor for 500 draws. Moving-block bootstrap over daily deltas: lag-1 autocorrelation **-0.018**, CI [+1901,+3318] at block 5, P(>0)=1.000 at every block length. **THE CASE AGAINST, which was NOT refuted and must not be forgotten:** (1) **the hypothesis was chosen on the same 76 days it is measured on.** Walk-forward removes threshold-fitting leakage only; a valid discovery-level test would have to repeat the entire filter SEARCH under each null, and that was not done. Invariant 8 applies at full force. (2) **The reversal mechanism.** z is a disagreement trade between Kalshi's implied probability and a Coinbase-spot diffusion model. If the venue lead/lag flips, or crypto turns more mean-reverting, **low z stops meaning "fragile" and starts meaning "Coinbase is behind and Kalshi is right"** — the rule then discards WINNERS while every historical number above stays true. Lead/lag has never been measured; the file already records the index running ~0.96bp rich to Coinbase. **This is the failure mode the monitor exists to catch.** (3) The strongest real-money evidence is DROP-ONLY (real fills, +$62.21 -> +$516.53, but cluster CI [-87,+1032] includes zero); the live gate FREES A SLOT and the replacement trades are untested, worth ~20% of the effect on the archive. (4) ~8% of the measured effect is sensitive to tie-breaking among equal-`secs_left` signals, and **neither backtest reproduces production's md5 hash tie-break** (#153/#154). (5) The unscoreable population is not neutral — in the real fills those 454 settlements lost **-$319.97 at 77.97%** — and defaulting them to trade normally is a CHOICE, not a default. (6) Live code had ~5 minutes of runtime at deploy. **FIRST-DAY LIVE BEHAVIOUR, 2026-08-27 (~3h, 35 gate decisions) — records what the live gate actually does, which no backtest could establish.** Block rate **26% (9/35)** against a modelled 20% — a good match. z range +0.414..+1.891, median +0.992. **The rate is NOT stationary and should not be read as one: it ran 0/15 in a calm opening stretch and 9/22 (41%) once volatility picked up.** That is the mechanism behaving correctly — z is vol-normalised, so a higher-vol regime lowers z and blocks more — but it means **any short-window block-rate reading is nearly uninformative**, and the 8-35% distribution-shift trip in the revert rule needs its full 3 consecutive days precisely because of this. Two concerns raised and then RETIRED on evidence the same afternoon, recorded so neither is re-raised from the same reasoning: (1) *"the gate is inert"* — 0/15 blocked looked structural, was a calm-market sample; (2) *"the gate DEFERS rather than excludes"* — real and structural (z rises mechanically as tau shrinks, and the bot re-evaluates every ~15s, so a blocked signal can pass later), measured at a **20% deferral rate (1 of 5 blocked markets later traded)**, but the one observed case is benign and instructive: `KXXRP15M-26AUG271515-15 YES` went SKIP z=+0.54@485s -> PASS z=+1.07@466s, i.e. **19 seconds later**, over which tau moved 2% and could not account for z nearly doubling — the move came from spot leaving the strike, so the position genuinely improved and entry landed at 466s, inside the +1.27pp 360-480s window rather than the adversely-selected 150-240s one. **Do not "fix" deferral with a sticky block on the strength of the argument alone** — the first proposed fix (make the gate's block sticky) would not have caught this case at all, because the gate never saw it at low z until it had already improved. If deferral is to be closed it needs its own pre-registration and a mechanism check, not an implementation reflex. **LIVE RESULTS THROUGH 2026-08-29 (~49h, 39 rejected signals settled) — a running total, NOT a verdict.** Rejected set: **39 settled, 3 lost, 36 won = 92.31% against a 91.20% break-even, edge +1.10pp, net -$4.** Backtest expects ~5.8 losses at this n; 3 observed. Health: ~4 runs/h, no failures, no halts. Block rate drifts DOWN across the window (24% -> 20% -> 14% cumulative) against a modelled 20% — inside the 8-35% trip band, worth watching, not actionable. Volatility over the same window is at the **66th percentile** of the backtest period (live median sigma 6.49bp/min vs 5.23), so there is **no calm-regime alibi** — that hypothesis was raised and killed. **THE METHODOLOGICAL LESSON, worth more than the numbers and it cost two false alarms:** at **n=24** the rejected set was 24/24 winners, a likelihood ratio of **8.7:1 AGAINST** the gate; I called it "the first genuinely negative signal" and offered to revert. Fifteen more observations moved it to **2.6:1** — uninformative. **That swing on 15 data points is precisely what the 200-signal horizon exists to absorb, and the one drifting off the pre-registration was the analyst, not Chris.** Rule: **do not quote a likelihood ratio at n<100 here.** Report raw counts; flag only PRE-SET thresholds. Three rescue hypotheses were also raised and killed on evidence in the same window — "the gate is inert" (small sample), "deferral pushes entries into the adversely-selected window" (the one observed case deferred 19s and entered at 466s, the BEST timing band), "it is a calm regime" (66th percentile). **Every attempt to explain away an interim result failed, in BOTH directions.** **Crash fill 2026-08-29 09:57Z — `KXETH15M-26AUG290600-00` NO filled at 85.00c on a 92.20c book, 188ms stale. NOT gate-caused:** the gate runs BEFORE the book read by design and is pinned there by `test_gate_runs_before_the_book_read`; 188ms sits inside the normal 131-231ms post-deploy range. It WON, +$5.22. **Separate finding worth its own look: it filled 37 contracts for $31.45 against a $25 flat bet — 26% over.** A cheaper fill buys more contracts, so a crash fill silently UPSIZES exactly when the book is disorderly. Pre-existing, unrelated to the z-gate, and not currently controlled. **PRE-REGISTERED REVERT RULE — fixed 2026-08-27 BEFORE any live gate data existed, enforced by `scripts/zgate_monitor.py`, which exits 2 and emails when one trips. Editing these numbers after seeing results is the optional-stopping error #152 died of; a change requires a NEW dated row.** **(1) REVERSAL — the one that matters: revert if rejected signals win at or above THEIR OWN break-even (mean ask + 0.539pp fees) once n>=200 rejected scoreable signals.** Backtest expectation is 83-88% against a ~91% break-even, i.e. clearly negative edge; at or above zero means the relationship has inverted. **(2) revert if overall $/trade < 0.10 at n>=500 trades** (`daily_summary.py`, never state-derived). **(3) revert if the rejection rate sits outside 8-35% for 3 consecutive days** — expected ~20%; outside that the z distribution has moved and 0.761 no longer means what it was fitted to mean. This one needs no outcomes, so it is the fastest warning available. **Horizon: 200 rejected scoreable signals lands in ~6 days at ~35 rejections/day. Do NOT read the reversal test before then, and do not stop early on a good stretch.** Full validation at n=1,000 rejected. Note the reviewer asked for 4-6 weeks; the extra time buys REGIME diversity, not sample size, and that difference should be stated honestly whenever this row is cited. **Deploying did not blind the measurement** — `archive_candles.py` records every market whether or not it is traded, so the gate's own decisions stay scoreable forever. **Not shipped, deliberately:** the sizing tilt (ties on edge, raises worst-cluster to -$64 and would force revalidating three risk controls) and z-ranked slot allocation (adds +$133, CI [-278,+556]). **Highest-value follow-up: a second spot source for BNB** — the gate currently cannot reach the trades that most need it. Monitor: `python3 scripts/zgate_monitor.py --days 7`. Reproduce: `scripts/vol_zscore_full.py`, `scripts/zgate_inference.py`, `scripts/zgate_variants.py`, `scripts/zgate_counterfactual.py`. |
| **z-GATE — PRE-REGISTERED 2026-08-27, NOT DEPLOYED. Phase 1 is shadow-only. Read this whole row before touching it** | Aug 27 | **A filter, so Invariant 8 applies and this is held to the higher bar it demands.** Claim: at a fixed 88-93c ask the market misprices *vol-normalised* distance to strike. Define, signed toward the position, `z = (spot - floor_strike)/spot / (sigma * sqrt(secs_left/60))` with `sigma` the trailing-60min sd of 1-min log returns. **Volatility as a LEVEL is refuted again and independently** — realized (1m/15m/60m/24h), Deribit DVOL and VIX are all flat against daily $, $/tr and WR (every abs(r)<0.09, every p>0.45, n=75 days), and entry-vol quintiles taken within (series x ask cent) are non-monotonic with top-bottom CI [-0.29,+0.76]. That reproduces `vol_bucket_test.py` on an EXTERNAL measure, so the Aug-18 refutation is not an artifact of using the Kalshi price as its own vol proxy. The one level effect is on VOLUME not edge: high-vol days generate more signals (r=+0.30, p=0.008). **As a DENOMINATOR it is the largest predictor in the dataset:** bottom z quintile 85.24% WR / -$1.795/tr / **-$2,870** — the strategy's entire loss column — against top quintile 97.19% / +$1.237. Top-bottom **+$3.03/tr, CI [+2.46,+3.59]**. Vol does the work, not distance: the same numerator over sqrt(tau) alone spreads +2.53, adding sigma widens it to +3.03, and high-z vs low-z WITHIN a dt quintile is **+$1.77/tr, CI [+1.37,+2.17]**. Not already in the price — inside every ask cent, high-z minus low-z is **+$2.23/tr, CI [+1.83,+2.63]**. **Re-simulated with slots reused** (rejected signals free their slot for the next signal in the cluster, so this is not a filter over a finished trade list): cut fitted on <Aug 1 at the IS 20th percentile, `z >= 0.764`; **HOLDOUT Aug 1-24: +$55.31/day -> +$84.92/day, delta +$711, CI [+305,+1122], P(>0)=1.000, 1,868 clusters**; positive in 6/6 series; flat from the 10th to 30th percentile (+$2,302 to +$3,085), so not a knife edge. **DECOMPOSITION, and this is the number to hold onto: +$668 of the holdout +$711 is losses NOT TAKEN, which does not depend on capture; only +$43 is freed slots refilled, which does and which Invariant 6 says the backtest overstates.** So the claim barely rests on the part the archive is bad at. **The sigma is ALREADY LIVE** — `_spot_momentum()` (l.1118) computes exactly this 60-min sigma (`MOMENTUM_VOL_WINDOW=60`), caches it per wall-clock minute, and has logged it as `sigma=X.XXbp` on every `[SHADOW:MOM3]` line since Aug 21. z needs only the strike and the spot level, both already inside that call. **BRTI GAP — MEASURED AND CLOSED 2026-08-27, it was the single open risk and it is no longer one.** CF Benchmarks' own API is paywalled (`not authorized`), but Kalshi hands over the settlement basis for free: `expiration_value` is the true RTI 60s-average print, and **`strike[t] == expiration_value[t-15min]` EXACTLY** — validated 100.00% on all six series over 38,622 settled markets, with `result == (expiration_value >= strike)` also 100%. So **`floor_strike` was ALWAYS the exact RTI level at the market's open**; the only proxied term in z was ever the entry price. Three consequences. (a) These are 15-minute UP/DOWN markets — *is the RTI at close >= the RTI at open* — so z reads as "how far it has moved since this market opened, in units of how far it can still move", which is exactly the right quantity. (b) Measured Coinbase-vs-RTI basis over 38,622 prints: **median -1.55bp, IQR 7bp, sd 18.4bp, 4.4% beyond 20bp**. (c) **Stress test, `scripts/vol_zscore_brti.py` + basis resampling:** holdout delta is **+$891 as measured**, **+$392 with a full extra unit of empirical basis injected into the entry price**, +$117 at 2x, and only turns negative at **3x**. The injected version DOUBLE-COUNTS, since the live number already uses a Coinbase entry, so **+$392 (+33% on a $47.64/day baseline) is a floor, not an estimate.** Also tested: rebuilding the numerator as a pure same-source Coinbase return from open to entry, which cancels the level basis entirely, measures **WEAKER** (holdout +$576, CI [+189,+997]) — because it trades an exact RTI anchor for a noisy Coinbase estimate of the same instant. **Keep the exact strike as the anchor; do not "fix" the basis by moving both ends to Coinbase.** Remaining gaps, both real:  (1) live capture of replacement signals, which the decomposition above bounds at +$43 of +$711; (2) **Invariant 8** — this is a post-hoc filter and that has a bad record here, which is an epistemic risk no amount of backtesting retires; ~~(3) sigma definitions differ~~ **CLOSED 2026-08-27** — RMS vs mean-centred sample sd correlate **0.9998** over 1,123 sampled 60-min windows, median ratio 0.9948, so z shifts <1% worst case against a cut at 0.764. **The trader's live sigma is canonical**; `shadow_z` reuses it rather than recomputing, so the two cannot drift; (3) z is known for 86.7% of signals (BNB is 71% minute-coverage on Coinbase) and unknowns are KEPT, never dropped; (4) the baseline carries the pre-Aug-22 rounding artifact, and the 3 exact-cent days show +$89 on n=364, which is nothing. **GATE-LOG PRE-CHECK, 2026-08-27 — the strongest evidence here, and it is NOT archive replay.** Scored on `data/gatelog/*.csv`, i.e. what the bot ACTUALLY SAW at poll instants (Invariant 6's correct dataset), Aug 22-25, n=1,448 first-polls joined to settlement: **z<0.764 wins 83.65% against a 90.80c break-even = -7.15pp**, z>=0.764 wins 95.89% against 92.37c = **+3.52pp**. Terciles are monotonic: 88.82% / 94.81% / 98.76% (edge -2.13 / +3.17 / +4.91pp). This does NOT close the BRTI gap — it uses the same Coinbase spot — but it does show the effect is present in live-observed polling, not only in archive replay. **Trap that cost an hour and will cost it again: Kalshi ticker timestamps are ET, not UTC.** Parsing `KXBTC15M-26AUG250000-00` as UTC puts every entry 4h off, samples the wrong spot, and **inverts z into a fake refutation**. Always take `close_ts` from the archive by ticker; never parse it out of the ticker string. **FULL-WINDOW WALK-FORWARD, 2026-08-27 — `scripts/vol_zscore_full.py`, and this is now the headline number.** Every day that exists: **2026-06-11..08-25, 76 days**. That is the MAXIMUM, not "since the series launched" — BTC/ETH 15M predate our data by months (their volume programs ran to 2026-05-12) but retention has moved and **the API now returns ZERO markets on or before 2026-06-20**; Jun 11-19 survives only because `archive_candles.py` backfilled to the Aug-17 retention floor, and `research/search2/data_ohlc/` starts Jun 19, so **the narrow archive is the deepest dataset in the project**. Each week is scored by a cut refitted on PRIOR WEEKS ONLY, so no day is judged by a threshold that saw it: **baseline +$2,869 (+$39.85/day) -> gated +$5,766 (+$80.08/day), delta +$2,897 (+$40.23/day), CI [+2150,+3663], P(>0)=1.000 over 5,330 clusters, and 11 of 11 weeks positive.** The refitted cut is stable at **0.746-0.776** across every week, so the threshold is not drifting. All three months positive out-of-sample (Jun +$723, Jul +$1,628, Aug +$735) and **all six series positive**. Note the signature: July was the WORST baseline month ($25.70/day) and the gate lifts it to $78.22 — it helps most where the baseline is weakest, which is what a risk filter should do and what an overfit one usually does not. **What walk-forward does NOT fix:** it removes threshold-fitting leakage only. The hypothesis itself — use z, 60-min sigma, ~20th percentile — was chosen after looking at this whole dataset, and no re-slicing of it can cure that. Invariant 8 is about exactly this, and prospective data is the only answer, which is what Phase 1 is for. **REAL-ACCOUNT COUNTERFACTUAL, 2026-08-27 — `scripts/zgate_counterfactual.py`.** Scored on actual fills and settlements (~30d retention, Jul 28-Aug 26), real fill price, real fill timestamp, real outcome, DROP-ONLY with no slot refill: on the 1,802 settlements where z is computable, **actual +$62.21 at 90.62% WR (+$0.035/tr) -> gated +$516.53 at 94.99% (+$0.365/tr), skipping 385 trades that lost -$454.32 at 74.55% WR.** But **the cluster bootstrap on the real account is CI [-87,+1032], P(>0)=0.945 — it INCLUDES ZERO**, because the live book is 1,166 clusters across three bet sizes with crash-fill variance, against 5,330 uniform-$25 clusters in the archive. Treat it as CONSISTENT WITH the archive result, never as independent confirmation. Three further caveats that matter: (a) z is uncomputable on 454 of 2,256 settlements, and those lost **-$319.97 at 77.97% WR** — 240 of them BNB, whose Coinbase minute-coverage is 71%; **the gate as designed cannot reach the trades that need it most, and a second spot source for BNB is the single highest-value implementation fix**; (b) of the 8 worst individual losses it catches 3 — this is a RATE effect, not a tail filter, so it will not stop the -$74 days; (c) free side effect — it skips **120 crash fills below 88c that won only 45.83%**, because spot sitting on top of the strike is what a crashed book looks like. **FORM TEST + PERMUTATION NULLS, 2026-08-27 — run BEFORE deploying, because shipping the wrong FORM is a change that has to be undone.** Four ways to spend the finding, same walk-forward: **A hard gate +$2,905** CI [+2135,+3663]; **B z-RANKED slots instead of earliest-first, no gate +$677** CI [+96,+1281]; **C gate+rank +$3,038** — ranking adds only **+$133 on top of the gate, CI [-278,+556], NOT established**; **D sizing tilt 2x/0.5x at the same average capital +$2,851** CI [+2284,+3462]. A and D are statistically indistinguishable (D-A = -$54, CI [-469,+383]) **but they are not the same risk**: A leaves worst-cluster at -$50 and max exposure at $50 and cuts max drawdown -$526 -> **-$211**; D raises worst-cluster to -$64, exposure to $64, drawdown -$267, and would force revalidation of `MAX_CONCURRENT`'s cluster cap, the bet x 20 daily limit and `STOP_BALANCE` — Invariant: fixed constants do not survive a sizing change. **The hard gate wins: same edge, strictly less risk, and the only variant that leaves every existing control's calibration intact. Do not deploy the tilt, and do not bother with z-ranked slots.** **PERMUTATION NULLS — the honest attribution, and it runs the OPPOSITE way to the obvious worry.** GLOBAL shuffle (z permuted across all rows, i.e. z is meaningless): **-$5.68/day**, real is **11.4 sd above, 0/60 permutations beat it**. WITHIN-CLUSTER shuffle (keeps cluster-level z, destroys the choice of which contract to skip): **+$19.34/day**, real is 8.1 sd above, 0/60. So the +$39.80/day decomposes as **mechanical -$5.68 (-14%)** + **cluster-level z +$25.02 (63%)** + **contract-level z +$20.46 (51%)**. Cutting ~20% of a slot-bound book with a meaningless rule LOSES money — z has to overcome a headwind, it is not riding one. **Most of the value (63%) is sitting out WHOLE SETTLEMENTS where every coin is on its strike**, which is the same whipsaw-regime effect that makes the daily loss limit work (§ daily loss limit) — a mechanism, not a curve fit. **Decision asymmetry: -$5.68/day if the whole idea is wrong against +$39.80/day if it is right, ~7:1, and the gate halves max drawdown either way.** **Deploying does NOT blind the measurement** — `archive_candles.py` captures every market regardless of what is traded, so the gate's own decisions stay scoreable forever. That materially weakens the case for waiting to measure. **PHASE 1 — SHADOW ONLY, gates nothing, trades nothing.** Log `[SHADOW:Z]` with spot, strike, sigma, secs_left and the computed z for every market in the live band, alongside the existing MOM3 line. **HORIZON: 1,000 logged in-band signals AND at least 400 settlement clusters, whichever is later.** Rate basis, fixed now so this is not re-guessed later: the archive yields ~173 qualifying in-band signals/day, so budget **~6-8 days**, not the ~4 weeks a first estimate assumed off the union-band gate-log rate of ~360/day. The cluster floor is the binding one — Invariant 3 — and exists so a 3-day burst cannot end Phase 1. **PHASE 1 DECISION RULE, fixed now:** proceed to Phase 2 only if, on live-logged z joined to settlement, the bottom quintile wins **below 90.5%** (its break-even at the observed mean ask) AND the top-bottom $/tr CI EXCLUDES zero on cluster resampling. Otherwise the effect did not survive the live feed and this row moves to PART III. **PHASE 2 is a separate pre-registration** and is not authorised by this one — it must state its own horizon and revert rule before any gate goes live. **Do not shorten Phase 1 because the numbers look good early — that is the optional-stopping error #152 died of, and a filter is exactly the shape of thing Invariant 8 says fits noise here.** Reproduce: `python3 scripts/fetch_spot.py` then `python3 scripts/vol_zscore_test.py`. |
| **v5.17 DEPLOYED 2026-08-24 — 88-89c YES-only band. PRE-REGISTERED, read before judging it** | Aug 24 | **Written before deployment, unlike #152.** Change: YES entries reach to 88c; **NO stays at 90c** (88-89c measures YES +$0.39/tr vs NO -$0.42/tr, so symmetric sweeps cancel it to -$2.07/day — that is why `MIN_ASK=89` sat unresolved for a week). Runs on the **existing 2 slots**, so simultaneous exposure is UNCHANGED; new entries displace marginal 90-93c ones. The existing <=91c low-ask gate (3rd prior >= 80c) already covers 88-89 and cuts the population 3,995 -> 1,959, keeping the better half. **Backtest (full archive, 0.105c slip, $25):** live +$2,657 / +$0.299/tr / +$35.93/day -> combined **+$3,101 / +$0.338/tr / +$41.92/day**, delta **+$443, CI [+68, +826], P(>0)=0.987**. Per-trade value RISES, which is the signature of displacing marginal trades rather than adding volume. 3 slots was tested and is WORSE (CI [-228,+1219], $/tr falls to 0.278) — do not add slots. **PREDICTION:** the 88-89c YES subset wins at ~90.7% and returns ~+$0.39/tr; overall $/tr rises from ~0.30 toward ~0.34. **HORIZON: 200 88-89c-YES trades. At ~3.7/day taken after slot competition that is ~54 days (~2026-10-17) — deliberately long, because that is the honest rate; do not read the subset before then.** **DECISION RULE, fixed now:** revert if the 88-89c YES subset WR is **below 88.5%** (its break-even) at n=200, or if overall $/tr falls below 0.25 at any point. Keep otherwise; full validation at n=1,000. **Do not stop early on a good stretch — that is the optional-stopping error that #152 died of.** Pinned by 4 tests in `test_order_safety.py::BandAsymmetryTests` so a future symmetric "cleanup" cannot silently reintroduce the -EV NO side. |
| **The archive is ROUNDED before 2026-08-22 — ~4% of the live band is phantom** | Aug 24 | Measured, not inferred. Every archived day from 2026-06-11 to 2026-08-21 stores **integer cents**; only 2026-08-22 onward is exact (#163). On the exact-cent days, **106 of 2,563 rows (4.1%) change band under rounding — all of them `outside -> 90-93`**, e.g. a true 93.4c ask becoming 93c. So roughly **4% of every historical row in the live 90-93c band was never really in the band**, and every pre-Aug-22 claim inherits it: `PRIOR_MIN_CENTS=75` (filter audit Aug 10), `MIN_ASK=89`, edge-by-price, entry-timing, and the config sweeps. **The 88-89c band gains ZERO rows from rounding**, so v5.17's new band is clean — but its BASELINE carries the 4%, which could bias the measured delta. Flagged under the freeze, not acted on. Re-deriving any pre-Aug-22 claim requires either restricting to 2026-08-22+ or re-archiving with `--force` (the raw candles are recoverable from Kalshi for ~67 days, so **days before ~2026-06-18 are already unrecoverable**). |
| **"EXTRA" is NOT bad trades — it is a resolution artifact** | Aug 24 | Diagnosed all 56 Aug-23 EXTRA trades against the archive and the fill records. **100% filled INSIDE the 90-93c band** (range 90.0-93.0), **100% inside the 150-600s window**, **94.6% met ALL THREE criteria** including priors. The model rejects them because the 1-min archive samples once a candle while the bot polls ~4x/min: the archived close ask reads 93.3 or 94.5 while the bot filled at 91.9. **The bot is already doing what the criteria specify.** EXTRA measures reconciliation coverage, not trade quality. Baseline EXTRA was *also* 97.5% in-band yet lost -$3.65/tr at 86% WR, and priors held in ~93% of BOTH groups (baseline priors-held: 87.6% WR, -$2.64/tr; Aug 23 priors-held: 94.3% WR, +$0.55/tr), so neither price nor priors explains the quality gap — no mechanism was found, which supports the #180 revert rather than undermining it. |
| **100% capture is not a goal worth having — it is worth about -$3** | Aug 24 | Every one of the 42 Aug-23 misses classified: **17 price NOT purchasable** (depth<60 — the quote was not on offer; model pnl +$34.69 is phantom), **17 seen + purchasable but not taken** (model pnl **+$7.87 total**, i.e. $0.46/tr), **8 never observed** (model pnl **-$10.60** — missing them MADE money). Closing the entire capture gap from 65.3% to 100% nets **about -$2.73**. Capture is a diagnostic, not an objective; the raw % understates true capture because ~40% of misses were never buyable. |
| **The concurrency cap blocks half of everything and costs nothing** | Aug 24 | `heat check: N open positions` is **102 of ~200 skips (51%)** across 8 sampled runs — by far the largest reason a qualifying entry is not placed (next: 74 ask-moved-between-scan-and-order, 16 priors-failed-at-order, 6 thin book). But sweeping it over the full archive at 0.105c slip, $25 flat: **max_conc 2 = 9,383tr +$2,624 (+$0.28/tr); 3 = 11,715tr +$2,610; 4 = 12,906tr +$2,860; 5 = 13,422tr +$2,483; 6 = +$2,538.** Going 2->3 adds 2,332 trades and **loses $14**. Slots are allocated earliest-signal-first and earlier entries are better (§ entry timing), so everything the cap blocks is marginal by construction. **Do not raise MAX_CONCURRENT to buy volume** — it buys trades, not dollars, at strictly worse $/tr and higher simultaneous exposure. This retires the "more slots is the path to more P&L" idea recorded on Aug 23. |
| **Depth gate is NOT too strict — the $48/day was an artifact** | Aug 23 | `depth==0` is **50.6%** of all gate-log rows and **88% of every block**, and it does not mean "thin book" — it means **nobody is offering at the price the signal fired on**. For depth-0 rows the real best price is **+1.30c worse** (median, 85% of cases); for depth 1-59 it is +1.15c worse. Only depth>=60 fills **better** than the signalled ask (-1.15c), which is exactly why execution measures as an ASSET (+$77.72). The backtest scored the blocked population as fills at the candle ask, so the "~$48/day" was pricing trades that were never purchasable. Arithmetic: at 92c break-even is 92.0% against ~93.5% WR (+1.5pp); pay 1.3c more and break-even is 93.3% -> **+0.2pp, gone**. `MIN_BOOK_DEPTH=60` is a near-perfect availability detector. Do not loosen it. |
| **88-89c YES-only, run as a SEPARATE book — the one survivor** | Aug 23 | Best new candidate found in the Aug 23 search. Disjoint from live by construction (below the band, YES only): **n=1,833, WR 90.73%, +$0.39/tr, +$9.88/day** at 0.105c slip, bootstrap **CI [-35,+1435], P(>0)=0.970**, and critically **84% executable** (depth>=60) versus 11-15% for the 94-96c candidates. Survives every split: slippage 0->0.105->0.5->1 tick = +0.421/+0.393/+0.287/+0.155 (never inverts), all three time-thirds positive (+0.267/+0.831/+0.109), 5 of 6 series positive (DOGE -0.267 the exception). **Side asymmetry is the whole point** — at 88-89c YES makes +$0.39/tr while NO loses **-$0.42/tr**, so a symmetric sweep cancels it to -$2.07/day, which is why `MIN_ASK=89` never showed up as significant. **Only additive with its OWN concurrency slots**; on the existing two it displaces live trades — the same reason `max_ask=94` reads P=0.65 as a config widening and P=0.998 as a separate book. Realistic after executability and the ~47% live capture: **+$4-5/day, ~+20%, NOT a doubling.** Caveats: CI touches zero, and the latest third is the weakest. |
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
| `[EXEC]` fill records now logged | Aug 21 | Every fill emits `side / scan / fresh / book / depth / book_age_ms / limit / contracts / cost / fee / avg_fill / attempts`. Compare `avg_fill` to `book` **by side, over distributions** — never per-fill against a candle (+0.85¢ artifact, §4). ~500 NO fills ≈ **12-16 days**. Harvest: `grep '\[EXEC\]'` over run logs. |
| MOM3 live rate is far below forecast | Aug 21 | Research predicted the veto bucket at ~8% of volume (8-10/day). First live day: **2/day** at m3>+0.50, 1/day at >+1.00, median m3 **−0.58** (n=62). At that rate 500 blocked trades is **~250 days**, not 6-8 weeks. If it holds a week, MOM3-as-veto is dead on timeline alone and only survives as a sizing input. |
| $200 daily-limit threshold, now measured | Aug 21 | The level the `bet×4` bug created is **worse than having no limit at all** (+$4,676 vs +$4,737); $300 is the best in the sweep (+$5,178). Retroactively confirms PR #134. Directional — the rolling-P&L sim approximates `daily_pnl` rather than reproducing it. |
| 94% WR with ~zero P&L is a SIZING artifact | Aug 19 | Win rate counts trades; P&L counts dollars. Bet size ran **$2.79 → $74 (26x)** inside the Aug 1-18 window, so most wins were banked when a win paid **$0.20** while the losses landed at $45-74. One $74 loss erases ~370 early wins. Dashboard showed 94.5% WR and **+$0.16/trade** against a +$1.00 backtest at flat $75. Now that sizing is flat $50 this distortion is gone — and it means the historical figure *understates* the edge. |
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


---

## History — how the current config was arrived at

*Moved out of PART I on 2026-08-24: this is narrative about work already
completed, not state anyone operates from. Kept in full because it records
why several things are the way they are.*

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

## Strategy-2 search — 2026-08-25, what was tried and what it cost

A systematic search for a SECOND strategy. Tooling in `research/search2/`; results in
`research/search2/results/`. Recorded so none of it is repeated.

**Read this before proposing a second strategy.** Two of the three ideas I generated
on 2026-08-25 were already in this file as tested — WTI ran LIVE for days and was
paused on 2026-08-19, GOLD/SILVER have measured per-trade numbers, and cross-strike
arb is in the GRAVEYARD. Proposing them wasted an afternoon. Search this file first.

**The fee is a parabola, and it is the strongest filter available.**
`fee = 0.07 x P x (1-P)` per contract, maximised at 50c: **1.750c at 50c, 0.515c at
92c.** Verified via `/series` that all 78 high-frequency series are
`quadratic / multiplier 1`. This kills every direction-neutral structure in the
GRAVEYARD at once — a market-neutral pair trades near 50c and pays the fee on BOTH
legs, 3.4x, for an edge that is smaller by construction. Eleven for eleven was
arithmetic, not a losing streak. **Compute the fee at the target price before
building anything.**

**MAKER FILLS ARE FREE — and it is not enough. TESTED 2026-08-25, REFUTED.**
Re-verified at 15x the evidence: **15,159 maker contracts at $0.00** against
0.5983c/ct taker (n=74,575), `/portfolio/fills` 07-24..08-25. ~~998 contracts at
0.5493c~~ was the earlier, smaller measurement. The FACT is true and `yes_ask +
no_ask = ~102c` still explains why both sides of every market scan negative as a
taker. The STRATEGY is not: resting a bid on the favourite side of a 15M market and
holding to settlement measures **+0.10c/contract, CI [-0.47, +0.65], P(>0)=0.630**
across all twelve 15M series, 68 days, 109,278 modelled fills — **8 series positive,
4 negative**, and dropping KXBNB15M alone takes the pooled figure to **-0.23c**. The
~2c a maker collects is exactly the size of the adverse selection it buys. Write-up
and the two doors that could reopen it: `research/search2/results/MAKER.md`.

**The searchable universe is 118 series, and the liquidity is not in crypto.**
`python3 research/search2/universe.py` — Climate and Weather 78.4M, Commodities
66.1M, Entertainment 17.0M, **Crypto fifth at 9.8M**. The binding constraint on
proving anything is observations inside the ~67-day retention window, which is
`events/day x strikes/event` — a daily series with 29 strikes beats an hourly one
with a single strike.

**The 15M half of the step-3 archive is ~8 DAYS DEEP, not 67.** `pull.py` caps at 600
markets and a 15M series runs 96 markets/day. Every 15M figure in the scan output rests
on about eight days; the 14.2M observations are carried by the weather ladders, which
do reach back 67 days. Not a small correction — the maker rule measured **+6.4c on 7
days and +0.10c on 68**. Check the date span of any 15M pull before believing it.
`pull_ohlc.py` defaults to 8,000 markets and threads the fetch (6 workers,
~12 min/series).

**RE-RUN ON 10x THE HISTORY, 2026-08-25 — still nothing.** `deep_scan.py` over
`data_ohlc/` (60,342 markets, 68 days, twelve 15M series, OHLC not closes): 836 cells
in-sample, **4** cleared a 95% CI, **1** survived the holdout —
`90-93c / prior75-89 / calm(rv3<=8)`, OOS +2.26c [+0.70, +3.71], full window +2.14c
[+1.11, +3.10], **11 of 12 series positive**. It is a slice of the LIVE band, and it is
**not actionable**: the non-calm entries it would exclude are **81.5% of volume and
$1,690 of the $2,764** total over 68 days, so filtering to calm COSTS $1,690 (and on
holdout, $972 -> $610). Identical arithmetic to the dispersion filter; invariant 7
again. As a SIZING tilt rather than an exclusion it is not worthless (calm 2x / other
0.5x, normalised to the same average bet, is +$56.78/day against flat's +$40.81) — but
that is a live-trader change, it belongs with the time-weighted-sizing thread that
already needs 122-279 days, and the filter framing is the honest one because
late-certainty is slot-bound (`MAX_CONCURRENT=2`), not capital-bound.
`research/search2/results/DEEP_SCAN.md`

**What the scan found: nothing that survived.** 14.2M observations, 60 series, every
slice ranked by `(won - ask) - fee`. Nine cells cleared a 95% CI in-sample; **zero**
survived the holdout. A `97-99c / prior>=90` pattern appeared in 10 unrelated markets
and looked structural — it was not: **22 series positive, 21 negative**, a coin flip.
That error came from reading a leads list that only PRINTS positives and treating it
as a sample. **Always check the denominator before calling something replication.**
**And checking the denominator is not sufficient if you also chose the order the
numerator arrived in.** The 2026-08-25 maker test read **6 positive / 0 negative of 6
series**, stable in and out of sample — because the pull was ordered by which series I
expected to work. At the full twelve it was 8 / 4 and the pooled estimate was zero.
**Pull in a fixed or random order, and do not read the table until it is full.**

**The scan is only trustworthy WITH path features.** Its first version sliced on
price/secs/side/spread — all snapshots — and rated the live 90-93c band at **-1.69c**,
i.e. losing. Late-certainty is a PATH strategy, so the scan could not have found it.
Adding prior-price features flipped the same band to **+1.71c** and made it monotone
in priors. Any future scan that cannot rediscover late-certainty unprompted is
broken; do not act on its output.

# PART III — GRAVEYARD

*Refuted, closed, or not worth re-opening. **This section exists so nothing gets
re-proposed.** Before suggesting an idea, check here first.*

*Nothing is deleted, only moved: the reasoning is kept so a future revisit starts from
the evidence rather than from scratch. A row here can be re-opened — but only with
NEW data, never with a new argument about the old data.*

## Refuted hypotheses (moved from dated observations, 2026-08-24)

| Observation | Date | Status |
|---|---|---|
| **#152 MEASUREMENT RESULT — failed its own test, REVERTED in #180** | Aug 24 | The pre-registered clean-day test ran on 2026-08-23. **Capture 45.1% -> 65.3% (+20.2pp)**, the highest of any day measured. **EXTRA did NOT fall**: 25.3% -> 46.3% per opportunity, count 38/day -> 56. #152's own commit message says: *"If EXTRA does not fall, the mid-candle-noise explanation is wrong and this should be reverted."* **By the letter of the pre-registration, revert.** BUT the metric was a proxy and the thing it proxied for inverted: **EXTRA went from -$3.503/tr (-$563.98 over Aug 18-22) to +$0.153/tr (+$8.58)**, WR 85.71% -> 92.86%. Live total -$497.79 -> **+$98.11**. Confound checked and rejected: Aug 20 was also a strongly positive day (+$122.56 live) and EXTRA still lost there (-$0.464/tr), so this is not a good-day artifact — per-day EXTRA $/tr runs -9.004, -0.464, -5.989, -2.418, **+0.153**. **RESOLVED: reverted in #180.** The profitability rescue does not survive arithmetic — the +$8.58 is 52 wins against 4 losses, avg win +$2.02 vs avg loss -$24.06, so **one win becoming a loss makes it -$17.49** and two makes it -$43.57; 45 distinct settlement clusters among 56 trades, so effective n is smaller again. A coin flip, not evidence. An outside review made the decisive point: proposing to **wait one more day only after seeing a favourable day is optional stopping** — the wait would not have been proposed on a -$50 day. **Reverting is not a claim that #152 is harmful**; the 20pp capture gain may be real. It is the consequence of the rule chosen before the result was known. If boundary-capture is worth recovering, it needs a FRESH prospective test: exclude Aug 23 as pilot data, freeze the implementation, use settlement-cluster inference, and fix the horizon in advance at the ~1,300-trade bar — not 56. Note the honest risk: this is exactly the "rescue it with a new story" trap the pre-registration was written to prevent, and one day at n=56 EXTRA cannot distinguish a fixed selection problem from a lucky one. `git revert` CONFLICTS (#157/#161-166 touched the same regions), so #180 is surgical: **every #165 daemon failure control is kept** (that is what caught the 2026-08-23 connection resets), and **#164's gate log was adapted** — it gated itself on candle alignment, which only worked while #152 existed, so an unadapted revert would have silently killed the gate log. 36 order-safety tests pass. |
| ask-94 as a separate book — killed by executability | Aug 23 | Model looks strong (n=3,628 disjoint, WR 95.48%, +$14.28/day, **P(>0)=0.998**, holds across halves and 6/6 series) but **only 11-15% of 94-96c signals have depth>=60** versus 47% in the live 90-93c band. Realistic value is ~15% of the model, **~+$2/day**. Notable structural oddity worth remembering: **BNB (+0.535) and SOL (+0.464) are BEST at 94c while being worst in the live band** (+0.03, -0.11) — whatever drives edge at 94c is not what drives it at 91c. |
| **Weather is CLOSED — three independent methods** | Aug 23 | 2.04M vol/24h (2nd largest complex on Kalshi, 40+ temperature series incl. international) and **none of it is usable**. (1) *Forecast edge fails:* Brier **model 0.1499 vs market 0.0932 on n=1,886, market better in 6 of 6 cities**, edge -0.0567 against a +0.0100 gate; the threshold rule loses **-$4,003 over 1,160 trades**. Open-meteo ensembles are public, so they are already in the price. (2) *No mispricing to harvest:* T-24h calibration **n=291, mean_p 0.170 vs actual 0.175 — a 0.5pp gap**, i.e. correctly priced (crypto is +1.42pp at 88c). (3) *The population does not exist:* across **1,184 quote observations**, only **0.9%** sit in 88-96c — **0% inside 6h of close**. Do not reopen this on the strength of the volume figure; the volume is real and irrelevant. |
| ~~Thin-book gate may be too strict~~ — **REFUTED Aug 23, see top of table** | Aug 21 | `MIN_BOOK_DEPTH=60` blocked 13 entries worth **+$50.24** (Aug 19) and 12 worth **+$46.79** (Aug 20) — consistent ~$48/day. **Upper bound only**: the model assumes a fill at the candle ask and knows nothing about what a thin book does to the fill. `[EXEC]` now logs `depth` beside `avg_fill`, so this becomes measurable rather than speculative in ~2 weeks. ~~Best open lead.~~ |
| Loss cooldowns and size-up-after-loss | Aug 21 | Both refuted. Losses are **not** clustered: lag-1 lift +1.91pp, permutation **p=0.095**; lags 2/3/8 negative. Every cooldown loses money and the blocked trades were profitable (+$0.31 to +$0.56/tr). No post-loss edge either (+$0.16/tr, P(>0)=0.61), and sizing up after a loss would *loosen* the daily limit via `max(300, bet×4)`. `archive/research/refuted/loss_cooldown/` |
| Kalshi incentives: not worth pursuing | Aug 19 | Public endpoint `GET /trade-api/v2/incentive_programs` (filters `status`, `type`). 145,145 programs, $9.0M liquidity / $0.9M volume. **SOL/DOGE/BNB/XRP/HYPE/NEAR: never incentivized.** BTC/ETH 15M had volume programs that **ended 2026-05-12**, and zero volume programs are active exchange-wide. Even live they paid $20 pool ÷ 1.68M contracts = **$0.00001/contract** — the $0.005/contract cap never binds. Liquidity programs pay real money but the exploitable pattern is parking unfillable penny walls in dead markets, which risks the "abusive behavior and fake trading" clause. Full write-up + scripts: `archive/research/refuted/kalshi_incentives/README.md`. |
| Hourly crypto ladders (KXBTCD/KXETHD) — no edge | Aug 20 | **Refuted.** 45-day archive, live gates, $50 flat: **1,641 trades, -$0.03/tr, -$41** (-$129 at the measured 0.105¢ gap, -$868 at one tick), vs the 15M book at **+$85.90/day** over the same window. Win rate straddles the ~92.3% break-even and **flips sign between halves for both series** (BTCD -0.57→+0.35, ETHD -1.34→+1.15). The multi-strike "leverage" worry that kept it in shadow was **backwards**: 310 of 392 stacked closes are a YES below spot + a NO above spot, which cannot both lose — 0 all-lose events in 45 days. Real finding: **100% of hourly entries settle on the same BRTI print as the :00 15M close on the same underlying**, and `MAX_CONCURRENT` does not see them as related. `python3 archive/research/refuted/hourly_crypto/analyze_hourly.py` |

## Not currently pursued

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

*Maker / spread collection (2026-08-25):* **refuted — and it is the general answer for
passive strategies here, not a one-off.** `scan.py` scores `(won - ask) - fee`, i.e. a
TAKER, so the resting side had never been evaluated at all. `pull_ohlc.py` re-pulled
with OHLC — candles carry `price.low`, which is what makes a fill modellable, a resting
bid at B being filled iff someone traded at or below B — and `maker_eval.py` scored it.
Pooled **+0.10c, CI [-0.47, +0.65]** over 12 series / 68 days / 109,278 fills. Three
things worth keeping: (1) **adverse selection is exactly the size of the prize** —
fills split by how far the market traded through the bid, gentle 0-3c fills earn +7 to
+10c and the **45% that are swept 6c+ earn -7c**, blending to zero; this will kill the
next passive idea too. (2) **Size must never scale with liquidity** — fill size follows
the minute's volume and the biggest minutes ARE the sweeps, so the same rule measures
**-5.05c** uncapped and **+4.80c** capped at <=100 contracts: a parameter that inverts
the sign rather than shrinking the edge. (3) **Population closes weather and the
commodity dailies for any maker strategy** — 60-89% of weather minutes and 84-90% of
commodity-daily minutes contain NO trade at all, against 0.0% for the 15M series, so
there is nothing to be filled by. Also settled here: the mid's favourite-longshot bias
is real (+1.25c at 55-74c, cluster-bootstrapped) and harvestable by neither side; the
fee-parabola "widest moat at 50c" mechanism is **falsified** (45-54c is the one dead
band); and **resting orders do not improve late-certainty** — only 13% of live signals
can host a `bid+1` order at all, because the 90-93c spread is 1c, and those that can
net +1.33c against the taker's +1.51c. `research/search2/results/MAKER.md`

*Direction-neutral structures (Aug 15-17, all negative after fees):* complete-set
accumulator · matched-pair maker · cross-crypto relative value · market-neutral pairs ·
vertical/cross-strike arb · all-taker catalog scan · maker-then-hedge · WTI ladder
maker · liquidity-reward farming · hourly range-pin · one-touch barriers.

*15M series that are NOT live — all measured 2026-08-25 on live geometry, 56-68 days
(`research/search2/results/DEEP_SCAN.md`):* **KXNEAR15M -4.55c/ct [-6.19, -2.96]** and
**KXZEC15M -2.85c/ct [-4.36, -1.40]** are RELIABLY NEGATIVE — CIs exclude zero, first
per-trade numbers either has ever had; do not add them. HYPE -0.80c, GOLD -1.35c, both
CI-inclusive of zero. WTI +0.42c and SILVER +0.26c over their shorter windows, both
CI-inclusive of zero (Silver stays a late-September calendar item, not a finding).
Pooled, the six non-live series are **-2.22c, CI [-3.01, -1.42], P(>0)=0.000** against
the live six at +0.53c [-0.14, +1.18]. **The live series selection is correct, and that
is now measured rather than assumed.**

*FX / index — the "one genuinely untested corner" of the archetype filter, CLOSED
2026-08-25:* **KXINXU** (hourly, 40 strikes) PASSES the weather population screen —
10.5% of quotes in 88-96c, ~212 in-window/day vs 171-178 for BTC/ETH — and then
produces nothing: 90-93c is **-2.46c** at 150-600s and +0.12c at 10-30m, every CI
spanning zero, and the cells nearest live geometry are the negative ones. **KXEURUSD**
(daily) is -6c to -18c through 88-93c, winning 71-86% at prices implying 88-93%. The
structural reason is invariant 2: **median spread 3.0c on KXINXU and 4.0c on KXEURUSD
against 1.0c on crypto 15M** — two to three extra cents is the whole edge before any
question of edge arises. Cluster arithmetic also forbids a quick answer: KXINXU gives
126 close clusters in 25 days, KXEURUSD 46 in 64.

*Other markets:* new 15M series (HYPE, NEAR, Gold, Silver — numbers above) · weather
crossed-strike · cross-venue sports arb (**account is US-only** — this kills most
venue arb) · Kalshi vs sportsbook · sports-futures dominance · Fed complete set.

*Cross-listing 3-leg arb (range book vs threshold ladder):* **real and verified but
economically worthless** — 4 opportunities in 14 days worth **+$1.05 total** at 10
contracts, requiring a websocket service and non-atomic batch orders. Re-scan with
`PYTHONPATH=. python3 archive/research/market-structure/xlist_arb.py BTC ETH` before ever reconsidering.

Raw 2026-08-15/17 work: `~/Documents/Codex/2026-08-12/i-ran-a-full-ablation-study/work/`

---

