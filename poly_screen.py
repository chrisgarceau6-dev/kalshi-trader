#!/usr/bin/env python3
"""
poly_screen.py  —  structural copyability screen + forward-log for Polymarket wallets.

Purpose: stop eyeballing "this wallet looks good" and instead FAIL wallets automatically
against the structural bar for copy-tradeability, then report whether any survivor is
distinguishable from a lucky coin-flip.

Reuses the exact fetch logic from kalshi_weather_edge.py (fetch_trades / leaderboard),
so the tested client behavior is preserved. The only genuinely new piece is the optional
Gamma market-metadata fetch, which is isolated and self-diagnosing.

Two subcommands:
  screen   — run the copyability gates + persistence test over a wallet universe
  forward  — freeze a survivor set today and append their live trades to a log,
             so in 4-8 weeks you have an uncontaminated out-of-sample copy record.

Everything measured on PAST trades (including this screen) can pass by luck.
Only the forward log is real evidence.
"""
import argparse, json, sys, time, os
from datetime import datetime, timezone
import requests
import pandas as pd

# ---- endpoint constants (match kalshi_weather_edge.py; override via CLI if yours differ) ----
DEFAULT_DATA  = "https://data-api.polymarket.com"
DEFAULT_GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
SLEEP = 0.5

# ------------------------------------------------------------------ fetch (verbatim logic) --
def fetch_trades(data_base, wallet, max_trades):
    rows, offset = [], 0
    while offset < max_trades:
        try:
            r = requests.get(data_base + "/trades",
                params=dict(user=wallet, limit=500, offset=offset), headers=UA, timeout=25)
        except requests.RequestException:
            break
        if not r.ok:
            if r.status_code == 429:
                time.sleep(3); continue
            break
        batch = r.json() or []
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch); offset += 500
        time.sleep(SLEEP)
        if len(batch) < 500:
            break
    return rows

def fetch_leaderboard(data_base, category, time_period, want):
    rows, offset = [], 0
    while offset <= 1000 and len(rows) < want + 50:
        try:
            r = requests.get(data_base + "/v1/leaderboard",
                params=dict(category=category, timePeriod=time_period,
                            orderBy="PNL", limit=50, offset=offset), headers=UA, timeout=25)
        except requests.RequestException as e:
            print(f"  leaderboard error: {e}"); break
        if not r.ok:
            print(f"  leaderboard {r.status_code}: {r.text[:150]}"); break
        batch = r.json() or []
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(batch); offset += 50
        time.sleep(SLEEP)
        if len(batch) < 50:
            break
    return rows

# ------------------------------------------------ optional Gamma market metadata (NEW/isolated) --
_META_CACHE = {}
def fetch_market_meta(gamma_base, condition_ids, debug=False):
    """Return {conditionId: {endDate, startDate}} for given ids. Self-diagnosing on schema."""
    out = {}
    ids = [c for c in condition_ids if c and c not in _META_CACHE]
    for i in range(0, len(ids), 20):
        chunk = ids[i:i+20]
        try:
            r = requests.get(gamma_base + "/markets",
                params={"condition_ids": ",".join(chunk)}, headers=UA, timeout=25)
            if not r.ok:
                if debug: print(f"  gamma {r.status_code}: {r.text[:120]}")
                continue
            data = r.json()
            data = data if isinstance(data, list) else data.get("data", [])
            if debug and data:
                print("  RAW GAMMA MARKET OBJECT (confirm field names):")
                print("  " + json.dumps(data[0], indent=2)[:600]); debug = False
            for m in data:
                cid = m.get("conditionId") or m.get("condition_id")
                end = m.get("endDate") or m.get("endDateIso") or m.get("end_date_iso")
                start = m.get("startDate") or m.get("createdAt") or m.get("startDateIso")
                if cid:
                    _META_CACHE[cid] = {"end": end, "start": start}
        except requests.RequestException as e:
            if debug: print(f"  gamma error: {e}")
        time.sleep(SLEEP)
    for c in condition_ids:
        if c in _META_CACHE:
            out[c] = _META_CACHE[c]
    return out

def _iso_to_dt(s):
    if not s: return None
    try:
        return pd.to_datetime(s, utc=True)
    except Exception:
        return None

