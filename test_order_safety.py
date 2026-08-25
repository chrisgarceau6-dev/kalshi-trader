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
            kalshi_auth.cancel_order("order-123", "KXETH15M-TEST")
        delete.assert_called_once_with(
            "/portfolio/events/orders/order-123",
            {"exchange_index": -1, "market_ticker": "KXETH15M-TEST"},
        )

    def test_cancel_routes_to_the_ticker_shard(self):
        """A cancel that lands on the wrong shard 404s, and the caller reads 404 as
        'already gone' — so an unrouted cancel silently leaks a resting GTC order."""
        with patch.object(kalshi_auth, "delete", return_value=(200, {})) as delete:
            kalshi_auth.cancel_order("order-123", "KXBTC15M-26AUG251200-00")
        params = delete.call_args[0][1]
        self.assertEqual(params["exchange_index"], -1)
        self.assertEqual(params["market_ticker"], "KXBTC15M-26AUG251200-00")

    def test_place_order_routes_by_ticker_not_shard_zero(self):
        """Kalshi defaults exchange_index to 0 when the field is absent; crypto lives
        on shard 2 since 2026-08-24, so an absent field means HTTP 404 on every order."""
        captured = {}

        def fake_post(path, body):
            captured.update({"path": path, "body": body})
            return 201, {"order_id": "order-123"}

        with patch.object(kalshi_auth, "post", side_effect=fake_post):
            kalshi_auth.place_order(
                "KXBTC15M-26AUG251200-00", "yes", 10, yes_price_cents=Decimal("92"))
        self.assertIn("exchange_index", captured["body"])
        self.assertEqual(captured["body"]["exchange_index"], -1)

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

        cancel.assert_called_once_with("order-123", "KXETH15M-TEST")

    def test_stop_balance_reads_the_account_total_not_the_shard(self):
        """STOP_BALANCE is calibrated against the whole account. If the stop read the
        shard balance instead, funding the shard to exactly $400 would halt the bot on
        the spot (400 <= 400)."""
        state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}
        halted, reason = trader.check_halts(
            state, balance=1201.19, shard_balance=float(trader.STOP_BALANCE))
        self.assertFalse(halted, f"healthy account halted: {reason}")

    def test_drained_shard_halts_instead_of_firing_rejected_orders(self):
        """A shard that cannot fund the book must halt loudly. Left unchecked it fails
        the way 2026-08-25 failed: gates pass, orders bounce, logs look like a drought."""
        state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}
        halted, reason = trader.check_halts(state, balance=1201.19, shard_balance=10.0)
        self.assertTrue(halted)
        self.assertIn("collateral", reason)

    def test_shard_collateral_halt_is_not_sticky(self):
        """It must clear itself when funds land — a brake, not a latch needing a human."""
        state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}
        self.assertTrue(trader.check_halts(state, 1201.19, 10.0)[0])
        self.assertFalse(trader.check_halts(state, 1201.19, 400.0)[0])

    def test_missing_breakdown_fails_open_to_the_total(self):
        """A subaccount-restricted key omits balance_breakdown. An unknown shard
        balance must never be able to halt a healthy bot."""
        resp = {"balance_dollars": "1201.1910"}
        with patch.object(trader, "kalshi_get", return_value=(200, resp)):
            total, shard = trader.fetch_balance()
        self.assertAlmostEqual(total, 1201.191)
        self.assertAlmostEqual(shard, 1201.191)

    def test_fetch_balance_picks_the_trading_shard(self):
        resp = {"balance_dollars": "1201.1910", "balance_breakdown": [
            {"exchange_index": 0, "balance": "801.1910"},
            {"exchange_index": 2, "balance": "400.0000"}]}
        with patch.object(trader, "kalshi_get", return_value=(200, resp)):
            total, shard = trader.fetch_balance()
        self.assertAlmostEqual(total, 1201.191)
        self.assertAlmostEqual(shard, 400.0)

    def test_halt_alerts_once_per_transition_not_once_per_scan(self):
        """~54 scans per job, a job every ~15 min. Alerting every cycle is ~96 emails
        an hour and gets muted inside a day, which is worse than no alert."""
        state = {}
        with patch.object(trader, "send_email") as mail:
            for _ in range(50):
                trader.alert_halt(state, "shard 2 collateral $10.00 < $50.00")
        self.assertEqual(mail.call_count, 1)

    def test_dedup_survives_live_numbers_in_the_reason(self):
        """Reasons embed counters that change every cycle ('cooldown 43min'). Dedup
        compares the KEY, so a changing number must not re-alert."""
        state = {}
        with patch.object(trader, "send_email") as mail:
            trader.alert_halt(state, "9 consec losses, cooldown 59min")
            trader.alert_halt(state, "9 consec losses, cooldown 58min")
            trader.alert_halt(state, "9 consec losses, cooldown 12min")
        self.assertEqual(mail.call_count, 1)

    def test_a_different_halt_reason_alerts_immediately(self):
        state = {}
        with patch.object(trader, "send_email") as mail:
            trader.alert_halt(state, "9 consec losses, cooldown 59min")
            trader.alert_halt(state, "balance $390.00 <= stop $400")
        self.assertEqual(mail.call_count, 2)

    def test_recovery_rearms_so_a_recurrence_alerts_again(self):
        state = {}
        with patch.object(trader, "send_email") as mail:
            trader.alert_halt(state, "shard 2 collateral $10.00 < $50.00")
            trader.clear_halt_alert(state)
            trader.alert_halt(state, "shard 2 collateral $10.00 < $50.00")
        self.assertEqual(mail.call_count, 2)

    def test_standing_halt_re_alerts_after_the_repeat_window(self):
        """A halt still standing hours later must not be forgotten."""
        state = {}
        with patch.object(trader, "send_email") as mail:
            trader.alert_halt(state, "shard 2 collateral $10.00 < $50.00")
            state["halt_alert"]["ts"] -= trader.HALT_ALERT_REPEAT_SECONDS + 1
            trader.alert_halt(state, "shard 2 collateral $10.00 < $50.00")
        self.assertEqual(mail.call_count, 2)

    def test_no_fill_heartbeat_catches_the_2026_08_25_failure_shape(self):
        """Rejected orders produce NO halt, so halt alerting alone would have missed
        the outage entirely. The heartbeat is what actually catches it."""
        now = trader.datetime.now(trader.ET).timestamp()
        state = {"last_fill_ts": now - (trader.NO_FILL_ALERT_SECONDS + 60)}
        with patch.object(trader, "send_email") as mail:
            trader.alert_if_no_fills(state)
        self.assertEqual(mail.call_count, 1)

    def test_no_fill_heartbeat_stays_quiet_inside_a_normal_gap(self):
        """Largest gap ever observed is 105 min; that must never page."""
        now = trader.datetime.now(trader.ET).timestamp()
        state = {"last_fill_ts": now - 105 * 60}
        with patch.object(trader, "send_email") as mail:
            trader.alert_if_no_fills(state)
        self.assertEqual(mail.call_count, 0)

    def test_no_fill_heartbeat_does_not_spam_while_still_quiet(self):
        now = trader.datetime.now(trader.ET).timestamp()
        state = {"last_fill_ts": now - (trader.NO_FILL_ALERT_SECONDS + 60)}
        with patch.object(trader, "send_email") as mail:
            for _ in range(50):
                trader.alert_if_no_fills(state)
        self.assertEqual(mail.call_count, 1)

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
             patch.object(trader, "FLAT_BET_DOLLARS", 75), \
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
        # 75 is the bet this test pins above; reading the module attribute here
        # would compare against whatever the live config happens to be.
        self.assertLessEqual(position["cost"], 75)
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
             patch.object(trader, "_fresh_ask_cents", side_effect=[Decimal("91"), Decimal("87")]), \
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
        # Tripwire: bet size is a risk decision and must never drift silently.
        # 75 -> 50 on 2026-08-19 to restore the <=4.6%-of-balance ratio.
        # 50 -> 25 on 2026-08-22 for survival: balance $979.62 left only 6.6 losses
        # of headroom above the $650 stop after Aug 21-22 ran -$383.42.
        self.assertEqual(trader.FLAT_BET_DOLLARS, 25)
        self.assertLessEqual(Decimal(count) * Decimal("0.93"), Decimal("25"))
        self.assertEqual(count, 26)

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

    # ── ambiguous order POST (2026-08-22) ─────────────────────────────────
    # A POST that times out or 5xxs may still have been ACCEPTED. Assuming failure
    # leaves a live order the bot cannot see, cancel or reconcile. All three outcomes
    # of the client_order_id lookup must be distinguishable.

    def test_ambiguous_post_adopts_an_order_that_actually_landed(self):
        found = {"client_order_id": "cid-1", "order_id": "srv-9", "status": "resting"}
        with patch.object(trader, "kalshi_get",
                          return_value=(200, {"orders": [found], "cursor": None})):
            self.assertEqual(trader.find_order_by_client_id("cid-1"), found)

    def test_ambiguous_post_confirms_absence_when_lookup_succeeds(self):
        with patch.object(trader, "kalshi_get",
                          return_value=(200, {"orders": [], "cursor": None})):
            # False, not None: searched successfully and it is genuinely not there.
            self.assertIs(trader.find_order_by_client_id("cid-1"), False)

    def test_ambiguous_post_returns_unknown_when_lookup_itself_fails(self):
        with patch.object(trader, "kalshi_get", return_value=(503, {})):
            # None, not False — "could not look" must never be read as "not there",
            # because that is what would leave a live order untracked.
            self.assertIsNone(trader.find_order_by_client_id("cid-1"))

    def test_client_order_id_lookup_pages_until_found(self):
        pages = [
            (200, {"orders": [{"client_order_id": "other"}], "cursor": "c2"}),
            (200, {"orders": [{"client_order_id": "cid-1", "order_id": "srv-9"}],
                   "cursor": None}),
        ]
        with patch.object(trader, "kalshi_get", side_effect=pages):
            got = trader.find_order_by_client_id("cid-1")
        self.assertEqual(got.get("order_id"), "srv-9")

    def test_totally_unreadable_book_blocks_the_trade(self):
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
        # Changed 2026-08-22 (was: an API error must not block a valid entry).
        # A 503 on the book read leaves BOTH depth and last-look unverified, and the
        # last-look guard exists precisely because a marketable order sweeps a crashed
        # book upward regardless of the limit sent. Ordering blind into that is the
        # failure mode that produced the 47c/57c/83c fills on 2026-08-18. Measured cost
        # of closing it: 0 of the 6 orders since the $25 cut had an unreadable book.
        self.assertNotIn("KXETH15M-TEST", state["positions"])

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
        """A cent under the floor is the book moving, not a crash-through. Only fills
        deeper than CRASH_FILL_TOLERANCE are worth an email.

        The probe price is taken from _band_min(side), not hardcoded: under v5.17 the
        YES floor is 88c, so 89c is INSIDE the band and would not be underfilled at
        all. Pinning 89c here asserted the pre-v5.17 symmetric band."""
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

        shallow = float(trader._band_min("yes")) - 1      # 1c under, inside tolerance
        mail, state = run(shallow)
        mail.assert_not_called()
        self.assertTrue(state["positions"]["KXETH15M-TEST"]["outside_safe_zone"])

        mail, _ = run(57.0)
        self.assertEqual(mail.call_count, 1)
        self.assertIn("CRASH FILL", mail.call_args.args[0])

    # ── survivor shadow log (logs only, never trades) ──────────────────────

    def _survivor_market(self, no_ask="0.9500", secs=200):
        return {
            "ticker": "KXBTC15M-TEST",
            "event_ticker": "KXBTC15M-TEST",
            "close_time": "2026-08-18T20:00:00Z",
            "yes_ask_dollars": "0.0500",
            "no_ask_dollars": no_ask,
            "_secs_left": secs,
        }

    def _run_survivor(self, market, early_yes_bid="0.0750"):
        """early_yes_bid 0.0750 -> the NO ask six minutes ago was 92.5c."""
        candles = {"candlesticks": [{
            "end_period_ts": 1787083200 - 540,     # 540s before the 20:00Z close
            "yes_ask": {"close_dollars": "0.9250"},
            "yes_bid": {"close_dollars": early_yes_bid},
        }]}
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_SURVIVOR_SEEN", set()), \
             patch.object(trader, "kalshi_get", return_value=(200, candles)), \
             patch.object(trader, "place_order") as place, \
             patch.object(trader, "log") as logger:
            trader.shadow_survivor(market, "KXBTC15M")
        place.assert_not_called()
        return " ".join(str(c.args[0]) for c in logger.call_args_list)

    def test_survivor_signal_is_logged_and_never_traded(self):
        out = self._run_survivor(self._survivor_market())
        self.assertIn("SHADOW:SURVIVOR94", out)
        self.assertIn("KXBTC15M-TEST", out)

    def test_survivor_ignores_contracts_that_were_not_92_93c_earlier(self):
        # yes_bid 0.02 -> the NO ask six minutes ago was 98c, not 92-93c
        out = self._run_survivor(self._survivor_market(), early_yes_bid="0.0200")
        self.assertNotIn("SURVIVOR94", out)

    def test_survivor_ignores_wrong_price_or_time(self):
        self.assertNotIn("SURVIVOR94", self._run_survivor(self._survivor_market(no_ask="0.9100")))
        self.assertNotIn("SURVIVOR94", self._run_survivor(self._survivor_market(secs=400)))

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


