"""Safety tests for the NO 90-91c research instrumentation.

The single most important property: this code must never place an order.
Everything else guards against recording signals a real order would have
rejected, which is exactly the defect in the logging this replaces.
"""
import time
import unittest
from decimal import Decimal
from unittest.mock import patch

import late_certainty_trader as trader


def empty_state():
    return {
        "positions": {},
        "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        "recent_results": [],
        "strategy_version": trader.STRATEGY_VERSION,
        "shadow_no_90_91": {},
        "shadow_no_totals": trader._empty_shadow_totals(),
    }


def market(series="KXBTC15M", suffix="A", close="2026-08-18T12:15:00Z", no_ask="0.9000", secs=300):
    return {
        "ticker": f"{series}-{suffix}",
        "event_ticker": f"{series}-EVENT",
        "yes_ask_dollars": "0.1000",
        "no_ask_dollars": no_ask,
        "close_time": close,
        "_secs_left": secs,
    }


def good_gates(fresh="90", priors=(Decimal("80"), Decimal("82")), depth=999.0):
    """Patch the three just-in-time gates to a passing state."""
    return (
        patch.object(trader, "_fresh_ask_cents", return_value=Decimal(fresh)),
        patch.object(trader, "_prior_k_candle_asks", return_value=list(priors)),
        patch.object(trader, "_book_depth_no", return_value=depth),
    )


class ShadowNeverTrades(unittest.TestCase):
    def test_collect_never_places_an_order(self):
        state = empty_state()
        a, b, c = good_gates()
        with patch.object(trader, "place_order") as po, \
             patch.object(trader, "open_markets_near_close", return_value=[market()]), \
             patch.object(trader, "save_state"), a, b, c:
            trader.collect_shadow_no_signals(state)
        po.assert_not_called()

    def test_settlement_never_places_an_order(self):
        state = empty_state()
        state["shadow_no_90_91"]["T"] = {
            "ticker": "T", "contracts": 82, "cost": 74.62, "fee_cost": 0.53,
            "close_ts": 1, "portfolio_selected": True, "settled": False,
        }
        with patch.object(trader, "place_order") as po, \
             patch.object(trader, "kalshi_get", return_value=(200, {"market": {"status": "settled", "result": "no"}})):
            trader.check_shadow_no_outcomes(state)
        po.assert_not_called()

    def test_shadow_does_not_mutate_live_stats_or_positions(self):
        state = empty_state()
        a, b, c = good_gates()
        with patch.object(trader, "open_markets_near_close", return_value=[market()]), \
             patch.object(trader, "save_state"), a, b, c:
            trader.collect_shadow_no_signals(state)
        self.assertEqual(state["positions"], {})
        self.assertEqual(state["stats"], {"trades": 0, "wins": 0, "pnl": 0.0})
        self.assertEqual(state["recent_results"], [])


class ShadowFailsClosed(unittest.TestCase):
    def _reject(self, **kw):
        state = empty_state()
        a, b, c = good_gates(**kw)
        with patch.object(trader, "save_state"), a, b, c:
            added = trader.evaluate_shadow_no_candidate(market(), state)
        return added, state

    def test_rejects_when_fresh_ask_left_the_band(self):
        added, state = self._reject(fresh="95")
        self.assertFalse(added)
        self.assertEqual(state["shadow_no_90_91"], {})

    def test_rejects_when_fresh_ask_collapsed(self):
        added, _ = self._reject(fresh="80")
        self.assertFalse(added)

    def test_rejects_when_a_prior_candle_is_too_low(self):
        added, _ = self._reject(priors=(Decimal("74"), Decimal("90")))
        self.assertFalse(added)

    def test_rejects_when_book_too_thin_for_full_size(self):
        added, _ = self._reject(depth=5.0)
        self.assertFalse(added)

    def test_rejects_when_depth_unavailable(self):
        state = empty_state()
        a, b, _ = good_gates()
        with patch.object(trader, "save_state"), a, b, \
             patch.object(trader, "_book_depth_no", return_value=None):
            self.assertFalse(trader.evaluate_shadow_no_candidate(market(), state))

    def test_rejects_when_priors_unavailable(self):
        state = empty_state()
        a, _, c = good_gates()
        with patch.object(trader, "save_state"), a, c, \
             patch.object(trader, "_prior_k_candle_asks", return_value=None):
            self.assertFalse(trader.evaluate_shadow_no_candidate(market(), state))

    def test_rejects_missing_close_time(self):
        state = empty_state()
        m = market()
        del m["close_time"]
        a, b, c = good_gates()
        with patch.object(trader, "save_state"), a, b, c:
            self.assertFalse(trader.evaluate_shadow_no_candidate(m, state))

    def test_rejects_scan_ask_outside_band(self):
        state = empty_state()
        a, b, c = good_gates()
        with patch.object(trader, "save_state"), a, b, c:
            self.assertFalse(trader.evaluate_shadow_no_candidate(market(no_ask="0.9200"), state))

    def test_rejects_outside_time_window(self):
        state = empty_state()
        a, b, c = good_gates()
        with patch.object(trader, "save_state"), a, b, c:
            self.assertFalse(trader.evaluate_shadow_no_candidate(market(secs=30), state))

    def test_rejects_series_not_in_candidate_set(self):
        state = empty_state()
        a, b, c = good_gates()
        with patch.object(trader, "save_state"), a, b, c:
            self.assertFalse(trader.evaluate_shadow_no_candidate(market(series="KXWTI15M"), state))

    def test_does_not_double_record_same_ticker(self):
        state = empty_state()
        a, b, c = good_gates()
        with patch.object(trader, "save_state"), a, b, c:
            self.assertTrue(trader.evaluate_shadow_no_candidate(market(), state))
            self.assertFalse(trader.evaluate_shadow_no_candidate(market(), state))
        self.assertEqual(len(state["shadow_no_90_91"]), 1)


