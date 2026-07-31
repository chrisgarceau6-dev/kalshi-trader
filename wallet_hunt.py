#!/usr/bin/env python3
"""End-to-end wallet hunt: re-test master list + harvest new + rank + real-size check.

Runs the whole pipeline unattended and writes a final report.

usage:
    python wallet_hunt.py                            # full run
    python wallet_hunt.py --skip-harvest             # only re-test master
    python wallet_hunt.py --skip-master              # only harvest new
    python wallet_hunt.py --n-new-markets 120        # harvest breadth
    python wallet_hunt.py --n-new-wallets 200        # target new candidates
"""
import argparse, subprocess, re, os, sys, time, csv, json
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
MASTER = BASE / "all_wallets_master.txt"
LOG_DIR = BASE / "hunt_logs"
FINAL_REPORT = BASE / "wallet_hunt_report.csv"
FINAL_TOP = BASE / "wallet_hunt_top.txt"
STATUS = BASE / "wallet_hunt_status.txt"

LOG_DIR.mkdir(exist_ok=True)


def status(msg):
    ts = datetime.now().isoformat(timespec='seconds')
    line = f"[{ts}] {msg}\n"
    with open(STATUS, "a") as f:
        f.write(line)
    print(line, end="", flush=True)


def run_screener(wallet):
    """Run wallet_5point2.py on one wallet, parse the scorecard."""
    log_file = LOG_DIR / f"{wallet[:10]}.log"
    try:
        r = subprocess.run(
            ["python", str(BASE / "wallet_5point2.py"), wallet],
            capture_output=True, text=True, timeout=300, cwd=str(BASE)
        )
    except subprocess.TimeoutExpired:
        return {"wallet": wallet, "error": "timeout"}
    out = r.stdout
    with open(log_file, "w") as f:
        f.write(out)

    def grab(pat, cast=str, default=None):
        m = re.search(pat, out)
        try:
            return cast(m.group(1)) if m else default
        except Exception:
            return default

    def hard_pass(name):
        return f"{name} [HARD]: PASS" in out

    def soft_pass(name):
        return f"{name} [SOFT]: PASS" in out

    row = {
        "wallet": wallet,
        "n": grab(r"corrected: n=(\d+)", int, 0),
        "pnl_corrected": grab(r"corrected: n=\d+\s+pnl=\$(-?[\d,]+)",
                              lambda s: float(s.replace(",", "")), 0),
        "sharpe": grab(r"sharpe=([-\d.]+)", float, 0),
        "t_stat": grab(r"t_stat=([-\d.]+)", float, 0),
        "median_hold": grab(r"median_hold=([\d.]+)h", float, 0),
        "open_positions": grab(r"open_positions=(\d+)", int, 0),
        "months": grab(r"months=(\d+)", int, 0),
        "top_month_share": grab(r"top_month_share=([\d.]+)", float, 0),
        "raw_pnl_3c": grab(r"raw @3c:.*?pnl=\$(-?[\d,]+)",
                           lambda s: float(s.replace(",", "")), 0),
        "hard_1_edge": hard_pass("1_edge_vs_spread"),
        "hard_3_sig": hard_pass("3_significant_edge_p05"),
        "hard_4_sample": hard_pass("4_sample_and_spread"),
        "soft_2_hold": soft_pass("2_hold_ge_24.0h"),
        "soft_5_conc": soft_pass("5_concurrent_le_20"),
    }
    row["hard_score"] = sum([row["hard_1_edge"], row["hard_3_sig"], row["hard_4_sample"]])
    row["total_score"] = row["hard_score"] + sum([row["soft_2_hold"], row["soft_5_conc"]])
    return row


def harvest_new(n_markets, min_size, max_wallets, exclude_set):
    """Harvest fresh wallets excluding known ones."""
    out_file = BASE / "hunt_harvested.txt"
    status(f"harvesting up to {max_wallets*3} raw candidates from top {n_markets} markets...")
    try:
        r = subprocess.run(
            ["python", str(BASE / "harvest_and_screen.py"),
             "--n-markets", str(n_markets),
             "--min-size", str(min_size),
             "--max-wallets", str(max_wallets * 3),  # oversample so we can filter
             "--out", str(out_file),
             "--skip-screen"],
            capture_output=True, text=True, timeout=1800, cwd=str(BASE)
        )
        status(f"harvest stdout tail: ...{r.stdout[-500:]}")
    except subprocess.TimeoutExpired:
        status("harvest timed out")
        return []
    if not out_file.exists():
        status("harvest produced no file")
        return []
    with open(out_file) as f:
        raw = [line.strip().lower() for line in f if line.strip().startswith("0x")]
    filtered = [w for w in raw if w not in exclude_set]
    filtered = filtered[:max_wallets]
    status(f"harvested {len(raw)} raw, {len(filtered)} new (after excluding {len(raw)-len(filtered)} duplicates)")
    with open(BASE / "hunt_new_candidates.txt", "w") as f:
        f.write("\n".join(filtered) + "\n")
    return filtered


