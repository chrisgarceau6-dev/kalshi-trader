#!/usr/bin/env python3
"""Canonical backtest for the late-certainty strategy.

WHY THIS EXISTS
---------------
Claims about this strategy used to live in CLAUDE.md as prose with numbers
attached — "NO side is -EV", "two NOs per cluster is -$1,211", "C1 is -$7.25/tr".
Nobody could re-run them, so they were unfalsifiable, and several turned out to be
badly wrong after months of steering decisions. A number with no command behind it
is an assertion, not evidence.

Every strategy claim should now be a command. If you cannot reproduce a number with
this script, treat the number as unverified regardless of who wrote it down.

DATA
----
data/candles/*.csv.gz — one file per UTC day, written nightly by
scripts/archive_candles.py and backfilled to 2026-06-11 (Kalshi's retention floor).
The archive only grows, so out-of-sample windows get better over time. Use
--since/--until to hold out a period a hypothesis was NOT formed on.

LIVE CONFIG
-----------
Defaults are read directly out of late_certainty_trader.py by AST (no import, no
side effects), so this harness cannot silently drift from what is actually running.

EXAMPLES
--------
  python3 scripts/backtest.py                          # live config, full history
  python3 scripts/backtest.py --set yes_only=1         # YES-only comparison
  python3 scripts/backtest.py --compare yes_only=1     # baseline vs variant + CI
  python3 scripts/backtest.py --sweep max_ask 92 93 94 95 96
  python3 scripts/backtest.py --slip 1                 # one tick of adverse fill
  python3 scripts/backtest.py --since 2026-08-01       # holdout window
"""
import argparse
import ast
import csv
import glob
import gzip
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADER = os.path.join(ROOT, "late_certainty_trader.py")
DATA = os.path.join(ROOT, "data", "candles")

FEE = 0.07          # modelled as 7% of gross win; Kalshi's actual is ~6.3-6.5%
                    # (0.07*C*P*(1-P)), so this is mildly conservative.


# ── live config, read without executing the trader ────────────────────────────