class ShadowClusterCap(unittest.TestCase):
    def test_only_one_no_selected_per_close_cluster(self):
        """Load-bearing: allowing two NOs per cluster is -$1,211 over 60 days."""
        state = empty_state()
        a, b, c = good_gates()
        with patch.object(trader, "save_state"), a, b, c:
            for i, s in enumerate(["KXBTC15M", "KXETH15M", "KXSOL15M"]):
                trader.evaluate_shadow_no_candidate(market(series=s, suffix=str(i)), state)
        recs = list(state["shadow_no_90_91"].values())
        self.assertEqual(len(recs), 3, "all signals should be recorded")
        self.assertEqual(sum(r["portfolio_selected"] for r in recs), 1,
                         "exactly one may be portfolio-selected per cluster")

    def test_separate_clusters_each_get_a_selection(self):
        state = empty_state()
        a, b, c = good_gates()
        with patch.object(trader, "save_state"), a, b, c:
            trader.evaluate_shadow_no_candidate(
                market(suffix="X", close="2026-08-18T12:15:00Z"), state)
            trader.evaluate_shadow_no_candidate(
                market(series="KXETH15M", suffix="Y", close="2026-08-18T12:30:00Z"), state)
        recs = list(state["shadow_no_90_91"].values())
        self.assertEqual(sum(r["portfolio_selected"] for r in recs), 2)


class ShadowEconomics(unittest.TestCase):
    def test_fee_matches_audit_model_rounded_up(self):
        # 0.07 * 82 * 0.91 * 0.09 = 0.4700...  -> ceil to cent
        self.assertEqual(trader._shadow_taker_fee(Decimal("0.91"), 82), Decimal("0.48"))

    def test_modelled_fill_is_one_cent_adverse_and_within_budget(self):
        state = empty_state()
        a, b, c = good_gates(fresh="90")
        with patch.object(trader, "save_state"), a, b, c:
            trader.evaluate_shadow_no_candidate(market(), state)
        rec = state["shadow_no_90_91"]["KXBTC15M-A"]
        self.assertEqual(rec["modelled_fill_cents"], 91.0)
        self.assertLessEqual(rec["cost"], float(trader.SHADOW_NO_BUDGET))

    def test_win_and_loss_pnl(self):
        for result, expect_win in (("no", True), ("yes", False)):
            state = empty_state()
            state["shadow_no_90_91"]["T"] = {
                "ticker": "T", "contracts": 82, "cost": 74.62, "fee_cost": 0.48,
                "close_ts": 1, "portfolio_selected": True, "settled": False,
            }
            with patch.object(trader, "kalshi_get",
                              return_value=(200, {"market": {"status": "settled", "result": result}})):
                trader.check_shadow_no_outcomes(state)
            rec = state["shadow_no_90_91"]["T"]
            self.assertTrue(rec["settled"])
            self.assertEqual(rec["won"], expect_win)
            self.assertAlmostEqual(rec["pnl"], 6.90 if expect_win else -75.10, places=2)

    def test_unsettled_market_is_not_scored(self):
        state = empty_state()
        state["shadow_no_90_91"]["T"] = {
            "ticker": "T", "contracts": 82, "cost": 74.62, "fee_cost": 0.48,
            "close_ts": 1, "portfolio_selected": True, "settled": False,
        }
        with patch.object(trader, "kalshi_get",
                          return_value=(200, {"market": {"status": "active", "result": ""}})):
            trader.check_shadow_no_outcomes(state)
        self.assertFalse(state["shadow_no_90_91"]["T"]["settled"])

    def test_only_selected_records_count_toward_totals(self):
        state = empty_state()
        state["shadow_no_90_91"] = {
            "A": {"ticker": "A", "contracts": 82, "cost": 74.62, "fee_cost": 0.48,
                  "close_ts": 1, "portfolio_selected": True, "settled": False},
            "B": {"ticker": "B", "contracts": 82, "cost": 74.62, "fee_cost": 0.48,
                  "close_ts": 1, "portfolio_selected": False, "settled": False},
        }
        with patch.object(trader, "kalshi_get",
                          return_value=(200, {"market": {"status": "settled", "result": "no"}})):
            trader.check_shadow_no_outcomes(state)
        self.assertEqual(trader.shadow_no_summary(state)["settled"], 1)


