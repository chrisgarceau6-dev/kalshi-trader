#!/usr/bin/env python3
"""Replay the trailing-24h loss limit over the archive, at any bet size.

WHY: compute_daily_loss_limit() returns max(300, bet*4). The $300 floor was validated
at a $75 bet, where ~4 net losses trip it. At the live $25 bet the same dollar figure
takes ~12, so the same constant is a materially different control. scripts/backtest.py
deliberately does not model the halt ("Not modelled: consecutive-loss cooldown, daily
loss limit, edge-degrade breaker, book-depth check"), so this measures it separately.

APPROXIMATION, stated up front: realized P&L is booked at cluster close, and entries
inside a cluster are decided before it resolves. The live daemon sees settlements
arrive with lag, so this is directional rather than exact — the same limitation
CLAUDE.md flags for its own $200-vs-$300 sweep.

    python3 docs/audit/claude/replay_loss_limit.py
    python3 docs/audit/claude/replay_loss_limit.py --bet 75 --slip 0.105

Reproduces docs/audit/claude/DECISIONS.md §D1.
"""
import argparse
import collections
import datetime as D
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import backtest as B                                          # noqa: E402

NO_LIMIT = 10 ** 9


def picks_by_cluster(rows, cfg):
    """The trades the live selection would take, grouped by close cluster."""
    clusters = collections.defaultdict(list)
    for r in rows:
        clusters[r[2]].append(r)
    out = {}
    for cts, crows in clusters.items():
        best = {}
        for (se, tk, _, side, ask, secs, won, p1, p2, p3) in crows:
            if not B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
                continue
            k = (tk, side)
            if k not in best or secs > best[k][5]:
                best[k] = (se, tk, cts, side, ask, secs, won, p1, p2, p3)
        out[cts] = sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]
    return out


def replay(picks, limit, bet, slip):
    """Walk clusters forward, halting while trailing-24h realized P&L <= -limit."""
    booked, total, blocked, halted = [], 0.0, [], 0
    for cts in sorted(picks):
        trailing = sum(p for ts, p in booked if ts >= cts - 86400)
        if trailing <= -limit:
            halted += 1
            for v in picks[cts]:
                blocked.append((B.pnl(v[6], v[4], bet, slip), v[6]))
            continue
        for v in picks[cts]:
            p = B.pnl(v[6], v[4], bet, slip)
            total += p
            booked.append((cts, p))
    return total, blocked, halted


def worst_drawdown(picks, bet, slip):
    booked, worst, when = [], 0.0, None
    for cts in sorted(picks):
        for v in picks[cts]:
            booked.append((cts, B.pnl(v[6], v[4], bet, slip)))
        t = sum(p for ts, p in booked if ts >= cts - 86400)
        if t < worst:
            worst, when = t, cts
    return worst, when


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bet", type=float, action="append",
                    help="bet size (repeatable; default 25 and 75)")
    ap.add_argument("--slip", type=float, default=0.227,
                    help="adverse fill in cents (default: the measured 0.227)")
    ap.add_argument("--limits", type=float, nargs="+",
                    default=[150, 200, 300, 400, 600])
    a = ap.parse_args()
    bets = a.bet or [25, 75]

    cfg = B.live_config()
    rows = B.load(series=cfg["series"])
    print(f"live config {cfg['version']} | {len(rows):,} rows | live series only | "
          f"slip {a.slip}c\n")

    for bet in bets:
        c = dict(cfg)
        c["bet"] = bet
        picks = picks_by_cluster(rows, c)
        floor = max(300, bet * 4)
        worst, when = worst_drawdown(picks, bet, a.slip)
        day = D.datetime.utcfromtimestamp(when).date() if when else "-"
        fires = "FIRES" if worst <= -floor else f"NEVER FIRES (clears by ${floor + worst:.0f})"
        print(f"  bet ${bet:.0f}  live limit max(300, bet*4) = ${floor:.0f}")
        print(f"    worst trailing-24h realized P&L ${worst:.2f} on {day} -> {fires}")
        print(f"    {'limit':>8}{'total':>10}{'halted':>9}{'blocked':>9}"
              f"{'blocked WR':>12}{'blocked $/tr':>14}")
        for limit in list(a.limits) + [NO_LIMIT]:
            total, bl, halted = replay(picks, limit, bet, a.slip)
            label = "none" if limit >= NO_LIMIT else f"${limit:.0f}"
            wr = 100 * sum(1 for _, w in bl if w) / len(bl) if bl else 0.0
            per = sum(p for p, _ in bl) / len(bl) if bl else 0.0
            star = "  <-- LIVE" if abs(limit - floor) < 1e-9 else ""
            print(f"    {label:>8}{total:>+10.0f}{halted:>9}{len(bl):>9}"
                  f"{wr:>11.2f}%{per:>+14.3f}{star}")
        print()
    print("  A post-hoc best row here is NOT a recommendation: these are in-sample over")
    print("  ~74 days on an archive that is integer-rounded before 2026-08-22, and one")
    print("  drawdown event drives the ranking. Invariant 8 applies. See DECISIONS.md D1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
