#!/usr/bin/env python3
"""Reconcile live fills against what scripts/backtest.py says the model would have done.

The model is an upper bound (CLAUDE.md invariant 6) and live has been running below
it. This decomposes that gap into the three causes that have opposite responses:

  MISSED  — model took it, live never did      -> capture problem (poll cadence, slots)
  EXTRA   — live took it, model would not      -> selection problem (stale prices, bug)
  MATCHED — both took it                       -> execution problem (fill price)

Reading the aggregate P&L gap without this split is how a capture shortfall gets
misread as edge decay; that already happened once (CLAUDE.md §2.6).

Live truth comes from the API, never from state: settlements give the outcome and the
realised dollars, fills give the entry time and the price actually paid.

    python3 scripts/reconcile.py --since 2026-08-12
    python3 scripts/reconcile.py --since 2026-08-18 --until 2026-08-20 --json out.json

Needs KALSHI_API_KEY_ID and a key path; reads .env the same way trade_history.py does.
"""
import argparse, json, os, sys, time
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))


def _load_dotenv():
    env = BASE / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv()
import backtest as B          # noqa: E402  canonical harness — never edited, only read
import kalshi_auth as K       # noqa: E402

def live_series():
    """SERIES_LIST from the trader, by AST, so it cannot drift from what is running.

    This must not be hardcoded. The archive also holds KXGOLD15M / KXSILVER15M /
    KXWTI15M, which are SHADOW_SERIES the bot deliberately does not trade. Counting
    them as model entries inflates the miss count and understates capture — the exact
    bug research/capture/audit2.py was written to fix, and repeating it here would
    make this tool agree with the wrong answer.
    """
    import ast
    tree = ast.parse(open(BASE / "late_certainty_trader.py").read())
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SERIES_LIST":
                    return set(ast.literal_eval(node.value))
    sys.exit("could not read SERIES_LIST from the trader")


SERIES = live_series()


# ── live side ─────────────────────────────────────────────────────────────────

def _page(path, key, min_date, date_field, max_pages=60):
    """Walk a cursor-paginated portfolio endpoint newest-first until min_date."""
    out, cursor, pages = [], None, 0
    while pages < max_pages:
        params = {"limit": 200}
        if cursor:
            # Both endpoints paginate on "cursor". "page_cursor" is silently ignored
            # and re-serves page 1 — an infinite loop, not an error.
            params["cursor"] = cursor
        code, data = K.get(path, params)
        if code != 200:
            sys.exit(f"{path} HTTP {code}: {str(data)[:200]}")
        batch = data.get(key, [])
        if not batch:
            break
        pages += 1
        stop = False
        for row in batch:
            if row.get(date_field, "")[:10] < min_date:
                stop = True
                break
            out.append(row)
        cursor = data.get("cursor")
        if stop or not cursor:
            break
        time.sleep(0.15)
    return out


def live_trades(since, until):
    """One record per (ticker, side) the account actually held, with realised P&L."""
    settle = _page("/portfolio/settlements", "settlements", since, "settled_time")
    fills = _page("/portfolio/fills", "fills", since, "created_time")

    # earliest fill per (ticker, side) is the entry; average the buys for the price
    ent = {}
    for f in fills:
        tk, side = f.get("ticker", ""), f.get("side", "")
        if f.get("action") != "buy":
            continue
        yp = float(f.get("yes_price_dollars", 0) or 0)
        price = yp if side == "yes" else 1.0 - yp
        e = ent.setdefault((tk, side), {"t": f["created_time"], "ct": 0.0, "notional": 0.0})
        e["t"] = min(e["t"], f["created_time"])
        c = float(f.get("count_fp", 0) or 0)
        e["ct"] += c
        e["notional"] += price * c

    out = {}
    for s in settle:
        tk = s.get("ticker", "")
        # Deliberately NOT filtered to SERIES here: main() splits live trades into
        # live-series and retired/shadow-series so the retired ones are reported
        # rather than silently dropped. Filtering here made that line unreachable.
        if not tk.split("-")[0].startswith("KX"):
            continue
        yc = float(s.get("yes_count_fp", 0) or 0)
        nc = float(s.get("no_count_fp", 0) or 0)
        if max(yc, nc) <= 0:
            continue
        side = "yes" if yc > nc else "no"
        cost = (float(s.get("yes_total_cost_dollars", 0) or 0)
                + float(s.get("no_total_cost_dollars", 0) or 0))
        rev = float(s.get("revenue", 0) or 0) / 100.0
        fee = float(s.get("fee_cost", 0) or 0)
        e = ent.get((tk, side))
        day = _close_day(tk)
        if not day or day < since or (until and day > until):
            continue
        out[(tk, side)] = dict(
            ticker=tk, side=side, day=day, contracts=max(yc, nc),
            cost=cost, pnl=rev - cost - fee,
            win=s.get("market_result", "") == side,
            entry_ts=e["t"] if e else None,
            fill_cents=(e["notional"] / e["ct"] * 100.0) if e and e["ct"] else None,
        )
    return out


