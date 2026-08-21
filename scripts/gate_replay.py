#!/usr/bin/env python3
"""Score any strategy version against what the bot ACTUALLY SAW at poll instants.

scripts/backtest.py replays versions against data/candles/*.csv.gz, which sees every
1-min candle. The bot sees the ask only when a poll lands, and 70% of qualifying
signals last exactly one candle (CLAUDE.md invariant 6) — so archive replay is an
upper bound and cannot rank versions by what they would have CAPTURED.

late_certainty_trader.shadow_gate_inputs logs the raw gate inputs at each poll:

  [SHADOW:GATE] KXBTC15M-26AUG211115-T1 YES ask=92c secs=317 p1=95 p2=93 p3=88 series=...

This pulls those lines out of the workflow logs, joins them to settlements for the
outcome, and scores an arbitrary gate set against them. Because the trader logs facts
rather than per-version verdicts, a config invented tomorrow can still be scored
against every poll already recorded.

    python3 scripts/gate_replay.py --since 2026-08-22
    python3 scripts/gate_replay.py --since 2026-08-22 --refresh   # re-pull logs

Workflow logs expire, so parsed lines are cached under data/gatelog/ permanently.
"""
import argparse, csv, json, os, re, subprocess, sys, time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
CACHE = BASE / "data" / "gatelog"
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

LINE = re.compile(
    r"\[SHADOW:GATE\]\s+(?P<ticker>\S+)\s+(?P<side>YES|NO)\s+ask=(?P<ask>\d+)c\s+"
    r"secs=(?P<secs>\d+)\s+p1=(?P<p1>-?\d+)\s+p2=(?P<p2>-?\d+)\s+p3=(?P<p3>-?\d+)\s+"
    r"series=(?P<series>\S+)")

# Per-market gate sets recovered from git history of the trader. max_conc is a
# cluster-level constraint and is applied below, not stored here.
VERSIONS = {
    "v5.16":     dict(min_ask=90, max_ask=93, min_secs=150, max_secs=600, prior_min=75, lookback=2, yes_only=0, p3=1, max_conc=2),
    "v5.12":     dict(min_ask=90, max_ask=93, min_secs=150, max_secs=600, prior_min=75, lookback=2, yes_only=1, p3=1, max_conc=3),
    "v5.7-240":  dict(min_ask=90, max_ask=93, min_secs=240, max_secs=600, prior_min=75, lookback=2, yes_only=1, p3=1, max_conc=6),
    "v5.6-p80":  dict(min_ask=90, max_ask=93, min_secs=150, max_secs=600, prior_min=80, lookback=3, yes_only=0, p3=1, max_conc=6),
    "v5.4":      dict(min_ask=90, max_ask=95, min_secs=150, max_secs=600, prior_min=80, lookback=3, yes_only=0, p3=0, max_conc=4),
    "v5.2":      dict(min_ask=90, max_ask=99, min_secs=150, max_secs=600, prior_min=80, lookback=3, yes_only=0, p3=0, max_conc=4),
    "wide-95":   dict(min_ask=95, max_ask=99, min_secs=150, max_secs=900, prior_min=88, lookback=2, yes_only=1, p3=0, max_conc=6),
}
FEE = 0.07


def qualifies(g, side, ask, secs, p1, p2, p3):
    if not (g["min_ask"] <= ask <= g["max_ask"]):
        return False
    if not (g["min_secs"] <= secs <= g["max_secs"]):
        return False
    if any(p < g["prior_min"] for p in [p1, p2, p3][:g["lookback"]]):
        return False
    if g["p3"] and ask <= 91 and p3 < 80:
        return False
    if side == "no" and g["yes_only"]:
        return False
    return True


def pnl(won, ask, bet):
    return bet / (ask / 100.0) * (1 - ask / 100.0) * (1 - FEE) if won else -bet


def _runs(since):
    out = subprocess.run(
        ["gh", "run", "list", "--workflow=late_certainty.yml", "--limit", "400",
         "--json", "databaseId,createdAt,conclusion"],
        capture_output=True, text=True, cwd=BASE)
    if out.returncode:
        sys.exit(f"gh run list failed: {out.stderr[:300]}")
    return [r for r in json.loads(out.stdout)
            if r["createdAt"][:10] >= since and r["conclusion"] == "success"]