class BandAsymmetryTests(unittest.TestCase):
    """The 88-89c extension is YES-only and must stay that way.

    Over the full archive 88-89c measures YES +$0.39/tr against NO -$0.42/tr.
    Trading both sides cancels to -$2.07/day, which is exactly why every
    symmetric MIN_ASK sweep found nothing. A future 'tidy up the band
    constants' change that symmetrises this silently reintroduces a -EV
    population, so it is pinned here rather than left to a comment.
    """

    def test_yes_reaches_88_but_no_does_not(self):
        self.assertEqual(trader._band_min("yes"), 88)
        self.assertEqual(trader._band_min("no"), trader.MIN_ASK_CENTS)
        for ask in (88, 89):
            self.assertTrue(trader._in_band(ask, "yes"),
                            f"YES must be allowed at {ask}c")
            self.assertFalse(trader._in_band(ask, "no"),
                             f"NO must be REJECTED at {ask}c (-$0.42/tr)")

    def test_shared_band_unchanged_for_both_sides(self):
        for ask in (90, 91, 92, 93):
            self.assertTrue(trader._in_band(ask, "yes"))
            self.assertTrue(trader._in_band(ask, "no"))

    def test_outside_band_rejected_on_both_sides(self):
        for ask in (87, 94, 96):
            self.assertFalse(trader._in_band(ask, "yes"))
            self.assertFalse(trader._in_band(ask, "no"))

    def test_none_ask_is_never_in_band(self):
        self.assertFalse(trader._in_band(None, "yes"))
        self.assertFalse(trader._in_band(None, "no"))