def _close_day(ticker):
    """KXBTC15M-26AUG211115-T1 -> 2026-08-21.

    The time embedded in a Kalshi ticker is **ET, not UTC**. Verified against
    close_ts in the archive: 26AUG201930 carries close_ts 2026-08-20 23:30 UTC,
    exactly +4h. Parsing it as UTC shifts every trade closing after 20:00 ET into
    the previous day, which silently mis-buckets a quarter of the book.

    The archive is keyed by close, not settlement; settlement lags close by ~4h in
    the other direction and would bucket into the wrong day too.
    """
    try:
        import datetime
        naive = datetime.datetime.strptime(ticker.split("-")[1].upper(), "%y%b%d%H%M")
        return (naive + datetime.timedelta(hours=4)).strftime("%Y-%m-%d")
    except (IndexError, ValueError):
        return None


# ── model side ────────────────────────────────────────────────────────────────

def model_trades(rows, cfg, slip):
    """Same selection as B.simulate, but keeping the ticker so it can be joined.

    B.simulate throws the ticker away, and the harness must not be edited (it is the
    thing every claim is checked against). So the selection is mirrored here and then
    asserted equal to B.simulate on the same rows — if the mirror ever drifts, this
    aborts rather than quietly reporting a different strategy.
    """
    clusters = defaultdict(list)
    for r in rows:
        clusters[r[2]].append(r)
    picked_all = {}
    for cts, crows in clusters.items():
        best = {}
        for (se, tk, _, side, ask, secs, won, p1, p2, p3) in crows:
            if not B.qualifies(cfg, se, side, ask, secs, p1, p2, p3):
                continue
            k = (tk, side)
            if k not in best or secs > best[k][5]:
                best[k] = (se, tk, cts, side, ask, secs, won, p1, p2, p3)
        for v in sorted(best.values(), key=lambda r: -r[5])[:cfg["max_conc"]]:
            picked_all[(v[1], v[3])] = dict(
                ticker=v[1], side=v[3], series=v[0], ask=v[4], secs=v[5],
                win=v[6], pnl=B.pnl(v[6], v[4], cfg["bet"], slip),
                day=_close_day(v[1]),
            )

    ref_pc, ref_tr = B.simulate(rows, cfg, slip)
    if len(ref_tr) != len(picked_all):
        sys.exit(f"MIRROR DRIFT: reconcile picked {len(picked_all)} trades, "
                 f"backtest.simulate picked {len(ref_tr)} — fix reconcile.py")
    a = round(sum(t["pnl"] for t in picked_all.values()), 2)
    b = round(sum(t[2] for t in ref_tr), 2)
    if abs(a - b) > 0.01:
        sys.exit(f"MIRROR DRIFT: P&L {a} vs backtest.simulate {b} — fix reconcile.py")
    return picked_all


# ── report ────────────────────────────────────────────────────────────────────