def harvest(since, refresh):
    """Pull [SHADOW:GATE] lines out of workflow logs into the permanent cache."""
    CACHE.mkdir(parents=True, exist_ok=True)
    done = set()
    idx = CACHE / "_runs.json"
    if idx.exists() and not refresh:
        done = set(json.load(open(idx)))
    rows = defaultdict(dict)
    for f in sorted(CACHE.glob("*.csv")):
        for r in csv.DictReader(open(f)):
            rows[f.stem][(r["ticker"], r["side"])] = r

    runs = [r for r in _runs(since) if str(r["databaseId"]) not in done]
    print(f"{len(runs)} new runs to scan", flush=True)
    for i, r in enumerate(runs, 1):
        log = subprocess.run(["gh", "run", "view", str(r["databaseId"]), "--log"],
                             capture_output=True, text=True, cwd=BASE).stdout
        for m in LINE.finditer(log):
            d = m.groupdict()
            day = close_day(d["ticker"])
            if not day:
                continue
            # first poll that saw this (ticker, side) is the one the bot could act on
            rows[day].setdefault((d["ticker"], d["side"].lower()), dict(
                ticker=d["ticker"], side=d["side"].lower(), ask=d["ask"],
                secs=d["secs"], p1=d["p1"], p2=d["p2"], p3=d["p3"],
                series=d["series"]))
        done.add(str(r["databaseId"]))
        if i % 25 == 0:
            print(f"  {i}/{len(runs)} runs", flush=True)
        time.sleep(0.05)

    for day, d in rows.items():
        with open(CACHE / f"{day}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, ["ticker", "side", "ask", "secs", "p1", "p2", "p3", "series"])
            w.writeheader()
            for v in d.values():
                w.writerow(v)
    json.dump(sorted(done), open(idx, "w"))
    return rows


def close_day(ticker):
    try:
        import datetime
        return datetime.datetime.strptime(ticker.split("-")[1].upper(),
                                          "%y%b%d%H%M").strftime("%Y-%m-%d")
    except (IndexError, ValueError):
        return None


def outcomes(since):
    """market_result per ticker, from settlements. Truth, never state."""
    import reconcile as R
    settle = R._page("/portfolio/settlements", "settlements", since, "settled_time")
    return {s["ticker"]: s.get("market_result", "") for s in settle}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--refresh", action="store_true", help="re-scan runs already cached")
    ap.add_argument("--bet", type=float, default=50.0)
    a = ap.parse_args()

    rows = harvest(a.since, a.refresh)
    seen = [r for day, d in rows.items() if day >= a.since for r in d.values()]
    if not seen:
        sys.exit("no [SHADOW:GATE] lines cached yet — the trader must run first")
    res = outcomes(a.since)
    seen = [r for r in seen if r["ticker"] in res]
    print(f"{len(seen)} poll-observed (ticker,side) with a known outcome, "
          f"{len({r['ticker'] for r in seen})} markets\n")

    print(f"{'version':<11}{'signals':>9}{'taken':>7}{'WR':>9}{'$/tr':>9}{'total $':>10}")
    for name, g in VERSIONS.items():
        fired = [r for r in seen
                 if qualifies(g, r["side"], int(r["ask"]), int(r["secs"]),
                              int(r["p1"]), int(r["p2"]), int(r["p3"]))]
        # slot allocation: within a close cluster, earliest signals win, capped
        by_cluster = defaultdict(list)
        for r in fired:
            by_cluster[r["ticker"].split("-")[1]].append(r)
        taken = []
        for c in by_cluster.values():
            taken += sorted(c, key=lambda r: -int(r["secs"]))[:g["max_conc"]]
        if not taken:
            print(f"{name:<11}{len(fired):>9}{0:>7}{'—':>9}{'—':>9}{'—':>10}")
            continue
        wins = [r for r in taken if res[r["ticker"]] == r["side"]]
        tot = sum(pnl(res[r["ticker"]] == r["side"], int(r["ask"]), a.bet) for r in taken)
        print(f"{name:<11}{len(fired):>9}{len(taken):>7}{len(wins)/len(taken)*100:>8.2f}%"
              f"{tot/len(taken):>+9.3f}{tot:>+10.2f}")

    print("\nThese are poll-observed signals, so unlike scripts/backtest.py they are "
          "NOT an\nupper bound — every row is something the bot actually had the "
          "chance to take.")


if __name__ == "__main__":
    main()