class BandReachabilityTests(unittest.TestCase):
    """Every cent the constants DECLARE must actually reach place_order.

    The four tests above pin _band_min()/_in_band(). Those helpers were correct the
    whole time — the deciding gate simply never called them. v5.17 shipped with the
    88-89c YES band unreachable, the suite green, and a pre-registration whose clock
    could never start, because nothing exercised the entry path end to end.

    These drive the real try_trade with a book at each price and assert on whether an
    order is actually placed. A future symmetric 'cleanup' of any band check now fails
    here rather than silently going inert.
    """

    def _drive(self, side, price):
        """Run try_trade against a book offering `side` at `price`. -> place_order mock."""
        yes_ask = price if side == "yes" else 100 - price
        book = {"orderbook_fp": {
            "no_dollars":  [[f"{(100 - yes_ask) / 100:.4f}", "500"]],
            "yes_dollars": [[f"{yes_ask / 100:.4f}", "500"]],
        }}
        market = {
            "ticker": "KXETH15M-TEST", "event_ticker": "KXETH15M-TEST",
            "yes_ask_dollars": f"{yes_ask / 100:.4f}",
            "no_ask_dollars": f"{(100 - yes_ask) / 100:.4f}",
            "_secs_left": 300,
        }
        state = {"positions": {}, "stats": {"trades": 0, "wins": 0, "pnl": 0.0}}
        priors = [Decimal("95"), Decimal("95"), Decimal("95")]
        with tempfile.TemporaryDirectory() as td, \
             patch.object(trader, "STATE_FILE", Path(td) / "state.json"), \
             patch.object(trader, "LOG_FILE", Path(td) / "trader.log"), \
             patch.object(trader, "_fresh_ask_cents", return_value=Decimal(str(price))), \
             patch.object(trader, "_prior_k_candle_asks", return_value=priors), \
             patch.object(trader, "kalshi_get", return_value=(200, book)), \
             patch.object(trader, "place_order", return_value=(201, {"order_id": "o-1"})) as place, \
             patch.object(trader, "cancel_order", return_value=(200, {})), \
             patch.object(trader, "reconcile_terminal_order", return_value=(10.0, price * 10 / 100.0, 0.0)), \
             patch.object(trader, "ORDER_MAX_ATTEMPTS", 1), \
             patch.object(trader.time, "sleep"), \
             patch.object(trader, "send_email"), \
             patch.dict(os.environ, {"KALSHI_API_KEY_ID": "test"}):
            trader.try_trade(market, state, False, balance=1000, live_position_tickers=set())
        return place, state

    def test_every_declared_cent_reaches_an_order(self):
        for side in ("yes", "no"):
            lo, hi = int(trader._band_min(side)), int(trader.MAX_ASK_CENTS)
            for price in range(lo, hi + 1):
                with self.subTest(side=side, price=price):
                    place, _ = self._drive(side, price)
                    self.assertEqual(
                        place.call_count, 1,
                        f"{side.upper()} {price}c is inside the declared band "
                        f"[{lo},{hi}] but no order was placed — a gate is using a "
                        f"hardcoded bound instead of _band_min(side)")

    def test_below_the_declared_floor_never_orders(self):
        for side in ("yes", "no"):
            price = int(trader._band_min(side)) - 1
            with self.subTest(side=side, price=price):
                place, _ = self._drive(side, price)
                self.assertEqual(place.call_count, 0,
                                 f"{side.upper()} {price}c is below the floor and must skip")

    def test_no_side_never_reaches_the_yes_only_extension(self):
        """The 88-89c extension is YES-only: NO there measures -$0.42/tr."""
        for price in range(int(trader.LOW_BAND_MIN_CENTS), int(trader.MIN_ASK_CENTS)):
            with self.subTest(price=price):
                place, _ = self._drive("no", price)
                self.assertEqual(place.call_count, 0,
                                 f"NO must never order at {price}c")

    def test_intended_low_band_fill_is_not_flagged_a_crash(self):
        """An 88-89c YES fill is the INTENDED entry, not a crash through the floor."""
        if trader.LOW_BAND_MIN_CENTS >= trader.MIN_ASK_CENTS:
            self.skipTest("no side-asymmetric band configured")
        _, state = self._drive("yes", int(trader.LOW_BAND_MIN_CENTS))
        self.assertFalse(state["positions"]["KXETH15M-TEST"]["outside_safe_zone"],
                         "a fill at the declared YES floor must not be flagged "
                         "outside_safe_zone — that flag stops top-ups and corrupts "
                         "any later crash-fill analysis")


