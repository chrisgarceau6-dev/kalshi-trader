#!/usr/bin/env python3
"""Prove which asks can actually reach place_order in the live entry path.

READ-ONLY on the trading path. It imports `late_certainty_trader` and rebinds the
network functions on the imported module object; nothing on disk is touched.

Every API call `try_trade` makes is answered consistently for a market whose ask on
the probed side is `ask` cents, with a deep book and priors well above every
threshold. The ONLY thing that can stop the order is a band gate. Run it after any
change to the entry band and check the reachable range against what the constants
declare.

    python3 docs/audit/claude/probe_v517.py     # exit 1 if a declared band is dead

v5.17 declares YES [88,93] and NO [90,93]. Reachable is [90,93] on both sides,
because the last-look gate is hardcoded to MIN_ASK_CENTS instead of _band_min(side).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import late_certainty_trader as T

DEPTH = 200          # far above min_book_depth(), so depth never binds
PRIOR = 95           # prior ask on the probed side, in cents


def make_stub(side, ask, prior=PRIOR):
    """Answer every endpoint the entry path touches, all agreeing on `ask`."""
    yes_ask = ask if side == "yes" else 100 - ask
    # A NO ask of X is a YES bid of 100-X, and vice versa.
    prior_yes_ask = prior if side == "yes" else 99
    prior_yes_bid = 94 if side == "yes" else 100 - prior

    def stub(path, params=None):
        if path.startswith("/markets/") and path.endswith("/orderbook"):
            # Kalshi quotes both sides as bids: buying YES lifts NO bids, so a NO bid
            # at P is a YES offer at 1-P. `_book_side_levels` inverts it back.
            return 200, {"orderbook_fp": {
                "no_dollars":  [[f"{(100 - yes_ask) / 100:.4f}", str(DEPTH)]],
                "yes_dollars": [[f"{yes_ask / 100:.4f}", str(DEPTH)]],
            }}
        if path.startswith("/markets/"):
            return 200, {"market": {"yes_ask_dollars": f"{yes_ask / 100:.4f}",
                                    "no_ask_dollars": f"{(100 - yes_ask) / 100:.4f}"}}
        if "candlesticks" in path:
            return 200, {"candlesticks": [
                {"end_period_ts": 1000 + 60 * i,
                 "yes_ask": {"close_dollars": f"{prior_yes_ask / 100:.4f}"},
                 "yes_bid": {"close_dollars": f"{prior_yes_bid / 100:.4f}"}}
                for i in range(8)]}
        return 200, {}
    return stub


def probe(side, ask):
    lines = []
    T.log = lambda m: lines.append(m)
    T.kalshi_get = make_stub(side, ask)
    T.place_order = lambda *a, **k: (201, {"order_id": "stub"})
    yes_ask = ask if side == "yes" else 100 - ask
    state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
             "recent_results": []}
    market = {"ticker": "KXBTC15M-26AUG24-T1", "event_ticker": "KXBTC15M-26AUG24",
              "_secs_left": 400, "close_time": "2026-08-24T20:00:00Z",
              "yes_ask_dollars": f"{yes_ask / 100:.4f}",
              "no_ask_dollars": f"{(100 - yes_ask) / 100:.4f}"}
    try:
        T.try_trade(market, state, dry_run=True, balance=1000.0,
                    live_position_tickers=set(), resting_order_tickers=[])
    except Exception as exc:
        lines.append(f"EXC {type(exc).__name__}: {exc}")
    ordered = any("TRADE:" in m for m in lines)
    blocked = next((m.strip() for m in lines if "SKIP" in m or "EXC" in m), "")
    return ordered, blocked


def main():
    print(f"{T.STRATEGY_VERSION}: constants declare "
          f"YES [{T._band_min('yes')},{T.MAX_ASK_CENTS}]c  "
          f"NO [{T._band_min('no')},{T.MAX_ASK_CENTS}]c\n")
    dead = 0
    for side in ("yes", "no"):
        lo = int(T._band_min(side))
        print(f"  {side.upper()}")
        reachable = []
        for ask in range(lo - 2, int(T.MAX_ASK_CENTS) + 2):
            ordered, blocked = probe(side, ask)
            if ordered:
                reachable.append(ask)
            print(f"    {ask}c  {'ORDER' if ordered else '  -  '}  {blocked[:62]}")
        want = list(range(lo, int(T.MAX_ASK_CENTS) + 1))
        if reachable != want:
            got = f"{reachable[0]}-{reachable[-1]}c" if reachable else "NOTHING"
            print(f"    !! DEAD BAND: constants declare {want[0]}-{want[-1]}c, "
                  f"reachable is {got}")
            dead += 1
        else:
            print(f"    ok: reachable == declared {want[0]}-{want[-1]}c")
        print()
    if dead:
        print(f"FAIL: {dead} declared band(s) are not reachable.")
    return 1 if dead else 0


if __name__ == "__main__":
    sys.exit(main())