# ------------------------------------------------------------------ round-trip construction --
def build_roundtrips(trades):
    """FIFO match buys->sells per asset. Returns DataFrame of realized round-trips."""
    df = pd.DataFrame(trades)
    if df.empty: return df, df
    for col in ("price", "size", "timestamp"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "timestamp" not in df.columns:
        return pd.DataFrame(), df
    df["ts"] = pd.to_datetime(df["timestamp"], unit="s", errors="coerce", utc=True)
    df = df.dropna(subset=["price", "size", "ts"])
    if df.empty or "asset" not in df.columns or "side" not in df.columns:
        return pd.DataFrame(), df

    rts, positions = [], {}
    for _, r in df.sort_values("ts").iterrows():
        asset, side, size = r.get("asset"), str(r.get("side") or "").upper(), r["size"]
        cid = r.get("conditionId") or r.get("condition_id")
        if not asset or size <= 0:
            continue
        if side in ("BUY", "MINT"):
            positions.setdefault(asset, []).append([r["ts"], size, r["price"], cid, (r.get("title") or "")[:70]])
        elif side in ("SELL", "REDEEM"):
            remaining = size
            while remaining > 0 and positions.get(asset):
                b = positions[asset][0]
                match = min(b[1], remaining)
                rts.append({"entry_ts": b[0], "exit_ts": r["ts"],
                            "entry_price": b[2], "exit_price": r["price"], "size": match,
                            "hold_hrs": (r["ts"] - b[0]).total_seconds()/3600,
                            "conditionId": b[3], "title": b[4]})
                remaining -= match; b[1] -= match
                if b[1] <= 0: positions[asset].pop(0)
    return pd.DataFrame(rts), df

# ------------------------------------------------------------------------------ the screen --
def net_pnl(rt, slip, fee_bps):
    """Realized net PnL per round-trip after slippage (both sides) + fee. Copyable-EV proxy."""
    fee = fee_bps / 10000.0
    gross = (rt["exit_price"] - rt["entry_price"]) * rt["size"]
    slip_cost = 2 * slip * rt["size"]                       # pay slip in, give up slip out
    fee_cost  = fee * rt["size"] * (rt["entry_price"] + rt["exit_price"])
    return gross - slip_cost - fee_cost

def screen_wallet(rt, meta, args):
    if rt.empty or len(rt) < args.min_roundtrips:
        return None
    rt = rt.copy()
    rt["notional"] = rt["size"] * rt["entry_price"]
    rt["net"] = net_pnl(rt, args.slippage, args.fee_bps)

    # --- gate 1: slow resolution ---
    ttr_hrs = None
    if meta is not None and not rt["conditionId"].isna().all():
        ttrs = []
        for _, r in rt.iterrows():
            m = meta.get(r["conditionId"])
            end = _iso_to_dt(m["end"]) if m else None
            if end is not None:
                ttrs.append((end - r["entry_ts"]).total_seconds()/3600)
        if ttrs:
            ttr_hrs = float(pd.Series(ttrs).median())
    slow_metric = ttr_hrs if ttr_hrs is not None else float(rt["hold_hrs"].median())
    slow_src = "market_ttr" if ttr_hrs is not None else "hold_proxy"
    slow_ok = slow_metric >= args.min_hold_hrs

    # --- gate 2: entry-price mass in the 30-70c band (by notional) ---
    band = rt[(rt.entry_price >= 0.30) & (rt.entry_price <= 0.70)]
    band_share = band["notional"].sum() / max(rt["notional"].sum(), 1e-9)
    band_ok = band_share >= args.min_band_share

    # --- gate 3: fee-clearing size ---
    med_notional = float(rt["notional"].median())
    size_ok = med_notional >= args.min_notional

    # --- gate 4: concentration (not 90k diffuse, not 1 lucky market) ---
    n_mkts = rt["conditionId"].nunique() if not rt["conditionId"].isna().all() else rt["title"].nunique()
    pnl_by_mkt = rt.groupby(rt["conditionId"].fillna(rt["title"]))["net"].sum().sort_values(ascending=False)
    pos = pnl_by_mkt[pnl_by_mkt > 0]
    top5_share = pos.head(5).sum() / max(pos.sum(), 1e-9) if len(pos) else 1.0
    # copyable if a handful of markets carry it (not 90k) AND it isn't all one market (regime luck)
    conc_ok = (n_mkts <= args.max_markets) and (top5_share <= args.max_top5_share)

    # --- persistence: select on early half, judge on late half ---
    rt = rt.sort_values("entry_ts")
    cut = rt["entry_ts"].quantile(0.5)
    early, late = rt[rt.entry_ts <= cut], rt[rt.entry_ts > cut]
    early_pnl, late_pnl = float(early["net"].sum()), float(late["net"].sum())
    persists = (early_pnl > 0) and (late_pnl > 0)

    # weekly sign vector (regime-dependence eyeball, e.g. all PnL from one window)
    wk = rt.set_index("entry_ts")["net"].resample("W").sum()
    sign = "".join("+" if v > 0 else ("-" if v < 0 else "0") for v in wk)
    wk_pos = (wk > 0).sum(); wk_tot = len(wk)

    passed = slow_ok and band_ok and size_ok and conc_ok and persists
    return dict(n_rt=len(rt), n_mkts=int(n_mkts),
                slow=round(slow_metric,1), slow_src=slow_src, slow_ok=slow_ok,
                band=round(band_share,2), band_ok=band_ok,
                med_notional=round(med_notional,1), size_ok=size_ok,
                top5=round(float(top5_share),2), n_mkt_ok=conc_ok,
                early=round(early_pnl,0), late=round(late_pnl,0), persists=persists,
                wk=f"{wk_pos}/{wk_tot}", sign=sign, PASS=passed)

# --------------------------------------------------------------------------- commands --
def load_universe(args):
    if args.wallets_csv:
        u = pd.read_csv(args.wallets_csv)
        wcol = "wallet" if "wallet" in u.columns else u.columns[0]
        names = dict(zip(u[wcol].str.lower(), u["name"])) if "name" in u.columns else {}
        return [w.lower() for w in u[wcol].tolist()], names
    print(f"pulling OVERALL/ALL leaderboard (top ~{args.top_n})...")
    rows = fetch_leaderboard(args.data_base, "OVERALL", "ALL", args.top_n)
    print(f"  got {len(rows)}")
    wallets, names = [], {}
    for r in rows[:args.top_n]:
        w = (r.get("proxyWallet") or "").lower()
        if w.startswith("0x"):
            wallets.append(w); names[w] = r.get("userName") or "?"
    return wallets, names

def cmd_screen(args):
    wallets, names = load_universe(args)
    print(f"\nscreening {len(wallets)} wallets "
          f"(slip={args.slippage}, fee_bps={args.fee_bps}, "
          f"slow>={args.min_hold_hrs}h, band>={args.min_band_share}, "
          f"min_notional=${args.min_notional}, meta={'on' if args.fetch_market_meta else 'off'})\n")
    results = []
    for i, w in enumerate(wallets):
        nm = names.get(w, "?")
        print(f"[{i+1}/{len(wallets)}] {nm} {w[:10]}...", end=" ", flush=True)
        trades = fetch_trades(args.data_base, w, args.max_trades)
        if not trades:
            print("no trades"); continue
        if i == 0:
            print("\n  RAW TRADE OBJECT (confirm field names):")
            print("  " + json.dumps(trades[0], indent=2)[:400])
        rt, _ = build_roundtrips(trades)
        meta = None
        if args.fetch_market_meta and not rt.empty and "conditionId" in rt:
            cids = [c for c in rt["conditionId"].dropna().unique().tolist()]
            meta = fetch_market_meta(args.gamma_base, cids, debug=(i == 0))
        res = screen_wallet(rt, meta, args)
        if res is None:
            print(f"  <{args.min_roundtrips} round-trips, skip"); continue
        res.update(name=nm, wallet=w)
        results.append(res)
        print(f"  {'PASS' if res['PASS'] else 'fail'}  "
              f"slow={res['slow']}h({res['slow_src']}) band={res['band']} "
              f"notl=${res['med_notional']} mkts={res['n_mkts']} top5={res['top5']} "
              f"early={res['early']:+.0f} late={res['late']:+.0f} wk={res['wk']}")

    if not results:
        print("\nno wallets characterized."); return
    df = pd.DataFrame(results)
    cols = ["name","wallet","n_rt","n_mkts","slow","slow_src","slow_ok","band","band_ok",
            "med_notional","size_ok","top5","n_mkt_ok","early","late","persists","wk","sign","PASS"]
    df = df[[c for c in cols if c in df.columns]]
    df.to_csv(args.out, index=False)

    n = len(df); n_pass = int(df["PASS"].sum())
    n_persist_cand = int((df["early"] > 0).sum())      # eligible for the persistence coin-flip
    exp_null = 0.5 * n_persist_cand                     # if late-profit were a coin flip
    print(f"\n{'='*70}")
    print(f"tested {n} | passed every gate: {n_pass}")
    for g in ["slow_ok","band_ok","size_ok","n_mkt_ok","persists"]:
        if g in df: print(f"  {g:10s}: {int(df[g].sum())}/{n}")
    print(f"\nMULTIPLE-TESTING NULL:")
    print(f"  {n_persist_cand} wallets were profitable in the early window (persistence-eligible).")
    print(f"  If forward profitability were a coin flip, ~{exp_null:.0f} would 'persist' by chance.")
    print(f"  observed persisting: {int(df['persists'].sum())}. "
          f"{'>' if df['persists'].sum() > exp_null else '<='} chance expectation.")
    print(f"  => a 'PASS' here is a CANDIDATE, not an edge. Only the forward log confirms.")
    print(f"\nsaved -> {args.out}")
    if n_pass:
        print("\nsurvivors (feed these to `forward`):")
        for _, r in df[df.PASS].iterrows():
            print(f"  {r['wallet']},{r['name']}")

def cmd_forward(args):
    """Freeze survivors today; append their trades occurring AFTER freeze to a log.
    Run daily/weekly. Later, PnL on post-freeze rows only = clean out-of-sample copy record."""
    u = pd.read_csv(args.wallets_csv)
    wcol = "wallet" if "wallet" in u.columns else u.columns[0]
    wallets = [w.lower() for w in u[wcol].tolist()]
    names = dict(zip(u[wcol].str.lower(), u["name"])) if "name" in u.columns else {}

    freeze_path = args.out + ".freeze"
    if not os.path.exists(freeze_path):
        ft = int(datetime.now(timezone.utc).timestamp())
        with open(freeze_path, "w") as f: f.write(str(ft))
        print(f"FROZE survivor set at {datetime.now(timezone.utc).isoformat()} (ts={ft})")
    freeze_ts = int(open(freeze_path).read().strip())
    print(f"freeze ts = {freeze_ts}; logging trades strictly after it\n")

    seen = set()
    if os.path.exists(args.out):
        old = pd.read_csv(args.out)
        seen = set(old["_key"].tolist()) if "_key" in old.columns else set()

    new_rows = []
    for w in wallets:
        trades = fetch_trades(args.data_base, w, args.max_trades)
        for t in trades:
            ts = t.get("timestamp")
            try: ts = int(float(ts))
            except (TypeError, ValueError): continue
            if ts <= freeze_ts:
                continue
            key = f"{w}:{t.get('transactionHash','')}:{t.get('asset','')}:{ts}:{t.get('side','')}"
            if key in seen:
                continue
            new_rows.append({"_key": key, "wallet": w, "name": names.get(w, "?"),
                             "ts": ts, "side": t.get("side"), "asset": t.get("asset"),
                             "price": t.get("price"), "size": t.get("size"),
                             "conditionId": t.get("conditionId"), "title": (t.get("title") or "")[:80]})
        time.sleep(SLEEP)

    if new_rows:
        ndf = pd.DataFrame(new_rows)
        hdr = not os.path.exists(args.out)
        ndf.to_csv(args.out, mode="a", header=hdr, index=False)
        print(f"appended {len(new_rows)} new post-freeze trades -> {args.out}")
    else:
        print("no new post-freeze trades this run.")

# ----------------------------------------------------------------------------------- cli --
def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-base", dest="data_base", default=DEFAULT_DATA)
    ap.add_argument("--gamma-base", dest="gamma_base", default=DEFAULT_GAMMA)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("screen", help="run copyability gates + persistence over a universe")
    s.add_argument("--wallets-csv", help="CSV with a 'wallet' (+optional 'name') column; "
                                         "omit to pull the ALL-time leaderboard instead")
    s.add_argument("--top-n", type=int, default=200)
    s.add_argument("--max-trades", type=int, default=8000)
    s.add_argument("--min-roundtrips", type=int, default=30)
    s.add_argument("--slippage", type=float, default=0.02, help="per-side, in price units")
    s.add_argument("--fee-bps", type=float, default=0.0, help="swap in your verified fee model for real numbers")
    s.add_argument("--min-hold-hrs", type=float, default=48.0, help="slow-market gate")
    s.add_argument("--min-band-share", type=float, default=0.40, help="notional share in 30-70c")
    s.add_argument("--min-notional", type=float, default=20.0, help="median round-trip $ must clear fees")
    s.add_argument("--max-markets", type=int, default=500, help="reject 90k-diffuse grinders")
    s.add_argument("--max-top5-share", type=float, default=0.85, help="reject one-regime-luck wallets")
    s.add_argument("--fetch-market-meta", action="store_true", help="enrich TTR from Gamma (else hold-proxy)")
    s.add_argument("--out", default="screen_results.csv")
    s.set_defaults(func=cmd_screen)

    f = sub.add_parser("forward", help="freeze survivors + append post-freeze trades")
    f.add_argument("--wallets-csv", required=True, help="survivor CSV from screen output")
    f.add_argument("--max-trades", type=int, default=3000)
    f.add_argument("--out", default="forward_log.csv")
    f.set_defaults(func=cmd_forward)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
