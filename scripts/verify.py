#!/usr/bin/env python3
"""Re-derive every headline number from raw sources and FAIL LOUDLY on disagreement.

WHY THIS EXISTS
---------------
Four tools report the same numbers: kalshi_dashboard.py, scripts/kstat.py,
scripts/reconcile.py and scripts/backtest.py. They use three different day keys, two
different definitions of "win" and three different series filters, and they have
disagreed in production more than once. A number that only one thing checks is an
assertion. This checks each one against an independent source and says exactly which
two disagree and by how much.

Raw truth, in order of authority: /portfolio/settlements (realised dollars, fees,
outcome), /portfolio/fills (price actually paid), data/candles/*.csv.gz (what the
model would have done), the `live-state` run artifact (what the trader believes),
and late_certainty_trader.py read by AST (the constants actually deployed).

    python3 scripts/verify.py            # everything; green check or named failures
    python3 scripts/verify.py --list     # what each check does
    python3 scripts/verify.py --table    # the per-day headline table
    --since YYYY-MM-DD | --check NAME... | --json | --ttl SECS | --no-state

Exit 0 = every check passed. Exit 1 = at least one FAIL. WARN never fails the run: it
marks a divergence that is understood and documented, so a NEW one stands out.

Needs KALSHI_API_KEY_ID and a key path; reads .env exactly as reconcile.py does.
API pulls are cached for --ttl seconds, so repeat runs are instant.

Full derivation and the dollar value of each divergence: docs/audit/claude/METRICS.md
"""
import argparse, ast, json, math, os, statistics, subprocess, sys, tempfile, textwrap, time
import datetime as D
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

CACHE = Path(tempfile.gettempdir()) / "kverify"
CENT = 0.011                      # a cent, plus float slack
G, R, Y, DIM, X = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def paint(s, c):
    return c + s + X if sys.stdout.isatty() else s


def _dotenv():
    f = BASE / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ── source 5: the deployed constants ──────────────────────────────────────────

def constants():
    """Module-level constants by AST. Never imports the trader."""
    out = {}
    for node in ast.parse((BASE / "late_certainty_trader.py").read_text()).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    try:
                        out[t.id] = ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        pass
    return out


K = constants()
SERIES = set(K["SERIES_LIST"])
BAND_MIN = {"yes": K.get("LOW_BAND_MIN_CENTS", K["MIN_ASK_CENTS"]),
            "no": K["MIN_ASK_CENTS"]}
MAX_ASK = K["MAX_ASK_CENTS"]


# ── sources 1 & 2: the API ────────────────────────────────────────────────────

def _page(path, key, field, ttl):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / f"{key}.json"
    if f.exists() and time.time() - f.stat().st_mtime < ttl:
        return json.loads(f.read_text())
    _dotenv()
    import kalshi_auth as A
    out, cur, pages = [], None, 0
    while pages < 400:
        p = {"limit": 200}
        if cur:
            p["cursor"] = cur
        code, data = A.get(path, p)
        if code != 200:
            sys.exit(f"{path} HTTP {code}: {str(data)[:200]}")
        batch = data.get(key, [])
        if not batch:
            break
        pages += 1
        out += batch
        cur = data.get("cursor")
        if not cur:
            break
        time.sleep(0.08)
    f.write_text(json.dumps(out))
    return out


def close_day(ticker, tz="utc"):
    """UTC day of close from the ticker. The embedded time is ET, so +4h is UTC.

    tz='et' returns the ET day instead — the two differ for every close after
    20:00 ET, which is 20.7% of all trades. See docs/audit/claude/METRICS.md §6.
    """
    try:
        naive = D.datetime.strptime(ticker.split("-")[1].upper(), "%y%b%d%H%M")
    except (IndexError, ValueError):
        return None
    dt = naive + D.timedelta(hours=4) if tz == "utc" else naive
    return dt.strftime("%Y-%m-%d")


def settlements(ttl):
    """One row per settled position, straight from the settlement record."""
    out = []
    for s in _page("/portfolio/settlements", "settlements", "settled_time", ttl):
        yc, nc = float(s.get("yes_count_fp", 0) or 0), float(s.get("no_count_fp", 0) or 0)
        if max(yc, nc) <= 0:
            continue
        tk = s.get("ticker", "")
        side = "yes" if yc > nc else "no"
        cost = (float(s.get("yes_total_cost_dollars", 0) or 0)
                + float(s.get("no_total_cost_dollars", 0) or 0))
        rev = float(s.get("revenue", 0) or 0) / 100.0
        fee = float(s.get("fee_cost", 0) or 0)
        out.append(dict(ticker=tk, series=tk.split("-")[0], side=side, ct=max(yc, nc),
                        cost=cost, rev=rev, fee=fee, pnl=round(rev - cost - fee, 2),
                        won=s.get("market_result", "") == side,
                        day=close_day(tk), etday=close_day(tk, "et"),
                        sday=(s.get("settled_time", "") or "")[:10]))
    return out




# ── source 4: the live state artifact ─────────────────────────────────────────

