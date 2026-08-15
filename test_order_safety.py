import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import kalshi_auth
import late_certainty_trader as trader


class OrderSafetyTests(unittest.TestCase):
    def test_v2_cancel_uses_v2_endpoint(self):
        with patch.object(kalshi_auth, "delete", return_value=(200, {})) as delete:
            kalshi_auth.cancel_order("order-123")
        delete.assert_called_once_with("/portfolio/events/orders/order-123")

    def test_place_order_respects_time_in_force_and_expiration(self):
        captured = {}

        def fake_post(path, body):
            captured.update({"path": path, "body": body})
            return 201, {"order_id": "order-123"}

        with patch.object(kalshi_auth, "post", side_effect=fake_post):
            kalshi_auth.place_order(
                "TEST-TICKER",
                "yes",
                10,
                yes_price_cents=Decimal("92.8"),
                time_in_force="good_till_canceled",
                expiration_time=1_800_000_003,
                client_order_id="client-123",
            )

        self.assertEqual(captured["path"], "/portfolio/events/orders")
        self.assertEqual(captured["body"]["time_in_force"], "good_till_canceled")
        self.assertEqual(captured["body"]["expiration_time"], 1_800_000_003)
        self.assertEqual(captured["body"]["client_order_id"], "client-123")
        self.assertEqual(captured["body"]["price"], "0.9280")

    def test_v2_top_level_order_id_is_cancelled(self):
        state = {
            "positions": {},
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        market = {
            "ticker": "KXETH15M-TEST",
            "event_ticker": "KXETH15M-TEST",
            "yes_ask_dollars": "0.9100",
            "no_ask_dollars": "0.0900",
            "_secs_left": 300,
        }

        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", return_value=Decimal("91")), \
             patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
             patch.object(trader, "place_order", return_value=(201, {"order_id": "order-123", "fill_count": "1.00", "remaining_count": "0.00"})), \
             patch.object(trader, "cancel_order", return_value=(200, {})) as cancel, \
             patch.object(trader, "reconcile_terminal_order", return_value=(1.0, 0.91, 0.0)), \
             patch.object(trader, "ORDER_MAX_ATTEMPTS", 1), \
             patch.object(trader.time, "sleep"), \
             patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
            trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())

        cancel.assert_called_once_with("order-123")

    def test_partial_fill_is_topped_up_without_exceeding_budget(self):
        state = {
            "positions": {},
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        market = {
            "ticker": "KXETH15M-TEST",
            "event_ticker": "KXETH15M-TEST",
            "yes_ask_dollars": "0.9100",
            "no_ask_dollars": "0.0900",
            "_secs_left": 300,
        }
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", side_effect=[Decimal("91"), Decimal("91")]), \
             patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
             patch.object(trader, "place_order", side_effect=[
                 (201, {"order_id": "order-1"}),
                 (201, {"order_id": "order-2"}),
             ]) as place, \
             patch.object(trader, "cancel_order", return_value=(200, {})), \
             patch.object(trader, "reconcile_terminal_order", side_effect=[
                 (13.51, 12.5643, 0.0),
                 (67.0, 62.31, 0.0),
             ]), \
             patch.object(trader.time, "sleep"), \
             patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
            trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())

        self.assertEqual([call.args[2] for call in place.call_args_list], [80, 67])
        position = state["positions"][market["ticker"]]
        self.assertAlmostEqual(position["cost"], 74.8743)
        self.assertLessEqual(position["cost"], trader.FLAT_BET_DOLLARS)
        self.assertEqual(position["order_ids"], ["order-1", "order-2"])
        self.assertEqual(state["stats"]["trades"], 1)

    def test_top_up_stops_when_fresh_ask_leaves_safe_zone(self):
        state = {
            "positions": {},
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        market = {
            "ticker": "KXETH15M-TEST",
            "event_ticker": "KXETH15M-TEST",
            "yes_ask_dollars": "0.9100",
            "no_ask_dollars": "0.0900",
            "_secs_left": 300,
        }
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", side_effect=[Decimal("91"), Decimal("89")]), \
             patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
             patch.object(trader, "place_order", return_value=(201, {"order_id": "order-1"})) as place, \
             patch.object(trader, "cancel_order", return_value=(200, {})), \
             patch.object(trader, "reconcile_terminal_order", return_value=(13.51, 12.5643, 0.0)), \
             patch.object(trader.time, "sleep"), \
             patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
            trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())

        self.assertEqual(place.call_count, 1)
        self.assertAlmostEqual(state["positions"][market["ticker"]]["cost"], 12.5643)

    def test_configured_contract_count_never_exceeds_flat_risk_at_limit(self):
        count = trader.contracts_for_risk(
            bet_dollars=trader.FLAT_BET_DOLLARS,
            limit_cents=Decimal("93"),
        )
        self.assertEqual(trader.FLAT_BET_DOLLARS, 75)
        self.assertLessEqual(Decimal(count) * Decimal("0.93"), Decimal("75"))
        self.assertEqual(count, 80)

    def test_subpenny_boundaries_are_not_rounded_into_band(self):
        with patch.object(trader, "kalshi_get", return_value=(200, {"market": {"yes_ask_dollars": "0.8950"}})):
            self.assertLess(trader._fresh_ask_cents("T", "yes"), Decimal("90"))
        with patch.object(trader, "kalshi_get", return_value=(200, {"market": {"yes_ask_dollars": "0.9340"}})):
            self.assertGreater(trader._fresh_ask_cents("T", "yes"), Decimal("93"))

    def test_corrupt_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"
            state_file.write_text("not-json")
            with patch.object(trader, "STATE_FILE", state_file):
                with self.assertRaises(RuntimeError):
                    trader.load_state()

    def test_version_change_marks_carried_open_positions(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "state.json"
            log_file = Path(td) / "trader.log"
            state_file.write_text(
                '{"strategy_version":"v5.11","positions":{"T":{"settled":false}},'
                '"stats":{"trades":1,"wins":0,"pnl":0},"recent_results":[]}'
            )
            with patch.object(trader, "STATE_FILE", state_file), patch.object(trader, "LOG_FILE", log_file):
                state = trader.load_state()
        self.assertEqual(state["strategy_version"], trader.STRATEGY_VERSION)
        self.assertEqual(state["positions"]["T"]["strategy_version"], "v5.11")
        self.assertEqual(state["stats"], {"trades": 0, "wins": 0, "pnl": 0.0})

    def test_carried_position_settlement_does_not_corrupt_new_version_wr(self):
        state = {
            "strategy_version": trader.STRATEGY_VERSION,
            "positions": {
                "T": {
                    "settled": False,
                    "side": "yes",
                    "contracts": 1,
                    "cost": 0.9,
                    "fee_cost": 0,
                    "strategy_version": "v5.11",
                }
            },
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
            "recent_results": [],
        }
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "kalshi_get", return_value=(200, {"market": {"status": "settled", "result": "yes"}})):
            trader.check_outcomes(state, 1000)
        self.assertEqual(state["stats"], {"trades": 0, "wins": 0, "pnl": 0.0})
        self.assertTrue(state["positions"]["T"]["settled"])
        self.assertEqual(state.get("consec_losses", 0), 0)

    def test_position_api_failure_is_not_treated_as_no_exposure(self):
        with patch.object(trader, "kalshi_get", return_value=(503, {})):
            self.assertIsNone(trader.fetch_live_position_tickers())

    def test_fill_api_failure_is_not_treated_as_no_fill(self):
        with patch.object(trader, "kalshi_get", return_value=(503, {})):
            self.assertIsNone(trader.query_actual_fill("T", "yes", "order-123"))

    def test_existing_live_exposure_counts_toward_cap(self):
        state = {
            "positions": {
                "STATE-OPEN": {"settled": False},
            },
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        market = {
            "ticker": "KXETH15M-TEST",
            "event_ticker": "KXETH15M-TEST",
            "yes_ask_dollars": "0.9100",
            "no_ask_dollars": "0.0900",
            "_secs_left": 300,
        }
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "place_order") as place:
            trader.try_trade(
                market,
                state,
                False,
                balance=1000,
                live_position_tickers={"LIVE-OPEN"},
            )
        place.assert_not_called()

    def test_each_resting_order_consumes_a_concurrency_slot(self):
        state = {
            "positions": {},
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        market = {
            "ticker": "KXETH15M-TEST",
            "event_ticker": "KXETH15M-TEST",
            "yes_ask_dollars": "0.9100",
            "no_ask_dollars": "0.0900",
            "_secs_left": 300,
        }
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "place_order") as place:
            trader.try_trade(
                market,
                state,
                False,
                balance=1000,
                live_position_tickers=set(),
                resting_order_tickers=["OTHER", "OTHER"],
            )
        place.assert_not_called()

    def test_missing_order_id_sets_persistent_execution_halt(self):
        state = {
            "positions": {},
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        market = {
            "ticker": "KXETH15M-TEST",
            "event_ticker": "KXETH15M-TEST",
            "yes_ask_dollars": "0.9100",
            "no_ask_dollars": "0.0900",
            "_secs_left": 300,
        }
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", return_value=Decimal("91")), \
             patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
             patch.object(trader, "place_order", return_value=(201, {"fill_count": "0", "remaining_count": "107"})), \
             patch.object(trader, "send_email"), \
             patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
            with self.assertRaises(RuntimeError):
                trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())
        self.assertIn("no order_id", state.get("execution_halt_reason", ""))

    def test_btc_utc_09_is_not_an_unvalidated_live_blackout(self):
        state = {
            "positions": {},
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        market = {
            "ticker": "KXBTC15M-26AUG140915-15",
            "event_ticker": "KXBTC15M-26AUG140915",
            "yes_ask_dollars": "0.9100",
            "no_ask_dollars": "0.0900",
            "_secs_left": 300,
        }
        with tempfile.TemporaryDirectory() as td:
            log_file = Path(td) / "trader.log"
            with patch.object(trader, "LOG_FILE", log_file), \
                 patch.object(trader, "_fresh_ask_cents", return_value=Decimal("91")), \
                 patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]):
                trader.try_trade(market, state, True, balance=1000, live_position_tickers=set())
            self.assertIn("TRADE:", log_file.read_text())

    def test_order_record_recovers_late_partial_fills(self):
        order = {
            "status": "executed",
            "fill_count_fp": "49.00",
            "remaining_count_fp": "0.00",
            "taker_fill_cost_dollars": "0.000000",
            "maker_fill_cost_dollars": "45.570000",
            "taker_fees_dollars": "0.000000",
            "maker_fees_dollars": "0.000000",
        }
        with patch.object(trader, "query_order", return_value=order):
            contracts, cost, fee = trader.reconcile_terminal_order("order-123", "T", "yes")
        self.assertEqual(contracts, 49.0)
        self.assertEqual(cost, 45.57)
        self.assertEqual(fee, 0.0)

    def test_zero_cost_order_record_falls_back_to_complete_fills(self):
        order = {
            "status": "executed",
            "fill_count_fp": "49.00",
            "remaining_count_fp": "0.00",
            "taker_fill_cost_dollars": "0.000000",
            "maker_fill_cost_dollars": "0.000000",
            "taker_fees_dollars": "0.000000",
            "maker_fees_dollars": "0.000000",
        }
        with patch.object(trader, "query_order", return_value=order), \
             patch.object(trader, "query_actual_fill", return_value=(49.0, 45.57, 0.0)):
            contracts, cost, fee = trader.reconcile_terminal_order("order-123", "T", "yes")
        self.assertEqual((contracts, cost, fee), (49.0, 45.57, 0.0))


if __name__ == "__main__":
    unittest.main()