def run_real_size_check(wallet):
    log_file = LOG_DIR / f"{wallet[:10]}_size.log"
    try:
        r = subprocess.run(
            ["python", str(BASE / "real_size_check.py"), wallet],
            capture_output=True, text=True, timeout=180, cwd=str(BASE)
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    with open(log_file, "w") as f:
        f.write(r.stdout + "\n---STDERR---\n" + r.stderr)
    return r.stdout


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--skip-master", action="store_true")
    p.add_argument("--skip-harvest", action="store_true")
    p.add_argument("--n-new-markets", type=int, default=120)
    p.add_argument("--n-new-wallets", type=int, default=200)
    p.add_argument("--min-trade-size", type=float, default=200)
    a = p.parse_args()

    # Clear status file
    with open(STATUS, "w") as f:
        f.write(f"=== wallet hunt started {datetime.now().isoformat(timespec='seconds')} ===\n")

    # Load master list
    with open(MASTER) as f:
        master = [line.strip().lower() for line in f if line.strip().startswith("0x")]
    status(f"master list: {len(master)} wallets")

    all_results = []
    exclude_set = set(master)

    # Step 1: re-test master
    if not a.skip_master:
        status(f"=== STEP 1: RE-TEST {len(master)} MASTER WALLETS ===")
        for i, w in enumerate(master, 1):
            row = run_screener(w)
            row["source"] = "master"
            all_results.append(row)
            if i % 10 == 0 or row.get("hard_score", 0) >= 2:
                status(f"  {i}/{len(master)} done. latest: {w[:10]} hard={row.get('hard_score',0)} t={row.get('t_stat',0)}")
    else:
        status("skipping master re-test")

    # Step 2: harvest + test new
    new_wallets = []
    if not a.skip_harvest:
        status(f"=== STEP 2: HARVEST + TEST NEW WALLETS ===")
        new_wallets = harvest_new(a.n_new_markets, a.min_trade_size, a.n_new_wallets, exclude_set)
        status(f"screening {len(new_wallets)} new wallets...")
        for i, w in enumerate(new_wallets, 1):
            row = run_screener(w)
            row["source"] = "new_harvest"
            all_results.append(row)
            if i % 10 == 0 or row.get("hard_score", 0) >= 2:
                status(f"  {i}/{len(new_wallets)} done. latest: {w[:10]} hard={row.get('hard_score',0)} t={row.get('t_stat',0)}")
    else:
        status("skipping harvest")

    # Step 3: compile results
    status(f"=== STEP 3: COMPILE RESULTS ({len(all_results)} tested) ===")
    # Filter and sort
    valid = [r for r in all_results if "error" not in r and r.get("hard_score", 0) >= 2]
    valid.sort(key=lambda r: (-r["hard_score"], -abs(r.get("t_stat", 0) or 0)))

    # Write full CSV
    if all_results:
        keys = list(all_results[0].keys())
        with open(FINAL_REPORT, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            for r in all_results:
                w.writerow(r)
        status(f"full report saved: {FINAL_REPORT}")

    # Write top hits + run real_size_check on 3/3s
    lines = [f"=== WALLET HUNT REPORT — {datetime.now().isoformat(timespec='seconds')} ==="]
    lines.append(f"Tested {len(all_results)} wallets total ({sum(1 for r in all_results if r.get('source')=='master')} master + {sum(1 for r in all_results if r.get('source')=='new_harvest')} new)")
    lines.append(f"Hits (>= 2/3 HARD): {len(valid)}")
    lines.append("")

    threes = [r for r in valid if r["hard_score"] == 3]
    twos = [r for r in valid if r["hard_score"] == 2]

    lines.append(f"### 3/3 HARD HITS ({len(threes)})")
    for r in threes:
        lines.append(f"  {r['wallet']} src={r['source']} t={r['t_stat']:.2f} n={r['n']} pnl_corr=${r['pnl_corrected']:,.0f} hold={r['median_hold']:.1f}h months={r['months']} top_share={r['top_month_share']:.2f}")

    lines.append("")
    lines.append(f"### 2/3 HARD HITS ({len(twos)}) — top 20 by t-stat")
    for r in twos[:20]:
        missed = []
        if not r['hard_1_edge']: missed.append("edge_vs_spread")
        if not r['hard_3_sig']:  missed.append("significant_p05")
        if not r['hard_4_sample']: missed.append("sample_spread")
        lines.append(f"  {r['wallet']} src={r['source']} t={r['t_stat']:.2f} n={r['n']} pnl_corr=${r['pnl_corrected']:,.0f} missed={','.join(missed)}")

    # Real-size check on 3/3s
    if threes:
        status(f"=== STEP 4: REAL SIZE CHECK ON {len(threes)} 3/3 HITS ===")
        lines.append("")
        lines.append("### REAL-SIZE CHECKS (for 3/3 hits)")
        for r in threes:
            status(f"  size check: {r['wallet']}")
            out = run_real_size_check(r['wallet'])
            lines.append(f"\n{r['wallet']}:")
            lines.append(out[-2000:] if len(out) > 2000 else out)

    with open(FINAL_TOP, "w") as f:
        f.write("\n".join(lines))

    status(f"=== DONE ===")
    status(f"full CSV: {FINAL_REPORT}")
    status(f"report:   {FINAL_TOP}")
    status(f"summary: {len(threes)} × 3/3, {len(twos)} × 2/3 out of {len(all_results)} tested")


if __name__ == "__main__":
    main()
