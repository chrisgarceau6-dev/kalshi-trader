#!/usr/bin/env python3
"""One-line-per-fact status for the late-certainty trader. Run from anywhere.

Answers the four things worth knowing without a dashboard or a remembered gh
incantation: is it alive, what did it make, is that above break-even, and what
is it holding.

    python3 scripts/kstat.py          # or: kstat
    python3 scripts/kstat.py --json   # machine-readable

Live state comes from the newest successful run's artifact, because the local
certainty_state.json is always empty — the trader persists through
actions/cache and never writes to the working tree.
"""
import argparse, datetime as D, json, os, shutil, subprocess, sys, tempfile
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")   # rule 12: report ET, never make Chris convert
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WF = "late_certainty.yml"
CACHE = Path(tempfile.gettempdir()) / "kstat-state.json"
CACHE_TTL = 60          # seconds; a scan cycle is 15 min, so this is never stale
CYCLE_MIN = 15.3        # observed cadence, used only to flag drift

C = dict(g="\033[32m", y="\033[33m", r="\033[31m", d="\033[2m", b="\033[1m", x="\033[0m")
def paint(s, c):
    return s if not sys.stdout.isatty() else C[c] + s + C["x"]

def gh(*args):
    r = subprocess.run(["gh", *args], cwd=REPO, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip()[:200])
    return r.stdout

def runs(n=15):
    return json.loads(gh("run", "list", "--workflow", WF, "--limit", str(n),
                         "--json", "status,conclusion,createdAt,updatedAt,databaseId"))

def state():
    if CACHE.exists() and (D.datetime.now().timestamp() - CACHE.stat().st_mtime) < CACHE_TTL:
        return json.loads(CACHE.read_text())
    rid = json.loads(gh("run", "list", "--workflow", WF, "--status", "success",
                        "--limit", "1", "--json", "databaseId"))[0]["databaseId"]
    tmp = Path(tempfile.mkdtemp())
    try:
        gh("run", "download", str(rid), "-D", str(tmp))
        f = next(tmp.rglob("certainty_state.json"))
        CACHE.write_text(f.read_text())
        return json.loads(f.read_text())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def ago(mins):
    return f"{mins:.0f}m ago" if mins < 60 else f"{mins/60:.1f}h ago"

