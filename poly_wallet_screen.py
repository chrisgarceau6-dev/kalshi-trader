#!/usr/bin/env python3
"""Screen candidate wallets for COPYABILITY, not for PnL.

Scores each wallet against the criteria that are measurable from the
data-api /positions snapshot, and prints PASS / REJECT with reasons.

WHAT THIS CAN TEST (from /positions):
  [edge]     resolved gross edge per contract at THEIR OWN fills   >= +15c
  [size]     position count small enough that $2k can hold it      10..60
  [persist]  edge positive in BOTH halves of the resolved window
  [conc]     top single position is not most of the PnL            <= 40%
  [sample]   enough resolved positions to mean anything            >= 30
  [slow]     archetype from poly_timing.csv is SWING/POSITION      (if column present)

WHAT THIS CANNOT TEST — gate 2, live paper-trading only:
  * maker vs taker (are their fills purchasable, or resting orders?)
  * order-book depth (can you get size at that price?)
  * your actual latency-adjusted fill when their trade prints
  A wallet passing this screen is a CANDIDATE, not a green light.

KNOWN BIAS — READ THIS BEFORE TRUSTING A REJECT:
  /positions only shows what they still hold. Winners they already redeemed
  are GONE from the snapshot. That biases every edge number DOWNWARD.
  So: a PASS is strong evidence. A REJECT is weak evidence, especially for
  wallets that redeem promptly. Do not treat a marginal reject as a kill.

usage:
    python poly_wallet_screen.py
    python poly_wallet_screen.py --wallets-file poly_timing.csv --max-wallets 60
    python poly_wallet_screen.py --wallets 0xabc...,0xdef...
    python poly_wallet_screen.py --min-edge 0.10 --max-positions 100
"""
import argparse, sys, time
import requests
import pandas as pd

DATA = "https://data-api.polymarket.com"


def fetch_positions(wallet, timeout=25, max_pages=6):
    out, offset, LIMIT = [], 0, 500
    for _ in range(max_pages):
        try:
            r = requests.get(f"{DATA}/positions",
                             params={"user": wallet, "limit": LIMIT, "offset": offset},
                             timeout=timeout)
        except Exception as e:
            return out, f"net error: {type(e).__name__}"
        if r.status_code != 200:
            return out, f"HTTP {r.status_code}"
        try:
            batch = r.json()
        except Exception:
            return out, "bad json"
        if not batch:
            break
        out.extend(batch)
        if len(batch) < LIMIT:
            break
        offset += LIMIT
        time.sleep(0.25)
    return out, None


