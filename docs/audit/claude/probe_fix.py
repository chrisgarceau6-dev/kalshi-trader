#!/usr/bin/env python3
"""How much of the hardcoded-90 defect does a one-line fix actually repair?

`docs/audit/codex/LIVE_SPEC.md` lists four sites where `MIN_ASK_CENTS` is used in
place of `_band_min(side)`; `docs/audit/claude/LIVE_SPEC.md` §6 argues the last look
alone is what blocks. Both are incomplete, and the difference decides how large the
fix has to be — so it is measured rather than argued.

METHOD, and why it does not touch the trading path: the live module is COPIED to a
scratch directory, the copy is patched at the last-look comparison only, and the copy
is imported. `late_certainty_trader.py` is never written to, and this script refuses
to run if the copy is not byte-identical to the live file before patching.

Every API call is stubbed to answer consistently for a market quoted at `ask` on the
probed side, with a deep book and priors clearing every threshold, and the order is
carried through to a simulated terminal fill at the same price. So the only thing that
can change an outcome between rows is a band gate.

    python3 docs/audit/claude/probe_fix.py

Result at v5.17: patching ONLY the last look makes 88-89c YES place an order, but the
fill is flagged `outside_safe_zone` and top-ups stop. Sufficient to trade, not
sufficient to trade correctly.
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

LIVE = Path(__file__).resolve().parents[3] / "late_certainty_trader.py"
AUTH = LIVE.parent / "kalshi_auth.py"

# The first-attempt last look and the top-up last look share a prefix, so the
# comparison alone is NOT unique — matching on it silently hits both. Anchor on the
# whole statement including its log call, and let the count check below prove it.
OLD = (
    '    if best_offer is not None and not '
    '(MIN_ASK_CENTS <= best_offer <= MAX_ASK_CENTS):\n'
    '        log(f"  SKIP {ticker} — last look: best {side} offer {best_offer}c '
    'is outside "\n'
    '            f"[{MIN_ASK_CENTS},{MAX_ASK_CENTS}]c while the quote still said '
    '{fresh_ask}c")\n'
    '        return'
)
NEW = OLD.replace('MIN_ASK_CENTS <= best_offer', '_band_min(side) <= best_offer', 1)


def load_patched():
    """Copy the live module, patch the last look only, import the copy."""
    tmp = Path(tempfile.mkdtemp(prefix="probe_fix_"))
    shutil.copy(LIVE, tmp / "fixtest.py")
    shutil.copy(AUTH, tmp / "kalshi_auth.py")
    src = (tmp / "fixtest.py").read_text()
    if src != LIVE.read_text():
        sys.exit("copy differs from the live file; aborting")
    if src.count(OLD) != 1:
        sys.exit(f"expected exactly 1 last-look site, found {src.count(OLD)} — "
                 f"the entry path changed; re-read it before trusting this probe")
    (tmp / "fixtest.py").write_text(src.replace(OLD, NEW, 1))
    sys.path.insert(0, str(tmp))
    import fixtest
    return fixtest


T = load_patched()


def stub_for(ask, fill_at):
    def stub(path, params=None):
        if path.endswith("/orderbook"):
            return 200, {"orderbook_fp": {
                "no_dollars":  [[f"{(100 - ask) / 100:.4f}", "200"]],
                "yes_dollars": [[f"{ask / 100:.4f}", "200"]]}}
        if path.startswith("/portfolio/orders/"):
            ct = T.contracts_for_risk(T.FLAT_BET_DOLLARS, min(T.MAX_ASK_CENTS, ask + 2))
            return 200, {"order": {
                "status": "canceled", "remaining_count_fp": "0",
                "fill_count_fp": str(ct),
                "taker_fill_cost_dollars": f"{ct * fill_at / 100:.4f}",
                "maker_fill_cost_dollars": "0",
                "taker_fees_dollars": "0.05", "maker_fees_dollars": "0"}}
        if path.startswith("/markets/"):
            return 200, {"market": {"yes_ask_dollars": f"{ask / 100:.4f}",
                                    "no_ask_dollars": f"{(100 - ask) / 100:.4f}"}}
        if "candlesticks" in path:
            return 200, {"candlesticks": [
                {"end_period_ts": 1000 + 60 * i,
                 "yes_ask": {"close_dollars": "0.9500"},
                 "yes_bid": {"close_dollars": "0.9400"}} for i in range(8)]}
        return 200, {}
    return stub


def run(ask):
    """Place a YES order at `ask` and fill it there. Returns (position, log lines)."""
    lines, placed = [], []
    T.kalshi_get = stub_for(ask, ask)
    T.log = lines.append
    T.place_order = lambda *a, **k: (placed.append(k), (201, {"order_id": "stub"}))[1]
    T.cancel_order = lambda oid: (200, {})
    T.send_email = lambda subject, body: None
    state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
             "recent_results": []}
    try:
        T.try_trade({"ticker": "KXBTC15M-PROBE-T1", "event_ticker": "KXBTC15M-PROBE",
                     "_secs_left": 400, "close_time": "2026-01-01T20:00:00Z",
                     "yes_ask_dollars": f"{ask / 100:.4f}",
                     "no_ask_dollars": f"{(100 - ask) / 100:.4f}"},
                    state, dry_run=False, balance=1000.0,
                    live_position_tickers=set(), resting_order_tickers=[])
    except Exception as exc:
        lines.append(f"EXC {type(exc).__name__}: {exc}")
    return state["positions"].get("KXBTC15M-PROBE-T1"), lines, len(placed)


def main():
    os.environ.setdefault("KALSHI_API_KEY_ID", "probe")
    print(f"\n{T.STRATEGY_VERSION}: last-look gate patched to _band_min(side); "
          f"every other MIN_ASK_CENTS site left as-is.\n")
    print(f"  {'ask':>4}{'orders':>8}{'position':>10}{'contracts':>11}"
          f"{'outside_safe_zone':>19}   note")
    degraded = []
    for ask in range(int(T._band_min("yes")), int(T.MAX_ASK_CENTS) + 1):
        pos, lines, n = run(ask)
        note = next((m.strip()[:44] for m in lines
                     if "under the band" in m or "CRASH FILL" in m), "")
        flag = pos.get("outside_safe_zone") if pos else None
        print(f"  {ask:>4}{n:>8}{('yes' if pos else 'NO'):>10}"
              f"{(pos['contracts'] if pos else 0):>11.0f}{str(flag):>19}   {note}")
        if pos and flag:
            degraded.append(ask)
    print()
    if degraded:
        print(f"  Unblocked, but MISLABELLED at {degraded[0]}-{degraded[-1]}c: the fill is "
              f"flagged outside_safe_zone\n  and the `break` at the end of that branch "
              f"stops top-ups, so the order fills once\n  instead of up to "
              f"ORDER_MAX_ATTEMPTS={T.ORDER_MAX_ATTEMPTS} times.\n")
        print("  => one line unblocks the band; four sites are needed to trade it "
              "correctly.\n     See docs/audit/claude/DIFF.md §3.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
