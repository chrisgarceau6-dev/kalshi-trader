"""Cache and transport behaviour for the dashboard.

These cover the two things that made the dashboard feel slow on a phone, both of
which are invisible to a screenshot and easy to reintroduce:

  * `cached()` is stale-while-revalidate. The client polls every 30s while settlements
    carry a 120s TTL, so every fourth poll used to pay the full paginated refetch with
    the browser sat on a pending request. A future edit that reverts it to a blocking
    refetch will still LOOK correct — these tests are what catches it.
  * /api/data is gzipped. It is ~540 KB of repetitive JSON polled every 30s; without
    compression an open tab pulls ~65 MB/hr.
"""
import gzip
import json
import threading
import time
import unittest

import kalshi_dashboard as dash


class CacheTests(unittest.TestCase):
    def setUp(self):
        with dash._cache_lock:
            dash._cache.clear()
            dash._refreshing.clear()
        self.calls = []

    def fn(self, value, delay=0.0):
        def _f():
            self.calls.append(value)
            if delay:
                time.sleep(delay)
            return value
        return _f

    def test_cold_miss_blocks_and_caches(self):
        self.assertEqual(dash.cached("k", 5, self.fn("A")), "A")
        self.assertEqual(dash.cached("k", 5, self.fn("B")), "A")
        self.assertEqual(self.calls, ["A"])

    def test_stale_value_is_served_without_blocking(self):
        """The whole point: an expired value comes back immediately, not after a
        refetch. A blocking implementation passes every other test but this one."""
        dash.cached("k", 0.05, self.fn("OLD"))
        time.sleep(0.1)
        started = time.time()
        value = dash.cached("k", 0.05, self.fn("NEW", delay=0.4))
        elapsed = time.time() - started
        self.assertEqual(value, "OLD")
        self.assertLess(elapsed, 0.1, "a stale read must not wait on the refetch")
        time.sleep(0.6)
        self.assertEqual(dash.cached("k", 5, self.fn("X")), "NEW")

    def test_concurrent_stale_reads_cause_one_refetch(self):
        """N viewers must not become N paginated refetches of the same key."""
        dash.cached("k", 0.05, self.fn("X"))
        time.sleep(0.1)
        self.calls.clear()
        out = []
        threads = [threading.Thread(target=lambda: out.append(
            dash.cached("k", 0.05, self.fn("Y", delay=0.3)))) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(set(out), {"X"})
        time.sleep(0.5)

    def test_past_the_ceiling_it_blocks_instead_of_serving_ancient_data(self):
        """A source that keeps failing must eventually surface as slowness, not as
        old numbers presented as live."""
        dash.cached("k", 0.02, self.fn("OLD"))
        time.sleep(0.02 * dash.STALE_CEILING + 0.1)
        started = time.time()
        value = dash.cached("k", 0.02, self.fn("NEW", delay=0.3))
        self.assertEqual(value, "NEW")
        self.assertGreaterEqual(time.time() - started, 0.25)

    def test_failing_background_refresh_does_not_wedge_the_key(self):
        """If the in-flight marker leaked on error, the key would never refresh
        again for the life of the process."""
        dash.cached("k", 0.05, self.fn("GOOD"))
        time.sleep(0.1)

        def boom():
            raise RuntimeError("api down")

        self.assertEqual(dash.cached("k", 0.05, boom), "GOOD")
        time.sleep(0.3)
        self.assertNotIn("k", dash._refreshing)
        self.assertEqual(dash.cached("k", 99, self.fn("Z")), "GOOD")


def _big_json():
    return json.dumps({"settlements": [
        {"ticker": f"KXBTC15M-26AUG30{i:04d}-30", "pnl": 1.25, "won": True}
        for i in range(2000)]})


# Routes must be registered before the app handles its first request, so they are
# attached at import rather than inside a test.
@dash.app.route("/_t/json")
def _t_json():
    return dash.app.response_class(_big_json(), mimetype="application/json")


@dash.app.route("/_t/small")
def _t_small():
    return dash.app.response_class('{"a":1}', mimetype="application/json")


@dash.app.route("/_t/html")
def _t_html():
    return dash.app.response_class("<html>" + "x" * 20000 + "</html>",
                                   mimetype="text/html")


class GzipTests(unittest.TestCase):
    def setUp(self):
        dash.app.config["TESTING"] = True
        # The token gate runs before every request; local dev with no token is the
        # path that serves normally, and it is the transport we are testing here.
        self._tok = dash.DASH_TOKEN
        dash.DASH_TOKEN = ""
        self.client = dash.app.test_client()

    def tearDown(self):
        dash.DASH_TOKEN = self._tok

    def test_json_is_gzipped_when_the_client_accepts_it(self):
        raw = self.client.get("/_t/json")
        gz = self.client.get("/_t/json", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(gz.headers.get("Content-Encoding"), "gzip")
        self.assertEqual(gz.headers.get("Vary"), "Accept-Encoding")
        self.assertLess(len(gz.data), len(raw.data) / 4)
        self.assertEqual(gzip.decompress(gz.data), raw.data)

    def test_a_client_that_does_not_accept_gzip_still_gets_plain_json(self):
        r = self.client.get("/_t/json")
        self.assertIsNone(r.headers.get("Content-Encoding"))
        self.assertEqual(len(json.loads(r.data)["settlements"]), 2000)

    def test_small_and_non_json_responses_are_left_alone(self):
        for path in ("/_t/small", "/_t/html"):
            r = self.client.get(path, headers={"Accept-Encoding": "gzip"})
            self.assertIsNone(r.headers.get("Content-Encoding"), path)


if __name__ == "__main__":
    unittest.main()
