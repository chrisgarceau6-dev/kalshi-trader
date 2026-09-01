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
  python3 scripts/zgate_monitor.py --days 7 --email      # mails on a trip OR on blindness

Exit: 0 clean · 2 a pre-registered revert rule tripped · 3 the archive is too stale to
score the decisions harvested (the monitor cannot see, which is its own alarm).
"""
import argparse, datetime as dt, glob, gzip, csv, json, os, re, subprocess, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = dt.timezone(dt.timedelta(hours=-4))   # Kalshi ticker/close times are ET
sys.path.insert(0, ROOT)

# ── PRE-REGISTERED REVERT RULE. Fixed 2026-08-27 BEFORE any live gate data existed.
#    Do not edit these after seeing results — that is the optional-stopping error
#    #152 died of. Changing a number here requires a NEW dated row in CLAUDE.md.
MIN_N_REJECTED   = 200     # before the reversal test can fire at all
REVERT_WR_MARGIN = 0.0     # revert if rejected WR >= its own break-even
MIN_N_TOTAL      = 500     # before the profitability test can fire
# 0.10 -> 0.14 on 2026-08-28 when FLAT_BET_DOLLARS went 25 -> 35. This is a UNIT
# RESCALE, not a relaxation: $/trade is absolute dollars, so a 1.4x bet inflates it
# 1.4x at an unchanged edge, and leaving 0.10 would have silently disarmed the test —
# a genuine collapse to $0.08/trade would have printed $0.11 and never tripped.
# 0.10 x 35/25 = 0.14 holds the trip at the same underlying per-contract edge.
#
# 2026-09-01, FLAT_BET_DOLLARS 35 -> 50: RESCALING THE THRESHOLD A SECOND TIME WOULD
# NOT HAVE WORKED, and that is why this is now a ratio instead. Aug 28 got away with a
# rescale because it landed at n~0, so the whole sample was in one unit. This change
# lands at n~46 rejected with the n>=500 horizon still ahead, so the rule-2 sample will
# SPAN TWO BET SIZES. A pooled $/trade over mixed units sits between the $35 and $50
# thresholds and is correctly compared to NEITHER — there is no single dollar number
# that is right for that sample, so no rescale of REVERT_PER_TRADE could have been
# correct. Only a per-observation normalisation is.
#
# So the rule is denominated in RETURN ON WAGERED — P&L divided by dollars actually
# risked — which every trade carries individually, making it invariant to bet size and
# correct across a mixed sample. Same fix, same reason, as DAILY_LOSS_LIMIT_BETS
# (bet-denominated) and min_book_depth() (self-rescaling): fixed constants do not
# survive a sizing change; ratios do. This is the THIRD time that lesson has been
# re-learned in this repo, and it is the last place a fixed dollar constant was left.
#
# Conversion is anchored on the measured median cost of $34.04 at the $35 bet (#233):
#     0.14 / 34.04 = 0.004112
# Cross-check against the ORIGINAL calibration, which must reproduce independently:
#     0.10 / (0.972 x 25 = 24.31) = 0.004114   <- agrees to 3 significant figures
# The two anchors agreeing is the evidence this is a units change and not a new number.
# NEUTRALITY CHECK at the new size: 0.0041 x (0.972 x 50 = 48.61) = $0.199/trade, against
# a naive rescale of 0.14 x 50/35 = $0.200. Identical to the cent — the trip fires at the
# same underlying edge it always did, which is what makes this a rescale and not a
# relaxation. Approximation accepted and stated: the anchor is a MEDIAN cost used as a
# proxy for the mean, since return-on-wagered is mean-cost weighted. The gap is far
# inside the 2 significant figures this threshold is quoted to.
REVERT_RETURN_ON_WAGERED = 0.0041   # revert if overall P&L / dollars wagered falls below this
RATE_LO, RATE_HI = 0.08, 0.35   # expected ~0.20; outside this for 3 days = distribution shift

# Archive lag that counts as BROKEN rather than normal. archive_candles.py runs daily
# and can only archive settled markets, so coverage trailing the newest decision by a
# day is the healthy steady state — alarming on that would email every single run, and
# an alert that fires daily gets filtered, which recreates the blindness this guard
# exists to prevent. Alarm only once the archive job has plainly stopped keeping up.
STALE_LAG_DAYS = 2
FEE_PP           = 0.539   # fee load in pp of break-even (CLAUDE.md)

SKIP = re.compile(r"\[ZGATE-SKIP\]\s+(\S+)\s+(YES|NO)\s+([\d.]+)c\s+(\d+)s\s+z=([-+\d.]+)")
PASS = re.compile(r"\[ZGATE-PASS\]\s+(\S+)\s+(YES|NO)\s+([\d.]+)c\s+(\d+)s\s+z=([-+\d.]+)")

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
            tk, side, ask, secs, z = m.groups()
            passes.setdefault((tk, side.lower()),
                              dict(ask=float(ask), secs=int(secs), z=float(z)))
        if i % 25 == 0:
            print(f"  ...{i}/{len(runs)} runs, {len(skips)} skips", file=sys.stderr)
    return skips, passes

# Kalshi ticker timestamps are ET, not UTC (CLAUDE.md — parsing them as UTC shifts
# entries 4h and inverts z into a fake refutation). Only the DATE is taken here, and
# only to compare against archive coverage, so no spot is ever sampled off it. Do not
# "fix" this into a UTC parse.
_TK_DATE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})\d{4}-")
_MON = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def ticker_et_date(tk):
    """ET calendar date encoded in a ticker, or None. Date only — never a time."""
    m = _TK_DATE.search(tk)
    if not m:
        return None
    yy, mon, dd = m.groups()
    if mon not in _MON:
        return None
    return f"20{yy}-{_MON[mon]:02d}-{int(dd):02d}"


def outcomes():
    """won per (ticker, side) from the archive — it records every market, traded or not.

    Also returns the newest ET date the archive actually covers. Without that the
    caller cannot tell "the gate made few decisions" from "the archive is stale and
    most decisions are unscoreable" — and those two look identical in the output while
    meaning opposite things. #232 shipped zgate_watch.sh because an undercounting
    watcher can miss an ALERT, not just a digest; this is the same defect one layer
    down, where a stale archive silently shrinks n.
    """
    out, coverage = {}, None
    for f in sorted(glob.glob(os.path.join(ROOT, "data", "candles", "*.csv.gz")))[-20:]:
        with gzip.open(f, "rt") as fh:
            for r in csv.DictReader(fh):
                out[(r["ticker"], r["side"])] = r["won"] == "True"
                ts = r.get("close_ts")
                if ts:
                    try:
                        d = dt.datetime.fromtimestamp(int(ts), ET).date().isoformat()
                    except (ValueError, OSError, OverflowError):
                        continue
                    if coverage is None or d > coverage:
                        coverage = d
    return out, coverage

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--email", action="store_true")
    a = ap.parse_args()

    skips, passes = harvest(a.days)
    won, coverage = outcomes()
    rej = [(k, v, won[k]) for k, v in skips.items() if k in won]
    kep = [(k, v, won[k]) for k, v in passes.items() if k in won]
    n_r, n_k = len(rej), len(kep)
    tot = n_r + n_k
    rate = n_r / tot if tot else 0.0

    # ── ARCHIVE STALENESS. A decision the archive cannot reach is not a decision the
    #    gate did not make — it is one this monitor cannot see. Every number below is
    #    computed on the joinable subset, so if that subset is a stale-truncated slice
    #    the report looks calm while measuring almost nothing: rule 1 can never reach
    #    n>=200, and rule 3's rate is a rate over a biased sample. daily_summary.yml
    #    runs this as `--days 2 --email || true`, which discards the exit code, so the
    #    EMAIL is the only channel that can actually raise this.
    decided = list(skips) + list(passes)
    dates = sorted(d for d in (ticker_et_date(tk) for tk, _ in decided) if d)
    newest_decision = dates[-1] if dates else None
    unscored = len(decided) - (n_r + n_k)
    beyond = sorted({d for d in dates if coverage and d > coverage})

    lag = None
    if coverage and newest_decision:
        lag = (dt.date.fromisoformat(newest_decision)
               - dt.date.fromisoformat(coverage)).days

    lines, trips, warns = [], [], []
    lines.append(f"z-GATE MONITOR — last {a.days}d, Z_GATE_MIN=0.761")
    share = 100.0 * (n_r + n_k) / len(decided) if decided else 0.0
    lines.append(f"  archive covers through {coverage or 'NOTHING'}"
                 + (f" ({lag}d behind newest decision {newest_decision})" if lag is not None else "")
                 + f"   decisions {len(decided)}   scoreable {n_r + n_k} ({share:.0f}%)"
                 + f"   unscoreable {unscored}")
    if coverage is None:
        warns.append("NO ARCHIVE — data/candles/*.csv.gz is empty or unreadable. "
                     "Nothing can be scored. This monitor is blind.")
    elif decided and not tot:
        warns.append(f"BLIND — {len(decided)} decisions harvested and NONE could be "
                     f"scored against archive coverage {coverage}. Every figure below "
                     f"is absent, not reassuring. Run scripts/archive_candles.py.")
    elif lag is not None and lag > STALE_LAG_DAYS:
        warns.append(f"STALE ARCHIVE — coverage ends {coverage} but decisions run to "
                     f"{newest_decision} ({lag}d behind, healthy is <={STALE_LAG_DAYS}d). "
                     f"{unscored} of {len(decided)} decisions ({beyond[0]}..{beyond[-1]}) "
                     f"CANNOT be scored, so n is undercounted: rule 1 (n>=200) cannot "
                     f"advance and rule 3's rate is a rate over a biased subset. "
                     f"archive_candles.py has stopped keeping up.")
    lines.append(f"  signals scored {tot}   rejected {n_r} ({rate*100:.1f}%)   kept {n_k}")
    if not tot:
        lines.append("  no scoreable decisions yet")
        if warns:
            lines += [""] + ["  !!! " + w for w in warns]
        print("\n".join(lines))
        _alert(a.email, [], warns, "\n".join(lines))
        sys.exit(3 if warns else 0)

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
        bek = sum(v["ask"] for _, v, _ in kep) / n_k + FEE_PP
        lines.append(f"  KEPT       WR {wrk:.2f}%  vs break-even {bek:.2f}%  "
                     f"edge {wrk-bek:+.2f}pp   (want POSITIVE)")

    if tot >= 50 and not (RATE_LO <= rate <= RATE_HI):
        trips.append(f"DISTRIBUTION SHIFT: rejection rate {rate*100:.1f}% is outside "
                     f"[{RATE_LO*100:.0f}%,{RATE_HI*100:.0f}%]. The z distribution has moved; "
                     f"the 0.761 cut no longer means what it was fitted to mean.")

    lines.append("")
    lines.append("  pre-registered revert rule (fixed before any live data):")
    lines.append(f"    1. rejected WR >= its break-even at n>={MIN_N_REJECTED}   [n={n_r}]")
    lines.append(f"    2. overall return-on-wagered < {REVERT_RETURN_ON_WAGERED} "
                 f"at n>={MIN_N_TOTAL}  (daily_summary.py prints '$/wagered')")
    lines.append(f"    3. rejection rate outside [{RATE_LO*100:.0f}%,{RATE_HI*100:.0f}%] "
                 f"for 3 consecutive days   [now {rate*100:.1f}% "
                 f"on {share:.0f}% of decisions{' — STALE SUBSET' if warns else ''}]")
    lines.append("    revert = set Z_GATE_ENABLED=False and merge. Nothing else needs touching.")

    if trips:
        lines.append("")
        lines += ["  *** " + t for t in trips]
    if warns:
        lines.append("")
        lines += ["  !!! " + w for w in warns]
    body = "\n".join(lines)
    print(body)
    _alert(a.email, trips, warns, body)
    sys.exit(2 if trips else (3 if warns else 0))


def _alert(want_email, trips, warns, body):
    """Email on a trip OR on blindness. A monitor that cannot measure is itself an
    alert condition — that is the whole lesson of #232's undercounting watcher, and
    daily_summary.yml runs this with `|| true`, so email is the only live channel."""
    if not want_email or not (trips or warns):
        return
    subject = ("[Kalshi] z-GATE REVERT CONDITION TRIPPED" if trips
               else "[Kalshi] z-GATE MONITOR IS BLIND — archive stale")
    try:
        import smtplib
        from email.mime.text import MIMEText
        # Secret names must match daily_summary.yml — COPY_EMAIL_*, not GMAIL_*.
        frm = os.environ.get("COPY_EMAIL_FROM", "")
        to  = os.environ.get("COPY_EMAIL_TO", "")
        pwd = os.environ.get("COPY_EMAIL_PASSWORD", "")
        if frm and to and pwd:
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"], msg["To"] = frm, to
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as sv:
                sv.login(frm, pwd); sv.send_message(msg)
            print("\n  alert emailed")
        else:
            print("\n  email not configured (COPY_EMAIL_FROM/TO/PASSWORD)")
    except Exception as exc:
        print(f"\n  email failed: {exc}")


if __name__ == "__main__":
    main()
