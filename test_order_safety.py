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
             patch.object(trader, "_book_last_look", return_value=(Decimal("91"), None)), \
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
             patch.object(trader, "_book_last_look", return_value=(Decimal("91"), None)), \
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
             patch.object(trader, "_book_last_look", return_value=(Decimal("91"), None)), \
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
             patch.object(trader, "_book_last_look", return_value=(Decimal("91"), None)), \
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
                 patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
                 patch.object(trader, "_book_last_look", return_value=(Decimal("91"), None)):
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


    def test_thin_book_skips_trade(self):
        state = {
            "positions": {},
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        market = {
            "ticker": "KXBNB15M-TEST",
            "event_ticker": "KXBNB15M-TEST",
            "yes_ask_dollars": "0.9100",
            "no_ask_dollars": "0.0900",
            "_secs_left": 300,
        }
        thin_orderbook = {
            "orderbook_fp": {
                "no_dollars": [["0.09", "10"], ["0.08", "5"]],
            }
        }
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", return_value=Decimal("91")), \
             patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
             patch.object(trader, "kalshi_get", return_value=(200, thin_orderbook)), \
             patch.object(trader, "place_order") as place:
            trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())
        place.assert_not_called()

    def test_book_depth_api_error_does_not_block_trade(self):
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
             patch.object(trader, "kalshi_get", return_value=(503, {})), \
             patch.object(trader, "place_order", return_value=(201, {"order_id": "order-123", "fill_count": "1.00", "remaining_count": "0.00"})), \
             patch.object(trader, "cancel_order", return_value=(200, {})), \
             patch.object(trader, "reconcile_terminal_order", return_value=(1.0, 0.91, 0.0)), \
             patch.object(trader, "ORDER_MAX_ATTEMPTS", 1), \
             patch.object(trader.time, "sleep"), \
             patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
            trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())
        self.assertIn("KXETH15M-TEST", state["positions"])

    # ── v5.16: NO side live ────────────────────────────────────────────────
    # A NO entry must be liquidity-checked against the YES bid side. The YES-path
    # helper (_book_depth_at_max_ask) reads NO bids, which for a NO entry is the
    # opposite side of the book — it would happily pass a NO order into a book with
    # no YES bids at all.

    def _no_side_market(self, ticker="KXBTC15M-TEST"):
        return {
            "ticker": ticker,
            "event_ticker": ticker,
            "yes_ask_dollars": "0.0900",   # YES is cheap -> NO is the expensive side
            "no_ask_dollars": "0.9100",
            "_secs_left": 300,
        }

    def test_no_side_trades_when_yes_bid_book_is_deep(self):
        state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}
        deep = {"orderbook_fp": {
            "yes_dollars": [["0.09", "500"], ["0.08", "300"]],   # NO offers, ample
            "no_dollars":  [["0.91", "500"]],
        }}
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", return_value=Decimal("91")), \
             patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
             patch.object(trader, "kalshi_get", return_value=(200, deep)), \
             patch.object(trader, "place_order", return_value=(201, {"order_id": "o-1", "fill_count": "1.00", "remaining_count": "0.00"})) as place, \
             patch.object(trader, "cancel_order", return_value=(200, {})), \
             patch.object(trader, "reconcile_terminal_order", return_value=(1.0, 0.91, 0.0)), \
             patch.object(trader, "ORDER_MAX_ATTEMPTS", 1), \
             patch.object(trader.time, "sleep"), \
             patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
            trader.try_trade(self._no_side_market(), state, False, balance=1000,
                             live_position_tickers=set())
        place.assert_called()
        self.assertEqual(state["positions"]["KXBTC15M-TEST"]["side"], "no")

    def test_book_depth_no_reads_yes_bid_side(self):
        """_book_depth_no must count YES bids at >= (1 - limit), i.e. the offers a
        NO buyer actually lifts. NO bids must not contribute."""
        book = {"orderbook_fp": {
            "yes_dollars": [["0.09", "40"], ["0.08", "25"], ["0.02", "999"]],
            "no_dollars":  [["0.91", "900"]],
        }}
        with patch.object(trader, "kalshi_get", return_value=(200, book)):
            depth = trader._book_depth_no("KXBTC15M-TEST", trader.MAX_ASK_CENTS)
        # 0.09 and 0.08 are >= 1-0.93 = 0.07 and count; 0.02 is below and does not.
        # The 900 NO bids must be ignored entirely.
        self.assertEqual(depth, 65.0)

    def _book_side_read_for(self, market):
        """Return the side try_trade asked the order book about."""
        state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", return_value=Decimal("91")), \
             patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
             patch.object(trader, "_book_side_levels", return_value=[]) as levels, \
             patch.object(trader, "place_order") as place:
            trader.try_trade(market, state, False, balance=1000,
                             live_position_tickers=set())
        # An empty book is zero depth, so the trade is skipped either way; the point
        # of the test is purely WHICH side of the book was measured.
        place.assert_not_called()
        return [c.args[1] for c in levels.call_args_list]

    def test_no_entry_measures_the_no_side_of_the_book(self):
        """Regression: before v5.16 a NO entry was liquidity-checked against NO bids
        — the wrong side entirely."""
        sides = self._book_side_read_for(self._no_side_market())
        self.assertEqual(sides, ["no"], "NO entry must read the NO side of the book")

    def test_yes_entry_still_measures_the_yes_side_of_the_book(self):
        market = {
            "ticker": "KXSOL15M-TEST",
            "event_ticker": "KXSOL15M-TEST",
            "yes_ask_dollars": "0.9100",
            "no_ask_dollars": "0.0900",
            "_secs_left": 300,
        }
        sides = self._book_side_read_for(market)
        self.assertEqual(sides, ["yes"], "YES entry must read the YES side of the book")

    # ── crash-through protection ───────────────────────────────────────────
    # A buy limit is a ceiling, not a floor: a marketable order sweeps the book
    # upward from the best offer, so a crashed book gets bought at crash prices
    # whatever limit we send. On Aug 18 this filled 80 BTC NO at 47c on a 92.5c
    # quote. The book read is the last call before the order for exactly this.

    def test_crashed_book_blocks_the_order(self):
        state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}
        market = {
            "ticker": "KXBTC15M-TEST",
            "event_ticker": "KXBTC15M-TEST",
            "yes_ask_dollars": "0.0900",
            "no_ask_dollars": "0.9250",
            "_secs_left": 227,
        }
        # Quote still says 92.5c; the book has already collapsed to 47c.
        crashed = {"orderbook_fp": {"yes_dollars": [["0.53", "400"], ["0.52", "300"]]}}
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", return_value=Decimal("92.5")), \
             patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
             patch.object(trader, "kalshi_get", return_value=(200, crashed)), \
             patch.object(trader, "place_order") as place, \
             patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
            trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())
        place.assert_not_called()

    def test_spiked_book_blocks_the_order(self):
        """The same guard upward: a book that ran past MAX_ASK is no longer good EV."""
        state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}
        market = {
            "ticker": "KXSOL15M-TEST",
            "event_ticker": "KXSOL15M-TEST",
            "yes_ask_dollars": "0.9100",
            "no_ask_dollars": "0.0900",
            "_secs_left": 300,
        }
        spiked = {"orderbook_fp": {"no_dollars": [["0.02", "500"]]}}   # YES offered at 98c
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", return_value=Decimal("91")), \
             patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
             patch.object(trader, "kalshi_get", return_value=(200, spiked)), \
             patch.object(trader, "place_order") as place, \
             patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
            trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())
        place.assert_not_called()

    def test_limit_is_priced_off_the_book_not_the_stale_quote(self):
        """The quote is refetched before the candle gates and is 2-4 calls stale by
        the time the order goes out; the book read is what the order is priced on."""
        state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}
        market = {
            "ticker": "KXETH15M-TEST",
            "event_ticker": "KXETH15M-TEST",
            "yes_ask_dollars": "0.9300",
            "no_ask_dollars": "0.0700",
            "_secs_left": 300,
        }
        # Quote 93c, book best offer 90c: limit must be 92c (90+2), not 93c.
        book = {"orderbook_fp": {"no_dollars": [["0.10", "500"], ["0.09", "400"]]}}
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", return_value=Decimal("93")), \
             patch.object(trader, "_prior_k_candle_asks", return_value=[Decimal("80"), Decimal("80"), Decimal("80")]), \
             patch.object(trader, "kalshi_get", return_value=(200, book)), \
             patch.object(trader, "place_order", return_value=(201, {"order_id": "o-1"})) as place, \
             patch.object(trader, "cancel_order", return_value=(200, {})), \
             patch.object(trader, "reconcile_terminal_order", return_value=(1.0, 0.90, 0.0)), \
             patch.object(trader, "ORDER_MAX_ATTEMPTS", 1), \
             patch.object(trader.time, "sleep"), \
             patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
            trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())
        self.assertEqual(place.call_args.kwargs["yes_price_cents"], Decimal("92"))

    def test_shallow_underfill_does_not_alert_but_a_crash_fill_does(self):
        """89c on a 90c band is the book moving, not a crash-through. Only fills
        deeper than CRASH_FILL_TOLERANCE are worth an email."""
        book = {"orderbook_fp": {"no_dollars": [["0.09", "500"]]}}   # YES offered at 91c

        def run(fill_price):
            state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}
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
                 patch.object(trader, "kalshi_get", return_value=(200, book)), \
                 patch.object(trader, "place_order", return_value=(201, {"order_id": "o-1"})), \
                 patch.object(trader, "cancel_order", return_value=(200, {})), \
                 patch.object(trader, "reconcile_terminal_order", return_value=(10.0, fill_price * 10 / 100.0, 0.0)), \
                 patch.object(trader, "ORDER_MAX_ATTEMPTS", 1), \
                 patch.object(trader.time, "sleep"), \
                 patch.object(trader, "send_email") as mail, \
                 patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
                trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())
            return mail, state

        mail, state = run(89.0)
        mail.assert_not_called()
        self.assertTrue(state["positions"]["KXETH15M-TEST"]["outside_safe_zone"])

        mail, _ = run(57.0)
        self.assertEqual(mail.call_count, 1)
        self.assertIn("CRASH FILL", mail.call_args.args[0])

    def test_opposite_side_of_held_ticker_is_never_re_entered(self):
        """A market that flips across its strike mid-window must not be re-entered
        on the other side — those cost -$40/trade in backtest."""
        state = {
            "positions": {"KXETH15M-TEST": {"side": "yes", "settled": False}},
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        }
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "place_order") as place:
            trader.try_trade(self._no_side_market("KXETH15M-TEST"), state, False,
                             balance=1000, live_position_tickers=set())
        place.assert_not_called()


if __name__ == "__main__":
    unittest.main()