class EmergencyBrakeTests(unittest.TestCase):
    """The halts are circuit breakers, not drawdown optimisers.

    Both must sit ABOVE normal variance, and must KEEP sitting above it when the bet
    size changes. A fixed dollar threshold does not: max(300, bet*4) was validated at
    $75 and, at $25, ended up nine dollars past the worst 24h stretch in 74 days of
    archive — firing once and blocking nothing. These pin the property, not the value,
    so the next sizing change cannot silently disarm them.

    Measured over 74 days at live config (docs/audit/claude/replay_loss_limit.py):
    worst rolling-24h P&L -$309.25 at $25, and 21 losses in the worst window at ANY
    bet size, because loss count is set by win rate and volume rather than by sizing.
    """

    WORST_24H_AT_25 = 309.25       # dollars, live config, 0.227c slip
    WORST_24H_LOSSES = 21          # bet-size invariant

    def test_daily_limit_clears_the_worst_measured_day_at_every_bet_size(self):
        for bet in (10, 25, 50, 75, 100):
            with self.subTest(bet=bet):
                limit = trader.compute_daily_loss_limit(bet)
                worst = self.WORST_24H_AT_25 * (bet / 25.0)   # scales with principal
                self.assertGreater(
                    limit, worst * 1.25,
                    f"at ${bet}/trade the limit is ${limit} against a worst measured "
                    f"24h of ${worst:.0f} — that is a drawdown control, not an "
                    f"emergency brake, and it will halt during ordinary variance")

    def test_daily_limit_scales_with_the_bet(self):
        """A fixed threshold is a different control at every size. This is the D1 bug."""
        a, b = trader.compute_daily_loss_limit(25), trader.compute_daily_loss_limit(75)
        self.assertAlmostEqual(b / a, 3.0, places=6,
                               msg="the limit must be proportional to the bet; a "
                                   "constant silently disarms when the bet is cut")

    def test_daily_limit_is_not_absurdly_loose(self):
        """Emergency-only is not the same as absent."""
        for bet in (25, 75):
            self.assertLess(trader.compute_daily_loss_limit(bet), bet * 40)

    def test_stop_balance_leaves_room_for_a_normal_drawdown(self):
        """The cash floor must not fire on a drawdown the strategy has survived."""
        worst_dd = 543.79          # peak-to-trough over 74 days at $25
        balance_when_set = 1227.48
        headroom = balance_when_set - trader.STOP_BALANCE
        self.assertGreater(
            headroom, worst_dd * 1.25,
            f"STOP_BALANCE={trader.STOP_BALANCE} leaves ${headroom:.0f} of headroom "
            f"against a ${worst_dd} worst measured drawdown — it would halt during "
            f"a stretch the strategy has already survived")
        self.assertGreater(trader.STOP_BALANCE, 0,
                           "a zero floor is not a stop")


class TopUpDepthTests(unittest.TestCase):
    """One order must not carry two depth thresholds."""

    def test_top_up_uses_the_same_depth_requirement_as_the_first_attempt(self):
        src = Path(trader.__file__).read_text()
        top_up = src.split("TOP-UP STOP — thin book")[0][-400:]
        self.assertIn("retry_depth < need_depth", top_up,
                      "the top-up depth gate must use need_depth (the dynamic, "
                      "order-sized requirement), not the legacy MIN_BOOK_DEPTH "
                      "constant — a smaller order needs LESS depth, not more")


if __name__ == "__main__":
    unittest.main()