def margin(rows):
    """Dollar-weighted WR vs its own break-even. See kalshi_dashboard.py for why a
    trade-weighted rate against a flat 92% is the wrong comparison."""
    cost = sum(r["cost"] for r in rows)
    fees = sum(r.get("fee", 0.0) for r in rows)
    W = [r for r in rows if r["won"]]
    wcost, wcon = sum(r["cost"] for r in W), sum(r["contracts"] for r in W)
    if not (cost > 0 and wcon > 0):
        return None
    px = wcost / wcon
    con = sum(r["contracts"] if r["won"] else r["cost"] / px for r in rows)
    if con <= 0:
        return None
    # Fees go INSIDE break-even: P&L is rev-cost-fee, so pricing break-even on
    # cost alone reads green at a real loss (~0.54c/contract = 0.54pp).
    dw, be = 100 * wcost / cost, 100 * (cost + fees) / con
    return dw, be, dw - be

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    out, rows_out = {}, []
    now = D.datetime.now(D.timezone.utc)

    # ── health ────────────────────────────────────────────────────────────
    try:
        R = runs()
        succ = [r for r in R if r["conclusion"] == "success"]
        live = any(r["status"] in ("in_progress", "queued", "pending") for r in R)
        if succ:
            age = (now - D.datetime.fromisoformat(
                succ[0]["updatedAt"].replace("Z", "+00:00"))).total_seconds() / 60
            gaps = [(D.datetime.fromisoformat(succ[i]["updatedAt"].replace("Z", "+00:00"))
                     - D.datetime.fromisoformat(succ[i + 1]["updatedAt"].replace("Z", "+00:00"))
                     ).total_seconds() / 60 for i in range(min(4, len(succ) - 1))]
            cad = sum(gaps) / len(gaps) if gaps else None
            ok = age <= 18 and (cad is None or abs(cad - CYCLE_MIN) < 3)
            tone = "g" if ok else ("y" if age <= 32 else "r")
            word = "live" if ok else ("lagging" if age <= 32 else "DOWN")
            cadtx = f" · {cad:.1f}m cadence" + ("" if cad is None or abs(cad - CYCLE_MIN) < 3 else " ⚠") if cad else ""
            rows_out.append(("trader", paint(word, tone) + f" · {ago(age)}{cadtx}"
                             + ("" if live else paint(" · nothing running", "y"))))
            out["health"] = dict(state=word, age_min=round(age, 1),
                                 cadence_min=round(cad, 1) if cad else None)
        else:
            rows_out.append(("trader", paint("no successful runs in last 15", "r")))
            out["health"] = dict(state="unknown")
    except Exception as e:
        rows_out.append(("trader", paint(f"health unavailable — {e}", "r")))

    # ── trading ───────────────────────────────────────────────────────────
    try:
        st = state()
        P = [v for v in st["positions"].values()]
        S = [p for p in P if p.get("settled")]
        SI = [(t, p) for t, p in st["positions"].items() if p.get("settled")]
        openp = [p for p in P if not p.get("settled")]
        # Bucket by the ET day of CLOSE.
        #
        # ET because this is a Chris-facing tool and CLAUDE.md rule 12 is explicit:
        # report ET, never make him convert UTC. The archive and scripts/backtest.py
        # key on the UTC day because that is how the files are named — that split is
        # deliberate, not a defect, and scripts/verify.py --check daykey prices what
        # it is worth (20.5% of trades land on a different date).
        #
        # By CLOSE, not opened_at, because close is when the P&L is realised. Entries
        # sit 150-600s before close, so the two almost always agree anyway.
        #
        # ZoneInfo, not a hardcoded -4: that offset is EDT and would have gone
        # silently wrong on 2026-11-01, reporting the wrong day for an hour every day
        # thereafter.
        today = D.datetime.now(ET).date()

        def close_day(ticker):
            """KXBTC15M-26AUG211115-T1 -> date(2026,8,21). The ticker time is ET."""
            try:
                naive = D.datetime.strptime(ticker.split("-")[1].upper(), "%y%b%d%H%M")
            except (IndexError, ValueError):
                return None
            return naive.date()

        def rows_for(pred):
            # A win is the settlement outcome, not the sign of P&L: a win that nets
            # exactly $0.00 after fees is still a win, and `pnl > 0` scored it a loss.
            g = [(t, p) for t, p in SI if pred(close_day(t))]
            return [dict(cost=p["cost"], contracts=p["contracts"],
                         won=(p.get("result") == p.get("side")
                              if p.get("result") else p["pnl"] > 0),
                         fee=p.get("fee_cost", 0.0), pnl=p["pnl"]) for _, p in g]

        td = rows_for(lambda d: d == today)
        if td:
            pnl = sum(r["pnl"] for r in td)
            wr = 100 * sum(r["won"] for r in td) / len(td)
            cost = sum(r["cost"] for r in td)
            rows_out.append(("today", f"{len(td)} tr · {wr:.1f}% · "
                             + paint(f"{pnl:+.2f}", "g" if pnl >= 0 else "r")
                             + f" · {100*pnl/cost:+.2f}% on ${cost:,.0f}"))
            m = margin(td)
            if m:
                dw, be, mg = m
                rows_out.append(("margin", paint(f"{mg:+.2f}pp", "g" if mg >= 0 else "r")
                                 + f" · $-wtd {dw:.2f}% vs {be:.2f}% b/e"))
                out["today_margin_pp"] = round(mg, 2)
            out["today"] = dict(trades=len(td), wr=round(wr, 1), pnl=round(pnl, 2))
        else:
            rows_out.append(("today", paint("no settled trades yet", "d")))

        s = st.get("stats", {})
        if s:
            lp = s.get("pnl", 0)
            rows_out.append(("lifetime", f"{s.get('trades',0)} tr · "
                             + paint(f"{lp:+.2f}", "g" if lp >= 0 else "r")
                             + f" · {st.get('strategy_version','?')}"))
            out["lifetime"] = dict(trades=s.get("trades"), pnl=lp)

        risk = sum(p["cost"] for p in openp)
        rows_out.append(("open", f"{len(openp)} positions"
                         + (f" · ${risk:,.2f} at risk" if openp else "")))
        out["open"] = dict(n=len(openp), at_risk=round(risk, 2))

        for k, label in (("consec_losses", "consec losses"),
                         ("edge_degrade_halted_at", "edge-degrade halt")):
            v = st.get(k)
            if v:
                rows_out.append((label, paint(str(v), "y")))
    except Exception as e:
        rows_out.append(("trades", paint(f"state unavailable — {e}", "r")))

    # ── next scheduled measurement (archive cron, 03:30 UTC) ──────────────
    nxt = now.replace(hour=3, minute=30, second=0, microsecond=0)
    if nxt <= now:
        nxt += D.timedelta(days=1)
    h, m = divmod(int((nxt - now).total_seconds() // 60), 60)
    rows_out.append(("next", f"archive + measurement in {h}h {m:02d}m"))
    out["next_measurement_utc"] = nxt.isoformat()

    if a.json:
        print(json.dumps(out, indent=1))
        return
    w = max(len(k) for k, _ in rows_out)
    print()
    for k, v in rows_out:
        print(f"  {paint(k.rjust(w), 'd')}  {v}")
    print()

if __name__ == "__main__":
    main()
