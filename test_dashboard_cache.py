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
import re
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


class WhatIfSliderTests(unittest.TestCase):
    """The what-if slider used to rebuild its own card from its 'input' handler, which
    destroyed the <input> being dragged: the thumb jumped one step and froze, and on
    touch it barely moved. Verified against a real drag through CDP — pre-fix the value
    stuck at 25 for all 8 drag steps and the node did not survive. That needs a browser,
    so what is guarded here is the structural invariant that made it possible."""

    def setUp(self):
        self.js = dash.HTML[dash.HTML.index("function renderWhatif"):]
        self.js = self.js[:self.js.index("\n}\n") + 3]

    def test_markup_is_built_once_not_on_every_input(self):
        body = self.js
        self.assertIn("el.dataset.built", body,
                      "the card must be built behind a one-time guard")
        guard = body.index("if(!el.dataset.built)")
        self.assertGreater(body.index("el.innerHTML="), guard,
                           "innerHTML must only be assigned inside the build-once guard")

    def test_the_input_handler_does_not_rebuild_the_card(self):
        handler = self.js[self.js.index("addEventListener('input'"):]
        handler = handler[:handler.index("\n")]
        self.assertIn("paintWhatif", handler)
        self.assertNotIn("renderWhatif", handler,
                         "calling renderWhatif from its own input handler destroys "
                         "the slider mid-drag — this is the exact bug")

    def test_a_background_refresh_does_not_yank_a_thumb_being_dragged(self):
        self.assertIn("document.activeElement!==r", self.js)


class DeployMarkerTests(unittest.TestCase):
    """The deploy diamonds were removed: hover-only titles are unreachable on a phone,
    and they sat over the chart with pointer-events:auto."""

    def test_no_deploy_marker_markup_or_styles_remain(self):
        for token in ("dep-t", "depWrap", "renderDeploys"):
            self.assertNotIn(token, dash.HTML, f"{token} should be gone")

    def test_payload_no_longer_ships_deploys(self):
        self.assertFalse(hasattr(dash, "get_deploys"),
                         "get_deploys is dead code once the markers are gone")


class ScrollContainerTests(unittest.TestCase):
    """Trackpad scrolling died on macOS Chrome while the scrollbar still worked.

    Cause: `html,body{overflow-x:hidden}`. Setting overflow-x on body forces its
    overflow-y to compute to `auto`, so BODY becomes a second scroll container nested
    inside the viewport scroller. A wheel gesture targets body, body has nothing to
    scroll, and the gesture is swallowed rather than chaining to the viewport —
    dragging the scrollbar still moves the viewport, which is why it looked selective.

    Measured with the rule removed: scrollWidth === clientWidth at every width from
    320 to 2200 in both modes, so it was never load-bearing.
    """

    def _css(self):
        return dash.HTML[:dash.HTML.index("</style>")]

    def test_body_never_gets_an_overflow_x_rule(self):
        css = self._css()
        for m in re.finditer(r"([^{}]*)\{([^{}]*)\}", css):
            sel, body = m.group(1), m.group(2)
            if "overflow-x" not in body and "overflow:" not in body:
                continue
            targets = [t.strip() for t in sel.split(",")]
            self.assertNotIn("body", targets,
                             f"overflow on body makes it a scroll container: {sel.strip()}")
            self.assertNotIn("html,body", [t.replace(" ", "") for t in targets])

    def test_the_root_clips_rather_than_hiding(self):
        """`clip` clips without creating a scroll container; `hidden` does not."""
        self.assertIn("html{overflow-x:clip}", self._css())


class PolishTests(unittest.TestCase):
    """Small interaction details, guarded structurally because the browser proof needs
    CDP. Verified there first: a BTC win marks only the BTC row `settled win`, the
    390px strip shows fade-r at rest and fade-l scrolled to the end, and tween() is
    instant under reduced motion."""

    def test_the_settle_flash_is_applied_after_render_not_on_a_frame_callback(self):
        """announce() runs before the series rows are rebuilt, so the flash must be
        recorded then applied at the end of render. A requestAnimationFrame defer
        works in a live tab but does nothing in a background tab or headless."""
        js = dash.HTML
        fn = js[js.index("function flashSeries"):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertNotIn("requestAnimationFrame", fn)
        self.assertIn("pendingFlash", fn)
        self.assertIn("applyFlash();", js, "the flash must be applied inside render()")
        # ...and after the innerHTML that builds the rows it marks
        self.assertGreater(js.index("applyFlash();"), js.index("renderOverview(sett, d);"))

    def test_tween_honours_prefers_reduced_motion(self):
        """The global reduced-motion CSS rule cannot reach a requestAnimationFrame
        counter, so tween has to check the query itself."""
        js = dash.HTML
        fn = js[js.index("function tween(el,to,fmt)"):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("RM.matches", fn)
        self.assertIn("prefers-reduced-motion", js)

    def test_the_range_strip_advertises_that_it_scrolls(self):
        """At 390px the strip cut ~35px with no affordance at all."""
        js = dash.HTML
        self.assertIn("fade-r", js)
        self.assertIn("fade-l", js)
        fn = js[js.index("function rangeFade"):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn("scrollWidth", fn)
        self.assertIn("scrollLeft", fn)


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