def live_config():
    """Pull module-level constants out of the trader by AST. Never imports it."""
    want = {"MIN_ASK_CENTS", "MAX_ASK_CENTS", "MIN_SECS_LEFT", "MAX_SECS_LEFT",
            "PRIOR_MIN_CENTS", "PRIOR_LOOKBACK", "YES_ONLY",
            "MAX_CONCURRENT_POSITIONS", "FLAT_BET_DOLLARS", "STRATEGY_VERSION"}
    out = {}
    tree = ast.parse(open(TRADER).read())
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in want:
                try:
                    out[t.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    missing = want - set(out)
    if missing:
        print(f"WARNING: could not read {sorted(missing)} from the trader; "
              f"using fallbacks", file=sys.stderr)
    return dict(
        min_ask=out.get("MIN_ASK_CENTS", 90),
        max_ask=out.get("MAX_ASK_CENTS", 93),
        min_secs=out.get("MIN_SECS_LEFT", 150),
        max_secs=out.get("MAX_SECS_LEFT", 600),
        prior_min=out.get("PRIOR_MIN_CENTS", 75),
        lookback=out.get("PRIOR_LOOKBACK", 2),
        yes_only=int(bool(out.get("YES_ONLY", False))),
        max_conc=out.get("MAX_CONCURRENT_POSITIONS", 2),
        bet=float(out.get("FLAT_BET_DOLLARS", 75)),
        # Not constants in the trader — encoded here, override with --set.
        p3_gate=1,      # ask<=91 requires 3rd prior >= p3_min
        p3_min=80,
        c1=1,           # KXSOL15M + prior2 75-79c quarantine (YES side)
        version=out.get("STRATEGY_VERSION", "?"),
    )


# ── data ──────────────────────────────────────────────────────────────────────

def load(since=None, until=None):
    files = sorted(glob.glob(os.path.join(DATA, "*.csv.gz")))
    if not files:
        sys.exit(f"no data in {DATA} — run scripts/archive_candles.py --backfill N")
    # Prices are EXACT cents and may carry decimals (Kalshi quotes sub-cent; a real
    # ask of 93.30c is not 93c). Days archived before 2026-08-22 hold integer cents,
    # which parse identically through float(), so old and new files mix safely.
    ip = lambda v: float(v) if v not in ("", "None") else -1.0
    rows, seen = [], set()
    for path in files:
        day = os.path.basename(path)[:10]
        if (since and day < since) or (until and day > until):
            continue
        with gzip.open(path, "rt") as f:
            for r in csv.DictReader(f):
                k = (r["ticker"], r["side"], r["candle_idx"])
                if k in seen:
                    continue
                seen.add(k)
                rows.append((r["series"], r["ticker"], int(r["close_ts"]), r["side"],
                             float(r["ask"]), float(r["secs_left"]), r["won"] == "True",
                             ip(r["prior_1"]), ip(r["prior_2"]), ip(r["prior_3"])))
    if not rows:
        sys.exit("no rows in the requested window")
    return rows


# ── simulation ────────────────────────────────────────────────────────────────

def pnl(won, ask, bet, slip):
    price = min(ask + slip, 99.0)
    contracts = bet / (price / 100.0)
    return contracts * (1 - price / 100.0) * (1 - FEE) if won else -bet


def qualifies(cfg, series, side, ask, secs, p1, p2, p3):
    if not (cfg["min_ask"] <= ask <= cfg["max_ask"]):
        return False
    if not (cfg["min_secs"] <= secs <= cfg["max_secs"]):
        return False
    priors = [p1, p2, p3][:cfg["lookback"]]
    if any(p < cfg["prior_min"] for p in priors):
        return False
    if cfg["p3_gate"] and ask <= 91 and p3 < cfg["p3_min"]:
        return False
    if side == "no" and cfg["yes_only"]:
        return False
    # C1 quarantine applies to the YES side only, matching the live trader.
    if cfg["c1"] and side == "yes" and series == "KXSOL15M" and 75 <= p2 <= 79:
        return False
    return True


def simulate(rows, cfg, slip):
    """Entry = first qualifying candle per (ticker, side); within a close cluster
    the earliest signals win slots, capped at MAX_CONCURRENT_POSITIONS.

    All 7 series settle simultaneously, so a 'close cluster' is one risk event —
    that is also the resampling unit for the bootstrap. Not modelled: consecutive-
    loss cooldown, daily loss limit, edge-degrade breaker, book-depth check. Those
    only ever remove trades, so they cannot manufacture edge.
    """
    clusters = defaultdict(list)
    for r in rows:
        clusters[r[2]].append(r)
    per_cluster, trades = defaultdict(float), []
    for cts, crows in clusters.items():
        best = {}
        for (se, tk, _, side, ask, secs, won, p1, p2, p3) in crows:
            if not qualifies(cfg, se, side, ask, secs, p1, p2, p3):
                continue
            k = (tk, side)
            if k not in best or secs > best[k][5]:
                best[k] = (se, tk, cts, side, ask, secs, won, p1, p2, p3)
        picked = sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]
        for v in picked:
            p = pnl(v[6], v[4], cfg["bet"], slip)
            per_cluster[cts] += p
            trades.append((cts, v[3], p, v[6]))
    return per_cluster, trades


def summary(rows, per_cluster, trades):
    ts = [r[2] for r in rows]
    days = max((max(ts) - min(ts)) / 86400.0, 1e-9)
    total = sum(per_cluster.values())
    n = len(trades)
    wr = sum(1 for t in trades if t[3]) / n * 100 if n else 0.0
    eq = peak = dd = 0.0
    for k in sorted(per_cluster):
        eq += per_cluster[k]
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return dict(trades=n, wr=wr, total=total, per_day=total / days,
                per_trade=total / n if n else 0.0, maxdd=dd, days=days)


def bootstrap(keys, base_pc, var_pc, iters=3000, seed=7):
    """Resample close clusters, not trades — same-expiry positions are one event."""
    d = [var_pc.get(k, 0.0) - base_pc.get(k, 0.0) for k in keys]
    rnd = random.Random(seed)
    N = len(d)
    o = sorted(sum(d[rnd.randrange(N)] for _ in range(N)) for _ in range(iters))
    return (sum(d), o[int(0.0125 * iters)], o[int(0.9875 * iters)],
            sum(1 for x in o if x > 0) / iters)