def _stat(rows, key):
    n = len(rows)
    if not n:
        return dict(n=0, wr=0.0, total=0.0, per=0.0)
    w = sum(1 for r in rows if r["win"])
    tot = sum(r[key] for r in rows)
    return dict(n=n, wr=w / n * 100, total=tot, per=tot / n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="YYYY-MM-DD, by CLOSE day")
    ap.add_argument("--until", help="YYYY-MM-DD inclusive")
    ap.add_argument("--slip", type=float, default=0.0)
    ap.add_argument("--json", help="write the full record to this path")
    a = ap.parse_args()

    cfg = B.live_config()
    rows = [r for r in B.load(a.since, a.until) if r[0] in SERIES]
    model = model_trades(rows, cfg, a.slip)
    live_all = live_trades(a.since, a.until)
    # Series the bot no longer trades (WTI was paused mid-Aug-19) still have live
    # settlements in the window. They are neither model entries nor selection errors,
    # so they are reported on their own line rather than dumped into EXTRA.
    live = {k: v for k, v in live_all.items() if k[0].split("-")[0] in SERIES}
    retired = {k: v for k, v in live_all.items() if k not in live}

    # The archive only covers complete days. Restrict both sides to days the archive
    # actually has, or every live trade from an unarchived day is a false "EXTRA".
    archived = {_close_day(r[1]) for r in rows}
    live = {k: v for k, v in live.items() if v["day"] in archived}

    mk, lk = set(model), set(live)
    matched = sorted(mk & lk)
    missed = sorted(mk - lk)
    extra = sorted(lk - mk)

    if retired:
        rt = _stat(list(retired.values()), "pnl")
        print(f"note: {rt['n']} live trades in retired/shadow series "
              f"({', '.join(sorted({k[0].split('-')[0] for k in retired}))}) "
              f"excluded from the comparison: {rt['wr']:.2f}% WR, ${rt['total']:+.2f}")
    print(f"config {cfg['version']}  bet=${cfg['bet']:.0f}  slip={a.slip}c  "
          f"window {min(archived)} -> {max(archived)}  ({len(archived)} archived days)")
    print(f"model took {len(model)}   live took {len(live)}   "
          f"capture {len(matched) / len(model) * 100:.1f}% of model entries\n")

    print(f"{'bucket':<10}{'n':>6}{'WR':>9}{'live $':>11}{'model $':>11}{'live $/tr':>11}")
    ms = _stat([model[k] for k in matched], "pnl")
    ls = _stat([live[k] for k in matched], "pnl")
    print(f"{'MATCHED':<10}{ls['n']:>6}{ls['wr']:>8.2f}%{ls['total']:>+11.2f}"
          f"{ms['total']:>+11.2f}{ls['per']:>+11.3f}")
    xs = _stat([model[k] for k in missed], "pnl")
    print(f"{'MISSED':<10}{xs['n']:>6}{xs['wr']:>8.2f}%{'—':>11}{xs['total']:>+11.2f}{'—':>11}")
    es = _stat([live[k] for k in extra], "pnl")
    print(f"{'EXTRA':<10}{es['n']:>6}{es['wr']:>8.2f}%{es['total']:>+11.2f}{'—':>11}"
          f"{es['per']:>+11.3f}")

    live_total = ls["total"] + es["total"]
    model_total = ms["total"] + xs["total"]
    print(f"\n{'LIVE TOTAL':<10}{len(live):>6}{'':>9}{live_total:>+11.2f}")
    print(f"{'MODEL TOTAL':<10}{len(model):>6}{'':>9}{'':>11}{model_total:>+11.2f}")
    print(f"{'GAP':<10}{'':>6}{'':>9}{live_total - model_total:>+11.2f}\n")

    print("gap decomposition:")
    print(f"  capture    (model-only trades never taken)   {-xs['total']:>+10.2f}")
    print(f"  selection  (live-only trades model rejects)  {es['total']:>+10.2f}")
    print(f"  execution  (same trades, different result)   {ls['total'] - ms['total']:>+10.2f}")

    # execution splits again into price paid and outcome luck
    pf = [(live[k]["fill_cents"], model[k]["ask"]) for k in matched
          if live[k]["fill_cents"] is not None]
    if pf:
        gap = sum(f - m for f, m in pf) / len(pf)
        print(f"\nfill quality on matched trades (n={len(pf)}):")
        print(f"  live avg fill {sum(f for f, _ in pf) / len(pf):.3f}c   "
              f"model ask {sum(m for _, m in pf) / len(pf):.3f}c   "
              f"gap {gap:+.3f}c")
        print(f"  one full tick removes ~69% of profit (CLAUDE.md invariant 2)")
        wr_gap = ls["wr"] - ms["wr"]
        print(f"\n  matched-trade WR: live {ls['wr']:.2f}% vs model {ms['wr']:.2f}% "
              f"({wr_gap:+.2f}pp)")
        if abs(wr_gap) > 0.01:
            print("  NOTE: on MATCHED trades these are the same contracts held to "
                  "settlement,\n        so any WR difference here is a data or "
                  "matching defect, not edge.")

    if a.json:
        json.dump(dict(matched=[dict(model=model[k], live=live[k]) for k in matched],
                       missed=[model[k] for k in missed],
                       extra=[live[k] for k in extra]), open(a.json, "w"), indent=1)
        print(f"\nwrote {a.json}")


if __name__ == "__main__":
    main()