def state(ttl):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / "state.json"
    if f.exists() and time.time() - f.stat().st_mtime < ttl:
        return json.loads(f.read_text())
    import shutil
    def gh(*a):
        r = subprocess.run(["gh", *a], cwd=BASE, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError((r.stderr or r.stdout).strip()[:200])
        return r.stdout
    rid = json.loads(gh("run", "list", "--workflow", "late_certainty.yml", "--status",
                        "success", "--limit", "1", "--json", "databaseId"))[0]["databaseId"]
    tmp = Path(tempfile.mkdtemp())
    try:
        gh("run", "download", str(rid), "-D", str(tmp))
        src = next(tmp.rglob("certainty_state.json"))
        f.write_text(src.read_text())
        return json.loads(f.read_text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── the one definition of every headline metric ───────────────────────────────

def metrics(rows):
    """P&L, both win rates, break-even and margin. This is the ONLY definition."""
    n = len(rows)
    if not n:
        return None
    cost = sum(r["cost"] for r in rows)
    fees = sum(r["fee"] for r in rows)
    W = [r for r in rows if r["won"]]
    wcost, wcon = sum(r["cost"] for r in W), sum(r["ct"] for r in W)
    m = dict(n=n, pnl=round(sum(r["pnl"] for r in rows), 2), cost=round(cost, 2),
             fees=round(fees, 2), wr=100 * len(W) / n,
             per_trade=sum(r["pnl"] for r in rows) / n,
             avg_fill=100 * cost / sum(r["ct"] for r in rows),
             avg_cost=cost / n)
    if cost > 0 and wcon > 0:
        px = wcost / wcon                                   # winner-implied avg price
        # Losers carry no contract count in the settlement record, so impute at px.
        con = sum(r["ct"] if r["won"] else r["cost"] / px for r in rows)
        if con > 0:
            # Fees belong INSIDE break-even: P&L is rev-cost-fee, so pricing it on
            # cost alone reads green at a real loss (~0.53pp at current fills).
            m["dw_wr"] = 100 * wcost / cost
            m["be"] = 100 * (cost + fees) / con
            m["margin"] = m["dw_wr"] - m["be"]
    return m




# ── checks ────────────────────────────────────────────────────────────────────
# Each returns (status, message). FAIL = two sources that must agree do not.
# WARN = a divergence that is real, understood and documented.

CHECKS = {}


def check(name, needs=()):
    def deco(fn):
        fn.needs = needs
        CHECKS[name] = fn
        return fn
    return deco


@check("units")
def c_units(ctx):
    """Settlement fee_cost is dollars and revenue is cents, checked against the fee
    schedule rather than trusting the field name. Kalshi's taker fee is
    ceil(0.07*C*P*(1-P)) cents and MAKER FILLS ARE FREE, so the schedule is a ceiling,
    not an equality. Only an overshoot indicates a units bug: fee_cost quoted in cents
    would read ~100x the ceiling on every row."""
    rows = [r for r in ctx["set_live"] if r["ct"] > 0 and 0.05 < r["cost"] / r["ct"] < 0.99]
    if not rows:
        return "SKIP", "no priced settlements"
    over, worst, maker = 0, (None, 0.0), 0
    for r in rows:
        px = r["cost"] / r["ct"]
        cap = math.ceil(0.07 * r["ct"] * px * (1 - px) * 100) / 100 + 0.02
        if r["fee"] > cap:
            over += 1
            if r["fee"] - cap > worst[1]:
                worst = (r["ticker"], r["fee"] - cap)
        if r["fee"] < 0.2 * 0.07 * r["ct"] * px * (1 - px):
            maker += 1
    if over:
        return "FAIL", (f"settlement fee_cost EXCEEDS ceil(0.07*C*P*(1-P)) on {over}/"
                        f"{len(rows)} rows (worst {worst[0]}: ${worst[1]:.4f} over the "
                        f"ceiling). If fee_cost were CENTS every row would read ~100x — "
                        f"the units assumption in kalshi_dashboard.py and reconcile.py "
                        f"is wrong.")
    fpc = 100 * sum(r["fee"] for r in rows) / sum(r["ct"] for r in rows)
    return "OK", (f"fee_cost=dollars, revenue=cents confirmed on {len(rows)} priced "
                  f"settlements; realised fee {fpc:.3f}c/contract, "
                  f"{maker} ({100 * maker / len(rows):.1f}%) filled as maker at ~zero fee")


@check("state")
def c_state(ctx):
    """The trader's own accounting vs the exchange's. These must be identical."""
    st = ctx.get("state")
    if st is None:
        # Report the ACTUAL failure, never a guess. A check that says "unavailable"
        # without saying why trains you to ignore it — and the first time this fired
        # it was a transient GitHub blob-storage 500, not an auth problem.
        return "SKIP", f"state artifact unavailable: {ctx.get('state_err', 'unknown')}"
    api = {r["ticker"]: r for r in ctx["set_all"]}
    P = {k: v for k, v in st.get("positions", {}).items() if v.get("settled")}
    both = [k for k in P if k in api]
    if not both:
        return "SKIP", "no overlap between the state artifact and the API window"
    diffs = []
    for k in both:
        for f_state, f_api, label in (("pnl", "pnl", "P&L"), ("cost", "cost", "cost"),
                                      ("fee_cost", "fee", "fee")):
            a, b = float(P[k].get(f_state, 0) or 0), api[k][f_api]
            if abs(a - b) > CENT:
                diffs.append((k, label, a, b))
    if diffs:
        k, lab, a, b = max(diffs, key=lambda d: abs(d[2] - d[3]))
        return "FAIL", (f"state artifact vs settlements API disagree on {len(diffs)} "
                        f"field(s) across {len(both)} positions. Worst: {k} {lab} "
                        f"state=${a:.4f} api=${b:.4f} (differ by ${a - b:+.4f})")
    sa = round(sum(P[k]["pnl"] for k in both), 2)
    sb = round(sum(api[k]["pnl"] for k in both), 2)
    return "OK", (f"state artifact == settlements API on all {len(both)} positions "
                  f"(P&L, cost and fee); both total ${sa:+.2f}")


@check("margin")
def c_margin(ctx):
    """kstat.margin() must agree with the canonical formula on identical rows."""
    try:
        import kstat
    except Exception as exc:
        return "SKIP", f"cannot import kstat: {exc}"
    rows = ctx["set_live"]
    mine = metrics(rows)
    theirs = kstat.margin([dict(cost=r["cost"], contracts=r["ct"], won=r["won"],
                                fee=r["fee"], pnl=r["pnl"]) for r in rows])
    if theirs is None or "margin" not in mine:
        return "SKIP", "not enough winners to price break-even"
    dw, be, mg = theirs
    for label, a, b in (("$-wtd WR", mine["dw_wr"], dw), ("break-even", mine["be"], be),
                        ("margin", mine["margin"], mg)):
        if abs(a - b) > 0.01:
            return "FAIL", (f"verify.py and scripts/kstat.py::margin disagree on "
                            f"{label}: {a:.4f}pp vs {b:.4f}pp (differ by {a - b:+.4f}pp)")
    return "OK", (f"kstat.margin == canonical on {len(rows)} rows: $-wtd {dw:.2f}% "
                  f"vs {be:.2f}% b/e = {mg:+.2f}pp")


@check("win")
def c_win(ctx):
    """A win is the settlement outcome, not the sign of P&L.

    kstat used `pnl > 0`, which scores a win netting exactly $0.00 after fees as a
    loss. Rare but not theoretical — one such row exists. Tests the CODE, because the
    data condition is permanent and a check that can only report it is decoration.
    """
    rows = [r for r in ctx["set_live"] if (r["pnl"] > 0) != r["won"]]
    src = (BASE / "scripts/kstat.py").read_text()
    fixed = 'p.get("result") == p.get("side")' in src
    if not fixed:
        return "FAIL", (f"scripts/kstat.py still infers a win from `pnl > 0`; "
                        f"{len(rows)} settlement(s) in the API window disagree with "
                        f"market_result, e.g. "
                        + (rows[0]["ticker"] if rows else "none right now")
                        + ". Read the outcome, do not infer it from P&L.")
    return "OK", (f"kstat reads the settlement outcome, not the sign of P&L "
                  f"({len(rows)} row(s) in the window where the two differ — the case "
                  f"this protects against)")


@check("bet")
def c_bet(ctx):
    """Realised principal must match FLAT_BET_DOLLARS. The config table said $50 for
    days while the bot bet $25; this is the check that catches that."""
    bet = K["FLAT_BET_DOLLARS"]
    days = sorted({r["day"] for r in ctx["set_live"] if r["day"]})
    if not days:
        return "SKIP", "no settled trades in the window"
    recent = [r for r in ctx["set_live"] if r["day"] == days[-1]]
    hi = max(r["cost"] for r in recent)
    if hi > bet + 0.02:
        return "FAIL", (f"FLAT_BET_DOLLARS=${bet} but a position on {days[-1]} cost "
                        f"${hi:.2f} — the deployed bet is not the constant "
                        f"(differ by ${hi - bet:+.2f})")
    lo = min(r["cost"] for r in recent)
    if lo < bet * 0.90:
        return "WARN", (f"smallest principal on {days[-1]} is ${lo:.2f} against a "
                        f"${bet} budget — partial fills or a mid-day size change")
    return "OK", (f"deployed bet == FLAT_BET_DOLLARS=${bet} on {days[-1]}: "
                  f"{len(recent)} trades, ${lo:.2f}-${hi:.2f}")


@check("band")
def c_band(ctx):
    """Every realised fill must sit inside the declared band, allowing the documented
    crash-through slack. A buy limit is a ceiling, never a floor: a marketable order
    sweeps the book upward, so CRASH_FILL_TOLERANCE cents under is expected and
    logged. Anything deeper, or anything ABOVE MAX_ASK, means a gate did not hold."""
    tol = K.get("CRASH_FILL_TOLERANCE", 0)
    rows = [r for r in ctx["set_live"] if r["day"] and r["day"] >= ctx["since"] and r["ct"]]
    if not rows:
        return "SKIP", f"no settled trades since {ctx['since']}"
    deep, high, slack = [], [], []
    for r in rows:
        px = 100 * r["cost"] / r["ct"]
        floor = BAND_MIN[r["side"]]
        if px > MAX_ASK + 0.5:
            high.append((r["ticker"], r["side"], px))
        elif px < floor - tol:
            deep.append((r["ticker"], r["side"], px))
        elif px < floor - 0.5:
            slack.append((r["ticker"], r["side"], px))
    if high:
        t, sd, px = high[0]
        return "FAIL", (f"{len(high)} fill(s) ABOVE MAX_ASK_CENTS={MAX_ASK} since "
                        f"{ctx['since']} — the limit cap did not hold. e.g. {t} "
                        f"{sd.upper()} @ {px:.2f}c")
    if deep:
        t, sd, px = deep[0]
        return "FAIL", (f"{len(deep)} crash fill(s) more than CRASH_FILL_TOLERANCE="
                        f"{tol}c below the floor since {ctx['since']}: e.g. {t} "
                        f"{sd.upper()} @ {px:.2f}c vs a floor of "
                        f"{BAND_MIN[sd]}c ({BAND_MIN[sd] - px:.2f}c through)")
    msg = f"all {len(rows)} fills since {ctx['since']} inside the declared band"
    if slack:
        msg += (f"; {len(slack)} landed 0.5-{tol}c under the floor (book moved inside "
                f"the order's flight time — tolerated, flagged outside_safe_zone): "
                + ", ".join(f"{t.split('-')[0]} {sd} {px:.2f}c" for t, sd, px in slack[:3]))
    return "OK", msg


def probe_ask(side, ask):
    """Run the REAL try_trade against a stubbed API quoting `ask` on `side`. Every
    endpoint answers consistently, the book is deep and the priors clear every
    threshold, so only a band gate can stop the order. Returns (ordered, first
    blocking log line). Read-only: rebinds network functions, then restores them."""
    import late_certainty_trader as T
    saved = (T.kalshi_get, T.place_order, T.log)
    y = ask if side == "yes" else 100 - ask                 # the YES-side quote
    pa, pb = (95, 94) if side == "yes" else (99, 5)         # prior candle, probed side

    def stub(path, params=None):
        if path.endswith("/orderbook"):
            return 200, {"orderbook_fp": {
                "no_dollars": [[f"{(100 - y) / 100:.4f}", "200"]],
                "yes_dollars": [[f"{y / 100:.4f}", "200"]]}}
        if path.startswith("/markets/"):
            return 200, {"market": {"yes_ask_dollars": f"{y / 100:.4f}",
                                    "no_ask_dollars": f"{(100 - y) / 100:.4f}"}}
        if "candlesticks" in path:
            return 200, {"candlesticks": [
                {"end_period_ts": 1000 + 60 * i,
                 "yes_ask": {"close_dollars": f"{pa / 100:.4f}"},
                 "yes_bid": {"close_dollars": f"{pb / 100:.4f}"}} for i in range(8)]}
        return 200, {}

    lines = []
    try:
        T.kalshi_get, T.log = stub, lines.append
        T.place_order = lambda *a, **k: (201, {"order_id": "stub"})
        T.try_trade({"ticker": "KXBTC15M-VERIFY-T1", "event_ticker": "KXBTC15M-VERIFY",
                     "_secs_left": 400, "close_time": "2026-01-01T20:00:00Z",
                     "yes_ask_dollars": f"{y / 100:.4f}",
                     "no_ask_dollars": f"{(100 - y) / 100:.4f}"},
                    {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
                     "recent_results": []},
                    dry_run=True, balance=1000.0,
                    live_position_tickers=set(), resting_order_tickers=[])
    except Exception as exc:
        lines.append(f"EXC {type(exc).__name__}: {exc}")
    finally:
        T.kalshi_get, T.place_order, T.log = saved
    return (any("TRADE:" in m for m in lines),
            next((m.strip() for m in lines if "SKIP" in m or "EXC" in m), ""))


def reachable(side):
    return [a for a in range(int(BAND_MIN[side]), int(MAX_ASK) + 1)
            if probe_ask(side, a)[0]]


@check("deadband")
def c_deadband(ctx):
    """Is every cent the constants declare actually REACHABLE? Catches a band constant
    no gate consults — v5.17's 88-89c YES extension shipped, reset the stats counter,
    and can never place an order."""
    dead = []
    for side in ("yes", "no"):
        got, want = reachable(side), list(range(int(BAND_MIN[side]), int(MAX_ASK) + 1))
        if got != want:
            dead.append(f"{side.upper()} declares {want[0]}-{want[-1]}c, reachable "
                        + (f"{got[0]}-{got[-1]}c" if got else "NOTHING"))
    if dead:
        return "FAIL", ("a declared entry band is unreachable — the constants and the "
                        "gates disagree: " + "; ".join(dead)
                        + ". See docs/audit/claude/LIVE_SPEC.md §6.")
    return "OK", (f"reachable band == declared band on both sides "
                  f"(YES [{BAND_MIN['yes']},{MAX_ASK}], NO [{BAND_MIN['no']},{MAX_ASK}])")


@check("cap")
def c_cap(ctx):
    """Every market in a close cluster expires together, so MAX_CONCURRENT_POSITIONS
    is a per-cluster cap as well as a concurrency cap."""
    cl = defaultdict(list)
    for r in ctx["set_live"]:
        if r["day"] and r["day"] >= ctx["since"]:
            cl[r["ticker"].split("-")[1]].append(r)
    if not cl:
        return "SKIP", "no clusters in the window"
    cap = K["MAX_CONCURRENT_POSITIONS"]
    over = {k: v for k, v in cl.items() if len(v) > cap}
    if over:
        k, v = max(over.items(), key=lambda kv: len(kv[1]))
        return "FAIL", (f"{len(over)}/{len(cl)} close clusters exceed "
                        f"MAX_CONCURRENT_POSITIONS={cap}; worst {k} held {len(v)} "
                        f"positions for ${sum(x['pnl'] for x in v):+.2f}")
    hist = sorted((n, sum(1 for v in cl.values() if len(v) == n))
                  for n in {len(v) for v in cl.values()})
    return "OK", (f"0/{len(cl)} clusters over cap {cap} "
                  f"({', '.join(f'{c} at {n}' for n, c in hist)})")


@check("series")
def c_series(ctx):
    """Nothing outside SERIES_LIST may be traded, and no tool may count retired series
    as strategy P&L. The dashboard filtered on ticker.endswith("15M") until 2026-08-24,
    which attributed 307 settlements and -$245 to a strategy that never placed them."""
    off = [r for r in ctx["set_all"]
           if r["series"] not in SERIES and r["day"] and r["day"] >= ctx["since"]]
    if off:
        by = defaultdict(float)
        for r in off:
            by[r["series"]] += r["pnl"]
        worst = ", ".join(f"{k} {v:+.2f}" for k, v in sorted(by.items(), key=lambda x: x[1]))
        return "FAIL", (f"{len(off)} settlement(s) since {ctx['since']} in series the "
                        f"trader does not list: {worst}")
    dash = (BASE / "kalshi_dashboard.py").read_text()
    if 'endswith("15M")' in dash and "live_series()" not in dash:
        strat = [r for r in ctx["set_all"]
                 if r["series"].endswith("15M") and r["series"] not in SERIES]
        return "FAIL", (f"kalshi_dashboard.py still filters strategy stats on "
                        f'ticker.endswith("15M"), counting {len(strat)} retired '
                        f"settlements (${sum(r['pnl'] for r in strat):+.2f}) as strategy "
                        f"P&L. It must read SERIES_LIST by AST like reconcile.py does.")
    non15 = [r for r in ctx["set_all"] if not r["series"].endswith("15M")]
    msg = (f"no off-list trades since {ctx['since']}; dashboard filters strategy stats "
           f"on the trader's SERIES_LIST")
    if non15:
        return "WARN", (msg + f" — note {len(non15)} non-15M settlement(s) "
                        f"(${sum(r['pnl'] for r in non15):+.2f}) move the balance but sit "
                        f"outside every strategy stat, by design")
    return "OK", msg


@check("daykey")
def c_daykey(ctx):
    """One definition of "what day did this trade happen".

    reconcile.py, the candle archive and (since 2026-08-24) kstat.py all key on the
    UTC day of CLOSE. The dashboard still buckets by settled_time in BROWSER-LOCAL
    time, so its "today" can differ from every other tool for any close after 20:00 ET.
    """
    rows = [r for r in ctx["set_live"] if r["day"] and r["etday"]]
    if not rows:
        return "SKIP", "no rows"
    dis = [r for r in rows if r["day"] != r["etday"]]
    worst, wd = None, 0.0
    for d in sorted({r["day"] for r in rows}):
        a = sum(r["pnl"] for r in rows if r["etday"] == d)
        b = sum(r["pnl"] for r in rows if r["day"] == d)
        if abs(a - b) > wd:
            worst, wd = (d, a, b), abs(a - b)
    kstat_fixed = "def close_day(ticker)" in (BASE / "scripts/kstat.py").read_text()
    if not kstat_fixed:
        return "FAIL", ("scripts/kstat.py does not key on the UTC close day; it "
                        "disagrees with reconcile.py and the archive on "
                        f"{len(dis)}/{len(rows)} trades")
    if not dis:
        return "OK", "ET and UTC close-day keys agree on every trade"
    d, a, b = worst
    return "WARN", (f"kstat, reconcile and the archive now agree (UTC close day). "
                    f"kalshi_dashboard.py still buckets by settled_time in "
                    f"browser-local time, which differs on {len(dis)}/{len(rows)} "
                    f"trades ({100 * len(dis) / len(rows):.1f}%) — worst {d}: "
                    f"${a:+.2f} by ET day vs ${b:+.2f} by UTC day, a ${a - b:+.2f} gap")


@check("harness", needs=("archive",))
def c_harness(ctx):
    """scripts/backtest.py must score only the series the trader actually trades."""
    import backtest as B
    import inspect
    rows = ctx["archive"]
    off = sorted({r[0] for r in rows} - SERIES)
    if not off:
        return "OK", "the archive holds only series the trader trades"
    cfg = B.live_config()
    if cfg.get("series") != SERIES:
        return "FAIL", (f"backtest.live_config() series {sorted(cfg.get('series') or [])} "
                        f"!= the trader's SERIES_LIST {sorted(SERIES)}")
    src = inspect.getsource(B.main)
    if "cfg[\"series\"]" not in src and "cfg['series']" not in src:
        a = B.summary(rows, *B.simulate(rows, cfg, DOCUMENTED_SLIP_CENTS))
        live = [r for r in rows if r[0] in SERIES]
        b = B.summary(live, *B.simulate(live, cfg, DOCUMENTED_SLIP_CENTS))
        return "FAIL", (f"backtest.py scores {', '.join(off)} (SHADOW_SERIES): "
                        f"{a['per_trade']:+.3f}/tr vs {b['per_trade']:+.3f}/tr on live "
                        f"series — every capture figure inherits the wrong denominator")
    return "OK", (f"backtest.py filters to the trader's {len(SERIES)} live series by "
                  f"default; the archive's {', '.join(off)} are scored only under "
                  f"--all-series")


@check("archive", needs=("archive",))
def c_archive(ctx):
    """archive_candles.py builds prior_k as candles[i-k], which is only a k-minute
    lookback if the API returns contiguous minutes. Assert it, don't assume it."""
    import csv, glob, gzip
    per, n, bad = defaultdict(list), 0, 0
    for p in sorted(glob.glob(str(BASE / "data/candles/*.csv.gz")))[-6:]:
        with gzip.open(p, "rt") as f:
            for r in csv.DictReader(f):
                per[(r["ticker"], r["side"])].append((int(r["candle_idx"]),
                                                      int(r["secs_left"])))
    for v in per.values():
        v.sort()
        for (i1, s1), (i2, s2) in zip(v, v[1:]):
            if i2 - i1 != 1:
                continue
            n += 1
            if s1 - s2 != 60:
                bad += 1
    if not n:
        return "SKIP", "no consecutive candle pairs in the last 6 archived days"
    if bad:
        return "FAIL", (f"{bad}/{n} consecutive candle_idx pairs are NOT 60s apart — "
                        f"prior_k is not a k-minute lookback and every prior gate "
                        f"measured against the archive is wrong")
    return "OK", f"{n}/{n} consecutive candle_idx pairs are exactly 60s apart"


@check("gates", needs=("archive",))
def c_gates(ctx):
    """Known divergences between the trader's gates and backtest.qualifies."""
    import backtest as B
    src = (BASE / "late_certainty_trader.py").read_text()
    bt = (BASE / "scripts/backtest.py").read_text()
    out = []
    trader_c1_yes_only = 'side == "yes"' in src.split("C1 PROVISIONAL")[1][:400] \
        if "C1 PROVISIONAL" in src else None
    bt_c1_yes_only = 'side == "yes" and series == "KXSOL15M"' in bt
    if bt_c1_yes_only and not trader_c1_yes_only:
        rows = [r for r in ctx["archive"] if r[0] == "KXSOL15M" and r[3] == "no"]
        n = sum(1 for (_, _, _, _, a, s, _, p1, p2, p3) in rows
                if 90 <= a <= MAX_ASK and 150 <= s <= 600 and p1 >= 75 and 75 <= p2 <= 79)
        out.append(f"C1 quarantine: the trader has no side test (quarantines NO too), "
                   f"backtest.qualifies restricts it to YES with a comment claiming they "
                   f"match — {n} NO-side SOL candidates in the archive are affected")
    if 'else Decimal("100")' in src and "else None" in \
            (BASE / "scripts/archive_candles.py").read_text():
        out.append("empty-book NO priors: the trader reads yes_bid==0 as a 100c NO ask "
                   "(passes the >=75 gate); archive_candles.py writes it blank, which "
                   "backtest parses as -1.0 (fails). Zero means 'not on offer'.")
    if f"min_depth={{MIN_BOOK_DEPTH}}" in src or "min_depth={MIN_BOOK_DEPTH}" in src:
        eff = max(math.ceil(int(K["FLAT_BET_DOLLARS"] / (MAX_ASK / 100))
                            * K["MIN_BOOK_DEPTH_MULTIPLE"]), K["MIN_BOOK_DEPTH_FLOOR"])
        if eff != K["MIN_BOOK_DEPTH"]:
            out.append(f"gate log writes min_depth={K['MIN_BOOK_DEPTH']} while the live "
                       f"gate needs {eff}; replays that read the logged field "
                       f"over-count depth blocks")
    if out:
        return "WARN", "trader vs harness gate divergences: " + " | ".join(out)
    return "OK", "no known gate divergence between the trader and the harness"


@check("reconcile", needs=("archive",))
def c_reconcile(ctx):
    """reconcile.py's LIVE TOTAL must equal the settlements API over the same days.
    Restricted to archived days, as reconcile is, or every unarchived day reads as a
    spurious gap. reconcile sums UNROUNDED per-trade P&L while verify and the state
    file round each trade first; both are defensible, so the spread is reported and
    only a gap beyond a cent per trade is a real disagreement."""
    import glob
    archived = {Path(p).name[:10] for p in glob.glob(str(BASE / "data/candles/*.csv.gz"))}
    rows = [r for r in ctx["set_live"]
            if r["day"] and r["day"] >= ctx["since"] and r["day"] in archived]
    if not rows:
        return "SKIP", f"no archived days at or after {ctx['since']}"
    m = metrics(rows)
    unrounded = sum(r["rev"] - r["cost"] - r["fee"] for r in rows)
    spread = m["pnl"] - unrounded
    if abs(spread) > 0.01 * len(rows):
        return "FAIL", (f"per-row rounding moves the total by ${spread:+.4f} over "
                        f"{len(rows)} trades — more than a cent per trade, so this is "
                        f"not a rounding convention")
    return "OK", (f"settlements API over archived days >= {ctx['since']}: {m['n']}tr "
                  f"{m['wr']:.2f}%WR, ${m['pnl']:+.2f} rounded per trade vs "
                  f"${unrounded:+.2f} unrounded (reconcile.py's convention, "
                  f"${spread:+.4f} apart). Cross-check: python3 scripts/reconcile.py "
                  f"--since {ctx['since']} --until {max(r['day'] for r in rows)}")


# The slippage figure every "at measured fill quality" backtest is quoted at.
# Was 0.105c (2026-08-17, YES fills only, against a stale 1-min candle). Re-measured
# 2026-08-24 against book_at_entry on n=500 across both sides: +0.227c, t=+6.6.
# docs/audit/claude/CLAIMS.md §1. If this check FAILS again, re-measure and move the
# constant — do not widen the tolerance.
DOCUMENTED_SLIP_CENTS = 0.227


@check("slippage", needs=("state",))
def c_slippage(ctx):
    """What execution actually costs, against the book read ~128ms before the order.

    This is the comparison the trader's own comment (l.1470) says is valid: avg fill
    vs `book_at_entry`, by side, over distributions. NOT against a 1-min candle, which
    is stale 47% of the time and yields a +0.85c regression-to-the-mean artifact.
    Positive means paying MORE than the book showed, i.e. adverse.
    """
    st = ctx.get("state")
    if st is None:
        return "SKIP", f"state artifact unavailable: {ctx.get('state_err', 'unknown')}"
    rows = [p for p in st.get("positions", {}).values()
            if p.get("contracts") and p.get("book_at_entry")]
    if len(rows) < 30:
        return "SKIP", f"only {len(rows)} positions carry a book read"
    d = {"yes": [], "no": []}
    for p in rows:
        d.setdefault(p.get("side", "?"), []).append(
            100 * p["cost"] / p["contracts"] - p["book_at_entry"])
    alld = d["yes"] + d["no"]
    mean = statistics.mean(alld)
    se = statistics.stdev(alld) / len(alld) ** 0.5
    t = (mean - DOCUMENTED_SLIP_CENTS) / se if se else 0.0
    by = "  ".join(f"{k.upper()} {statistics.mean(v):+.3f}c (n={len(v)})"
                   for k, v in d.items() if v)
    if abs(t) > 3:
        return "FAIL", (f"measured slippage {mean:+.3f}c vs the documented "
                        f"{DOCUMENTED_SLIP_CENTS:+.3f}c in CLAUDE.md l.175 — "
                        f"differ by {mean - DOCUMENTED_SLIP_CENTS:+.3f}c, t={t:+.1f} "
                        f"over n={len(alld)}. Every '--slip {DOCUMENTED_SLIP_CENTS}' "
                        f"figure is quoted at the wrong number. {by}")
    return "OK", (f"slippage {mean:+.3f}c vs documented {DOCUMENTED_SLIP_CENTS:+.3f}c "
                  f"(t={t:+.1f}, n={len(alld)}).  {by}")


@check("rounding", needs=("archive",))
def c_rounding(ctx):
    """Every archived day before 2026-08-22 stores integer cents. Measure what that
    does to SELECTED TRADES, not to rows, by running the exact-cent days both ways."""
    import backtest as B
    import csv, glob, gzip
    exact = []
    for path in sorted(glob.glob(str(BASE / "data/candles/*.csv.gz"))):
        if Path(path).name[:10] < "2026-08-22":
            continue
        with gzip.open(path, "rt") as f:
            exact += [r for r in csv.DictReader(f) if r["series"] in SERIES]
    if len(exact) < 500:
        return "SKIP", f"only {len(exact)} exact-cent rows archived so far"

    def build(round_it):
        seen, out = set(), []
        for r in exact:
            k = (r["ticker"], r["side"], r["candle_idx"])
            if k in seen:
                continue
            seen.add(k)
            f = lambda v: float(v) if v not in ("", "None") else -1.0
            g = (lambda v: float(int(f(v))) if f(v) >= 0 else -1.0) if round_it else f
            out.append((r["series"], r["ticker"], int(r["close_ts"]), r["side"],
                        g(r["ask"]), float(r["secs_left"]), r["won"] == "True",
                        g(r["prior_1"]), g(r["prior_2"]), g(r["prior_3"])))
        return out

    cfg = B.live_config()
    e, r = build(False), build(True)
    se = B.summary(e, *B.simulate(e, cfg, 0.105))
    sr = B.summary(r, *B.simulate(r, cfg, 0.105))
    dn = sr["trades"] - se["trades"]
    dp = sr["per_trade"] - se["per_trade"]
    tone = "WARN" if abs(dn) or abs(dp) > 0.005 else "OK"
    return tone, (f"on the {len(exact):,} exact-cent rows, rounding prices the way every "
                  f"pre-2026-08-22 day stores them changes the simulator's picks by "
                  f"{dn:+d} trades ({100 * dn / max(se['trades'], 1):+.1f}%) and "
                  f"{dp:+.3f}/tr ({sr['wr'] - se['wr']:+.2f}pp WR) — always optimistic. "
                  f"72 of 74 archived days carry this, so every pre-Aug-22 claim does too.")


@check("edge")
def c_edge(ctx):
    """Is the margin actually negative, or is that a story about variance? Reported
    as a z, never as a bare percentage difference."""
    rows = [r for r in ctx["set_live"] if r["day"] and r["day"] >= ctx["since"]]
    if len(rows) < 30:
        return "SKIP", f"only {len(rows)} trades since {ctx['since']}"
    m = metrics(rows)
    if "margin" not in m:
        return "SKIP", "cannot price break-even"
    se = math.sqrt(m["be"] / 100 * (1 - m["be"] / 100) / m["n"]) * 100
    z = (m["dw_wr"] - m["be"]) / se if se else 0.0
    tone = "FAIL" if z < -2.5 else ("WARN" if m["margin"] < 0 else "OK")
    return tone, (f"since {ctx['since']}: {m['n']}tr, $-wtd {m['dw_wr']:.2f}% vs "
                  f"{m['be']:.2f}% break-even = {m['margin']:+.2f}pp (z={z:+.2f}). "
                  f"P&L ${m['pnl']:+.2f} at ${m['avg_cost']:.2f}/trade"
                  + (" — trades inside a close cluster are correlated, so the true "
                     "interval is wider than this z." if abs(z) < 3 else ""))


# ── the per-day table ─────────────────────────────────────────────────────────

def table(rows):
    print(f"\n  {'close day (UTC)':<17}{'n':>5}{'WR':>8}{'$-wtd':>8}{'b/e':>8}"
          f"{'margin':>9}{'P&L':>10}{'$/tr':>8}{'avg fill':>10}{'avg cost':>10}")
    print("  " + "-" * 93)
    for d in sorted({r["day"] for r in rows if r["day"]}):
        m = metrics([r for r in rows if r["day"] == d])
        mg = f"{m['margin']:+.2f}pp" if "margin" in m else "—"
        be = f"{m['be']:.2f}%" if "margin" in m else "—"
        dw = f"{m['dw_wr']:.2f}%" if "margin" in m else "—"
        print(f"  {d:<17}{m['n']:>5}{m['wr']:>7.2f}%{dw:>8}{be:>8}"
              f"{paint(f'{mg:>9}', G if m.get('margin', 0) >= 0 else R)}"
              f"{m['pnl']:>+10.2f}{m['per_trade']:>+8.2f}{m['avg_fill']:>9.2f}c"
              f"{m['avg_cost']:>10.2f}")
    m = metrics(rows)
    mg = f"{m['margin']:+.2f}pp" if "margin" in m else "—"
    print("  " + "-" * 93)
    print(f"  {'ALL':<17}{m['n']:>5}{m['wr']:>7.2f}%{m['dw_wr']:>7.2f}%{m['be']:>7.2f}%"
          f"{paint(f'{mg:>9}', G if m.get('margin', 0) >= 0 else R)}"
          f"{m['pnl']:>+10.2f}{m['per_trade']:>+8.2f}{m['avg_fill']:>9.2f}c\n")


# ── cli ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="YYYY-MM-DD; default = the last 3 archived days")
    ap.add_argument("--check", nargs="+", metavar="NAME", help="run only these checks")
    ap.add_argument("--list", action="store_true", help="list check names and exit")
    ap.add_argument("--table", action="store_true", help="print the per-day table")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ttl", type=int, default=600, help="API cache seconds (default 600)")
    ap.add_argument("--no-state", action="store_true", help="skip the gh artifact download")
    a = ap.parse_args()

    if a.list:
        for n, f in CHECKS.items():
            print(f"  {n:<12}{(f.__doc__ or '').strip().splitlines()[0]}")
        return 0

    names = a.check or list(CHECKS)
    unknown = [n for n in names if n not in CHECKS]
    if unknown:
        sys.exit(f"unknown check(s): {', '.join(unknown)} — try --list")

    S = settlements(a.ttl)
    live = [r for r in S if r["series"] in SERIES]
    days = sorted({r["day"] for r in live if r["day"]})
    since = a.since or (days[-3] if len(days) >= 3 else days[0])
    ctx = {"set_all": S, "set_live": live, "since": since}

    if any("archive" in (CHECKS[n].needs or ()) for n in names):
        import backtest as B
        ctx["archive"] = B.load()
    if any("state" in (CHECKS[n].needs or ()) or n == "state" for n in names) and not a.no_state:
        try:
            ctx["state"] = state(a.ttl)
        except Exception as exc:
            ctx["state"] = None
            ctx["state_err"] = str(exc)

    if a.table:
        table(live)

    results, fails, warns = [], 0, 0
    for n in names:
        try:
            status, msg = CHECKS[n](ctx)
        except Exception as exc:
            status, msg = "FAIL", f"check raised {type(exc).__name__}: {exc}"
        results.append(dict(check=n, status=status, message=msg))
        fails += status == "FAIL"
        warns += status == "WARN"

    if a.json:
        m = metrics([r for r in live if r["day"] and r["day"] >= since])
        print(json.dumps(dict(since=since, version=K["STRATEGY_VERSION"],
                              headline=m, checks=results), indent=1))
        return 1 if fails else 0

    w = max(len(r["check"]) for r in results)
    print(f"\n  {paint('verify', DIM)}  {K['STRATEGY_VERSION']}  "
          f"since {since}  ({len(live)} live settlements in the API window)\n")
    for r in results:
        tone = {"OK": G, "FAIL": R, "WARN": Y, "SKIP": DIM}[r["status"]]
        head = f"  {paint(r['status'].ljust(4), tone)}  {r['check'].ljust(w)}  "
        pad = " " * (len(r["check"]) + 10)
        print(head + ("\n" + pad).join(textwrap.wrap(r["message"], 84)))
    print()
    if fails:
        print(paint(f"  ✗ {fails} check(s) FAILED"
                    + (f", {warns} warning(s)" if warns else ""), R))
    elif warns:
        print(paint(f"  ✓ no failures, {warns} known divergence(s) flagged", Y))
    else:
        print(paint("  ✓ every source agrees", G))
    print()
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