# ── cli ───────────────────────────────────────────────────────────────────────

def apply_overrides(cfg, pairs):
    cfg = dict(cfg)
    for p in pairs:
        if "=" not in p:
            sys.exit(f"--set expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        if k not in cfg:
            sys.exit(f"unknown key {k!r}; valid: {', '.join(sorted(cfg))}")
        cfg[k] = type(cfg[k])(v) if not isinstance(cfg[k], str) else v
    return cfg


def line(label, s):
    return (f"{label:<34} {s['trades']:>6}tr {s['wr']:>6.2f}%WR "
            f"{s['total']:>+9.0f} ({s['per_trade']:+.2f}/tr, {s['per_day']:+.0f}/day)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VAL",
                    help="override a config key (repeatable)")
    ap.add_argument("--compare", action="append", default=[], metavar="KEY=VAL",
                    help="run live config vs these overrides, with bootstrap CI")
    ap.add_argument("--sweep", nargs="+", metavar=("KEY", "VAL"),
                    help="sweep one key over several values")
    ap.add_argument("--slip", type=float, default=0.0,
                    help="adverse fill in cents (measured live: ~0.105)")
    ap.add_argument("--since", help="YYYY-MM-DD inclusive")
    ap.add_argument("--until", help="YYYY-MM-DD inclusive")
    a = ap.parse_args()

    cfg = live_config()
    rows = load(a.since, a.until)
    ts = [r[2] for r in rows]
    span = (f"{datetime.fromtimestamp(min(ts), timezone.utc):%Y-%m-%d} -> "
            f"{datetime.fromtimestamp(max(ts), timezone.utc):%Y-%m-%d}")
    clusters = len(set(ts))
    print(f"live config {cfg['version']} | {len(rows):,} rows | {clusters:,} clusters "
          f"| {span} | fills at ask+{a.slip:g}c | ${cfg['bet']:.0f}/trade")
    print(f"  gates: ask [{cfg['min_ask']},{cfg['max_ask']}]c  secs [{cfg['min_secs']},"
          f"{cfg['max_secs']}]  prior>={cfg['prior_min']}x{cfg['lookback']}  "
          f"yes_only={cfg['yes_only']}  max_conc={cfg['max_conc']}\n")

    if a.sweep:
        key, values = a.sweep[0], a.sweep[1:]
        if key not in cfg:
            sys.exit(f"unknown key {key!r}")
        for v in values:
            c = apply_overrides(cfg, [f"{key}={v}"])
            pc, tr = simulate(rows, c, a.slip)
            star = "  <-- LIVE" if str(cfg[key]) == str(v) else ""
            print(line(f"{key}={v}", summary(rows, pc, tr)) + star)
        return 0

    base_cfg = apply_overrides(cfg, a.set)
    base_pc, base_tr = simulate(rows, base_cfg, a.slip)
    print(line("BASELINE" if a.compare else "RESULT", summary(rows, base_pc, base_tr)))

    if a.compare:
        var_cfg = apply_overrides(base_cfg, a.compare)
        var_pc, var_tr = simulate(rows, var_cfg, a.slip)
        print(line("VARIANT  " + ",".join(a.compare), summary(rows, var_pc, var_tr)))
        keys = sorted(set(base_pc) | set(var_pc))
        d, lo, hi, pb = bootstrap(keys, base_pc, var_pc)
        if lo > 0:
            verdict = "CI excludes zero — variant is better"
        elif hi < 0:
            verdict = "CI excludes zero — variant is WORSE"
        else:
            verdict = "CI includes zero — difference NOT established"
        print(f"\n  delta {d:>+8.0f}   98.75% CI [{lo:>+8.0f}, {hi:>+8.0f}]   "
              f"P(better)={pb:.3f}")
        print(f"  {verdict}")
        if a.since or a.until:
            print("  (windowed — check this against the full history too)")
        else:
            print("  NOTE: full history includes the data most hypotheses were formed "
                  "on.\n        Re-run with --since to test a genuine holdout.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