class ShadowStateHygiene(unittest.TestCase):
    def test_pruning_drops_old_settled_but_keeps_totals(self):
        state = empty_state()
        old = int(time.time()) - (trader.SHADOW_NO_PRUNE_DAYS + 1) * 86400
        state["shadow_no_90_91"] = {
            "OLD": {"ticker": "OLD", "settled": True, "settled_ts": old,
                    "close_ts": 1, "portfolio_selected": True, "pnl": 5.0},
        }
        state["shadow_no_totals"] = {"signals": 10, "settled": 9, "wins": 8,
                                     "pnl": 41.5, "clusters": [1, 2, 3]}
        trader.check_shadow_no_outcomes(state)
        self.assertEqual(state["shadow_no_90_91"], {}, "old settled record pruned")
        s = trader.shadow_no_summary(state)
        self.assertEqual((s["settled"], s["wins"], s["clusters"]), (9, 8, 3),
                         "cumulative totals survive pruning")

    def test_recent_settled_records_are_kept(self):
        state = empty_state()
        state["shadow_no_90_91"] = {
            "NEW": {"ticker": "NEW", "settled": True, "settled_ts": int(time.time()),
                    "close_ts": 1, "portfolio_selected": True, "pnl": 5.0},
        }
        trader.check_shadow_no_outcomes(state)
        self.assertIn("NEW", state["shadow_no_90_91"])

    def test_load_state_migrates_missing_shadow_keys(self):
        """A state file written before this change must load without KeyError."""
        import json
        import tempfile
        from pathlib import Path

        legacy = {"positions": {}, "stats": {"trades": 3, "wins": 3, "pnl": 9.0},
                  "recent_results": [True], "strategy_version": trader.STRATEGY_VERSION}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "certainty_state.json"
            p.write_text(json.dumps(legacy))
            with patch.object(trader, "STATE_FILE", p):
                s = trader.load_state()
        self.assertIn("shadow_no_90_91", s)
        self.assertIn("shadow_no_totals", s)
        self.assertEqual(s["stats"], {"trades": 3, "wins": 3, "pnl": 9.0},
                         "migration must not disturb live stats")


class ShadowIsolation(unittest.TestCase):
    def test_scan_failure_does_not_propagate(self):
        state = empty_state()
        with patch.object(trader, "open_markets_near_close", side_effect=RuntimeError("api down")):
            self.assertEqual(trader.collect_shadow_no_signals(state), 0)

    def test_eval_failure_does_not_propagate(self):
        state = empty_state()
        with patch.object(trader, "open_markets_near_close", return_value=[market()]), \
             patch.object(trader, "_fresh_ask_cents", side_effect=RuntimeError("boom")):
            self.assertEqual(trader.collect_shadow_no_signals(state), 0)

    def test_collection_ignores_blackout_hours(self):
        """Shadow data must not be silently dropped by a live blackout."""
        captured = {}

        def fake(series, apply_blackout=True):
            captured["apply_blackout"] = apply_blackout
            return []

        with patch.object(trader, "open_markets_near_close", side_effect=fake):
            trader.collect_shadow_no_signals(empty_state())
        self.assertFalse(captured["apply_blackout"])

    def test_open_markets_near_close_still_applies_blackout_by_default(self):
        import inspect
        sig = inspect.signature(trader.open_markets_near_close)
        self.assertIs(sig.parameters["apply_blackout"].default, True)


if __name__ == "__main__":
    unittest.main()
