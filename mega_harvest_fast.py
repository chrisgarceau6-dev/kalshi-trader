#!/usr/bin/env python3
"""Parallel screening of remaining mega-harvest candidates.

Kills the serial one, picks up where it left off, runs 6 screens in parallel.
"""
import os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
import pandas as pd

BASE = Path(__file__).parent
CANDIDATES = BASE / "mega_harvest_candidates.txt"
RESULTS = BASE / "mega_harvest_hits.csv"
PROGRESS = BASE / "mega_harvest_fast_progress.txt"
WORKERS = 6
TIMEOUT = 120  # per wallet


def log(msg):
    ts = datetime.now().isoformat(timespec='seconds')
    with open(PROGRESS, "a") as f: f.write(f"[{ts}] {msg}\n")
    print(f"[{ts}] {msg}", flush=True)


def screen(wallet):
    try:
        r = subprocess.run(
            ["python", str(BASE / "wallet_5point2.py"), wallet],
            capture_output=True, text=True, timeout=TIMEOUT, cwd=str(BASE)
        )
    except subprocess.TimeoutExpired:
        return {"wallet": wallet, "timeout": True}
    out = r.stdout
    def grab(pat, cast=str, default=None):
        m = re.search(pat, out)
        try: return cast(m.group(1)) if m else default
        except: return default
    def hard(name): return f"{name} [HARD]: PASS" in out
    row = {
        "wallet": wallet,
        "n": grab(r"corrected: n=(\d+)", int, 0),
        "pnl_c": grab(r"corrected: n=\d+\s+pnl=\$(-?[\d,]+)",
                      lambda s: float(s.replace(",", "")), 0),
        "t_stat": grab(r"t_stat=([-\d.]+)", float, 0),
        "months": grab(r"months=(\d+)", int, 0),
        "top_share": grab(r"top_month_share=([\d.]+)", float, 0),
        "median_hold": grab(r"median_hold=([\d.]+)h", float, 0),
        "h1_edge": hard("1_edge_vs_spread"),
        "h3_sig":  hard("3_significant_edge_p05"),
        "h4_samp": hard("4_sample_and_spread"),
    }
    row["hard_score"] = sum([row["h1_edge"], row["h3_sig"], row["h4_samp"]])
    return row


def main():
    with open(PROGRESS, "w") as f:
        f.write(f"=== fast-mega-harvest start {datetime.now().isoformat(timespec='seconds')} ===\n")

    # Load all candidates
    with open(CANDIDATES) as f:
        all_candidates = [l.strip().lower() for l in f if l.strip().startswith("0x")]
    log(f"total candidates: {len(all_candidates)}")

    # Already screened = has a tmp csv file
    done = set()
    for f in BASE.glob("_tmp_0x*_0.03.csv"):
        # File name: _tmp_0xXXXXXXXX_0.03.csv → wallet prefix 0xXXXXXXXX
        m = re.match(r'_tmp_(0x[0-9a-f]+)_', f.name)
        if m: done.add(m.group(1))

    remaining = [w for w in all_candidates if w[:10] not in done]
    log(f"already screened: {len(all_candidates) - len(remaining)}, remaining: {len(remaining)}")

    # Load existing hits to append to
    existing_hits = []
    if RESULTS.exists():
        try:
            existing_hits = pd.read_csv(RESULTS).to_dict('records')
            log(f"existing hits loaded: {len(existing_hits)}")
        except: pass

    # Parallel screen
    results = list(existing_hits)
    hits = [r for r in results if r.get('hard_score', 0) >= 2]
    log(f"starting parallel screen with {WORKERS} workers, timeout {TIMEOUT}s")
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(screen, w): w for w in remaining}
        done_count = 0
        for fut in as_completed(futures):
            w = futures[fut]
            done_count += 1
            try:
                row = fut.result()
                if row.get("timeout"):
                    if done_count % 10 == 0:
                        log(f"  {done_count}/{len(remaining)} done. (last was timeout)")
                    continue
                if row.get("hard_score", 0) >= 2:
                    hits.append(row)
                    results.append(row)
                    log(f"  HIT: {w[:12]} hard={row['hard_score']} t={row['t_stat']:.1f} n={row['n']} pnl=${row['pnl_c']:,.0f}")
                if done_count % 25 == 0:
                    elapsed = time.time() - t0
                    rate = done_count / elapsed * 60
                    eta_min = (len(remaining) - done_count) / rate if rate > 0 else 999
                    log(f"  {done_count}/{len(remaining)} screened. total hits so far: {len(hits)}. rate: {rate:.1f}/min. ETA: {eta_min:.0f} min")
            except Exception as e:
                log(f"  error on {w[:12]}: {e}")

    # Save all hits
    if hits:
        pd.DataFrame(hits).drop_duplicates(subset=['wallet']).to_csv(RESULTS, index=False)
        log(f"=== DONE ===")
        log(f"total hits (>=2/3): {len(hits)}, threes: {sum(1 for h in hits if h.get('hard_score')==3)}")
        log(f"saved -> {RESULTS}")
        # Top by hard_score, then t_stat
        h = pd.DataFrame(hits).drop_duplicates(subset=['wallet']).sort_values(
            ["hard_score", "t_stat"], ascending=[False, False]
        )
        log(f"\n=== TOP HITS ===")
        for _, r in h.head(20).iterrows():
            log(f"  {r['wallet']} hard={r['hard_score']} t={r['t_stat']:.2f} n={r['n']} pnl=${r['pnl_c']:,.0f} months={r['months']}")
    log("=== FAST HARVEST COMPLETE ===")


if __name__ == "__main__":
    main()
