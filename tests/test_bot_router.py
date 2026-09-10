import unittest

from src.bot_router import _active_route, route_for_feed, x_username_from_feed, is_secondary_feed


class BotRouterTests(unittest.TestCase):
    def test_extracts_x_username(self):
        self.assertEqual(x_username_from_feed("http://127.0.0.1:1200/twitter/user/daydayuplift"), "daydayuplift")

    def test_route_context_is_scoped(self):
        with route_for_feed("http://127.0.0.1:1200/twitter/user/daydayuplift", {"daydayuplift"}):
            self.assertEqual(x_username_from_feed("/twitter/user/daydayuplift"), "daydayuplift")

    def test_route_accepts_exact_rss_source_and_default_secondary(self):
        source = "https://example.test/feed.xml"
        with route_for_feed(source, {source}):
            self.assertEqual(_active_route.get(), "secondary")
        with route_for_feed("https://example.test/other.xml", {"*"}):
            self.assertEqual(_active_route.get(), "secondary")

    def test_secondary_feed_matches_x_username(self):
        self.assertTrue(is_secondary_feed(
            "http://127.0.0.1:1200/twitter/user/daydayuplift", {"daydayuplift"}))
        self.assertFalse(is_secondary_feed(
            "http://127.0.0.1:1200/twitter/user/other", {"daydayuplift"}))


if __name__ == "__main__":
    unittest.main()
