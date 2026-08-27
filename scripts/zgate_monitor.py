#!/usr/bin/env python3
"""z-gate live monitor — enforces the PRE-REGISTERED revert rule (CLAUDE.md, Aug 27).

The gate was shipped against review advice, on the strength of evidence gathered from
the same 76 days the hypothesis was found in. The single failure mode that matters is
not "it stops working" but "it REVERSES": z is a disagreement trade between Kalshi's
implied probability and a Coinbase-spot diffusion model, so if the venue lead/lag flips
the same rule starts discarding WINNERS while every historical statistic stays true.
That is what the rejected-signal win rate below is watching for.

Harvests [ZGATE-SKIP] / [ZGATE-PASS] from Actions run logs — the decisions the bot
ACTUALLY made — and joins them to settlement outcomes from the archive.

  python3 scripts/zgate_monitor.py --days 7
  python3 scripts/zgate_monitor.py --days 7 --email      # only mails if a rule trips
"""
import argparse, glob, gzip, csv, json, os, re, subprocess, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ── PRE-REGISTERED REVERT RULE. Fixed 2026-08-27 BEFORE any live gate data existed.
#    Do not edit these after seeing results — that is the optional-stopping error
#    #152 died of. Changing a number here requires a NEW dated row in CLAUDE.md.
MIN_N_REJECTED   = 200     # before the reversal test can fire at all
REVERT_WR_MARGIN = 0.0     # revert if rejected WR >= its own break-even
MIN_N_TOTAL      = 500     # before the profitability test can fire
REVERT_PER_TRADE = 0.10    # revert if overall $/trade falls below this
RATE_LO, RATE_HI = 0.08, 0.35   # expected ~0.20; outside this for 3 days = distribution shift
FEE_PP           = 0.539   # fee load in pp of break-even (CLAUDE.md)

SKIP = re.compile(r"\[ZGATE-SKIP\]\s+(\S+)\s+(YES|NO)\s+([\d.]+)c\s+(\d+)s\s+z=([-+\d.]+)")
PASS = re.compile(r"\[ZGATE-PASS\]\s+(\S+)\s+(YES|NO)\s+z=([-+\d.]+)")

def sh(*a):
    return subprocess.run(a, cwd=ROOT, capture_output=True, text=True, timeout=600).stdout

def harvest(days):
    runs = json.loads(sh("gh", "run", "list", "--workflow=late_certainty.yml",
                         "--limit", str(days * 100), "--json",
                         "databaseId,createdAt,conclusion") or "[]")
    runs = [r for r in runs if r.get("conclusion") == "success"]
    skips, passes = {}, {}
    for i, r in enumerate(runs):
        log = sh("gh", "run", "view", str(r["databaseId"]), "--log")
        for m in SKIP.finditer(log):
            tk, side, ask, secs, z = m.groups()
            k = (tk, side.lower())
            if k not in skips: skips[k] = dict(ask=float(ask), secs=int(secs), z=float(z))
        for m in PASS.finditer(log):
            tk, side, z = m.groups()
            passes.setdefault((tk, side.lower()), dict(z=float(z)))
        if i % 25 == 0:
            print(f"  ...{i}/{len(runs)} runs, {len(skips)} skips", file=sys.stderr)
    return skips, passes

def outcomes():
    """won per (ticker, side) from the archive — it records every market, traded or not."""
    out = {}
    for f in sorted(glob.glob(os.path.join(ROOT, "data", "candles", "*.csv.gz")))[-20:]:
        with gzip.open(f, "rt") as fh:
            for r in csv.DictReader(fh):
                out[(r["ticker"], r["side"])] = r["won"] == "True"
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--email", action="store_true")
    a = ap.parse_args()

    skips, passes = harvest(a.days)
    won = outcomes()
    rej = [(k, v, won[k]) for k, v in skips.items() if k in won]
    kep = [(k, v, won[k]) for k, v in passes.items() if k in won]
    n_r, n_k = len(rej), len(kep)
    tot = n_r + n_k
    rate = n_r / tot if tot else 0.0

    lines, trips = [], []
    lines.append(f"z-GATE MONITOR — last {a.days}d, Z_GATE_MIN=0.761")
    lines.append(f"  signals scored {tot}   rejected {n_r} ({rate*100:.1f}%)   kept {n_k}")
    if not tot:
        print("\n".join(lines) + "\n  no scoreable decisions yet"); return

    if n_r:
        wr = sum(w for _, _, w in rej) / n_r * 100
        be = sum(v["ask"] for _, v, _ in rej) / n_r + FEE_PP
        lines.append(f"  REJECTED   WR {wr:.2f}%  vs break-even {be:.2f}%  "
                     f"edge {wr-be:+.2f}pp   (want NEGATIVE — that is the gate working)")
        if n_r >= MIN_N_REJECTED and wr - be >= REVERT_WR_MARGIN:
            trips.append(f"REVERSAL: rejected signals win {wr:.2f}% vs break-even {be:.2f}% "
                         f"on n={n_r} (>= {MIN_N_REJECTED}). The gate is discarding winners. REVERT.")
    if n_k:
        wrk = sum(w for _, _, w in kep) / n_k * 100
        bek = sum(v.get("ask", 92.0) for _, v, _ in kep) / n_k + FEE_PP if kep and "ask" in kep[0][1] else 92.0 + FEE_PP
        lines.append(f"  KEPT       WR {wrk:.2f}%")

    if tot >= 50 and not (RATE_LO <= rate <= RATE_HI):
        trips.append(f"DISTRIBUTION SHIFT: rejection rate {rate*100:.1f}% is outside "
                     f"[{RATE_LO*100:.0f}%,{RATE_HI*100:.0f}%]. The z distribution has moved; "
                     f"the 0.761 cut no longer means what it was fitted to mean.")

    lines.append("")
    lines.append("  pre-registered revert rule (fixed before any live data):")
    lines.append(f"    1. rejected WR >= its break-even at n>={MIN_N_REJECTED}   [n={n_r}]")
    lines.append(f"    2. overall $/trade < {REVERT_PER_TRADE} at n>={MIN_N_TOTAL}  (daily_summary.py)")
    lines.append(f"    3. rejection rate outside [{RATE_LO*100:.0f}%,{RATE_HI*100:.0f}%] "
                 f"for 3 consecutive days   [now {rate*100:.1f}%]")
    lines.append("    revert = set Z_GATE_ENABLED=False and merge. Nothing else needs touching.")

    if trips:
        lines.append("")
        lines += ["  *** " + t for t in trips]
    body = "\n".join(lines)
    print(body)
    if a.email and trips:
        try:
            import smtplib
            from email.mime.text import MIMEText
            # Secret names must match daily_summary.yml — COPY_EMAIL_*, not GMAIL_*.
            frm = os.environ.get("COPY_EMAIL_FROM", "")
            to  = os.environ.get("COPY_EMAIL_TO", "")
            pwd = os.environ.get("COPY_EMAIL_PASSWORD", "")
            if frm and to and pwd:
                msg = MIMEText(body)
                msg["Subject"] = "[Kalshi] z-GATE REVERT CONDITION TRIPPED"
                msg["From"], msg["To"] = frm, to
                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as sv:
                    sv.login(frm, pwd); sv.send_message(msg)
                print("\n  alert emailed")
            else:
                print("\n  email not configured (COPY_EMAIL_FROM/TO/PASSWORD)")
        except Exception as exc:
            print(f"\n  email failed: {exc}")
    sys.exit(2 if trips else 0)

if __name__ == "__main__":
    main()