def score(wallet, name, archetype, a):
    pos, err = fetch_positions(wallet)
    if err:
        return {"wallet": wallet, "name": name, "verdict": "SKIP", "why": err}
    if not pos:
        return {"wallet": wallet, "name": name, "verdict": "SKIP", "why": "no positions"}

    df = pd.DataFrame(pos)
    for c in ["size", "avgPrice", "curPrice", "initialValue",
              "currentValue", "cashPnl", "realizedPnl"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    need = {"avgPrice", "curPrice", "size"}
    if not need.issubset(df.columns):
        return {"wallet": wallet, "name": name, "verdict": "SKIP", "why": "schema drift"}

    df = df[df["size"].fillna(0) > 0]
    n_pos = len(df)
    if n_pos == 0:
        return {"wallet": wallet, "name": name, "verdict": "SKIP", "why": "no live size"}

    res = df[(df["curPrice"] >= 0.98) | (df["curPrice"] <= 0.02)].copy()
    n_res = len(res)
    row = {"wallet": wallet, "name": name, "archetype": archetype,
           "n_pos": n_pos, "n_resolved": n_res}

    if n_res == 0:
        row.update(verdict="REJECT", why="no resolved positions", edge_c=None)
        return row

    edge = (res["curPrice"] - res["avgPrice"]).mean()
    inv = (res["initialValue"] if "initialValue" in res
           else res["size"] * res["avgPrice"])
    pnl = (res["cashPnl"] if "cashPnl" in res
           else (res["curPrice"] - res["avgPrice"]) * res["size"])
    roi = pnl.sum() / max(inv.sum(), 1)
    row.update(edge_c=round(edge * 100, 1), roi_pct=round(roi * 100, 1),
               invested=round(inv.sum()), pnl=round(pnl.sum()))

    # concentration: does one position carry the whole book?
    tot_win = pnl[pnl > 0].sum()
    row["top_pnl_share"] = round(100 * pnl.max() / tot_win, 0) if tot_win > 0 else None

    # persistence: split the resolved window in half by endDate
    half_ok = None
    if "endDate" in res.columns:
        res["_end"] = pd.to_datetime(res["endDate"], errors="coerce", utc=True)
        e = res.dropna(subset=["_end"]).sort_values("_end")
        if len(e) >= 2 * a.min_resolved // 2 and len(e) >= 10:
            mid = len(e) // 2
            p1 = (e.iloc[:mid]["cashPnl"].sum() if "cashPnl" in e
                  else ((e.iloc[:mid]["curPrice"] - e.iloc[:mid]["avgPrice"])
                        * e.iloc[:mid]["size"]).sum())
            p2 = (e.iloc[mid:]["cashPnl"].sum() if "cashPnl" in e
                  else ((e.iloc[mid:]["curPrice"] - e.iloc[mid:]["avgPrice"])
                        * e.iloc[mid:]["size"]).sum())
            row["pnl_h1"], row["pnl_h2"] = round(p1), round(p2)
            half_ok = (p1 > 0) and (p2 > 0)
    row["persist"] = half_ok

    fails = []
    if edge < a.min_edge:
        fails.append(f"edge {edge*100:+.1f}c < {a.min_edge*100:.0f}c")
    if n_res < a.min_resolved:
        fails.append(f"only {n_res} resolved")
    if n_pos > a.max_positions:
        fails.append(f"{n_pos} positions > {a.max_positions}")
    if n_pos < a.min_positions:
        fails.append(f"{n_pos} positions < {a.min_positions}")
    if half_ok is False:
        fails.append("edge not positive in both halves")
    if row["top_pnl_share"] is not None and row["top_pnl_share"] > a.max_conc:
        fails.append(f"top position = {row['top_pnl_share']:.0f}% of wins")
    if archetype and str(archetype).upper() in ("SCALP", "DAY"):
        fails.append(f"archetype {archetype} (too fast to copy)")

    row["verdict"] = "PASS" if not fails else "REJECT"
    row["why"] = "; ".join(fails) if fails else "clears every measurable gate"
    return row


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--wallets-file", default="poly_timing.csv")
    p.add_argument("--wallets", default=None, help="comma-sep 0x addresses")
    p.add_argument("--max-wallets", type=int, default=50)
    p.add_argument("--min-edge", type=float, default=0.15, help="dollars/contract")
    p.add_argument("--min-resolved", type=int, default=30)
    p.add_argument("--min-positions", type=int, default=10)
    p.add_argument("--max-positions", type=int, default=60)
    p.add_argument("--max-conc", type=float, default=40.0, help="pct of wins in top position")
    p.add_argument("--out", default="wallet_screen.csv")
    a = p.parse_args()

    cands = []
    if a.wallets:
        cands = [(w.strip().lower(), "", "") for w in a.wallets.split(",") if w.strip()]
    else:
        try:
            src = pd.read_csv(a.wallets_file)
        except FileNotFoundError:
            print(f"missing {a.wallets_file} — pass --wallets instead"); sys.exit(1)
        print(f"{a.wallets_file} columns: {sorted(src.columns.tolist())}")
        wcol = next((c for c in src.columns if c.lower() in
                     ("wallet", "_wallet", "address", "proxywallet")), None)
        if wcol is None:
            print("no wallet column found — tell me the column list above"); sys.exit(1)
        ncol = next((c for c in src.columns if c.lower() in ("name", "wallet_name")), None)
        acol = next((c for c in src.columns if "arche" in c.lower()), None)
        for _, r in src.head(a.max_wallets).iterrows():
            cands.append((str(r[wcol]).lower(),
                          str(r[ncol]) if ncol else "",
                          str(r[acol]) if acol else ""))

    print(f"screening {len(cands)} wallets "
          f"(edge >= {a.min_edge*100:.0f}c, {a.min_positions}-{a.max_positions} positions, "
          f"{a.min_resolved}+ resolved)\n")

    rows = []
    for i, (w, nm, arch) in enumerate(cands):
        print(f"[{i+1}/{len(cands)}] {nm or w[:10]}...", flush=True)
        rows.append(score(w, nm, arch, a))
        time.sleep(0.4)

    R = pd.DataFrame(rows)
    if "edge_c" in R.columns:
        R = R.sort_values("edge_c", ascending=False, na_position="last")
    R.to_csv(a.out, index=False)

    cols = [c for c in ["name", "wallet", "archetype", "n_pos", "n_resolved",
                        "edge_c", "roi_pct", "top_pnl_share", "persist",
                        "verdict", "why"] if c in R.columns]
    with pd.option_context("display.width", 250, "display.max_colwidth", 46):
        print("\n=== SCREEN RESULTS (sorted by gross edge at their own fills) ===")
        print(R[cols].to_string(index=False))

    passes = R[R.get("verdict") == "PASS"] if "verdict" in R else pd.DataFrame()
    print(f"\n{len(passes)} PASS of {len(R)} screened -> {a.out}")
    if len(passes):
        print("\nA PASS is a CANDIDATE, not a green light. Next step is gate 2:")
        print("  paper-trade it live, logging the real best-ask and depth at the")
        print("  moment each of their trades prints. That is the only thing that")
        print("  answers whether their fills are purchasable by you.")
    else:
        print("\nNo passes. Before concluding the pool is empty, remember the")
        print("redemption bias above — rerun with --min-edge 0.08 and see whether")
        print("anything lands in the 8-15c band worth a closer look.")


if __name__ == "__main__":
    main()
