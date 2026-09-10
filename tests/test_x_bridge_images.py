import unittest
from unittest.mock import patch

from src.twitter_rss_bridge import configured_x_usernames, generate_rss


class XBridgeImageTests(unittest.TestCase):
    def test_config_limits_fetching_to_configured_usernames(self):
        with patch(
            "src.twitter_rss_bridge.get_bridge_config",
            return_value={"users": ["daydayuplift"]},
        ):
            self.assertEqual(configured_x_usernames(), {"daydayuplift"})

    def test_rss_includes_the_local_tweet_screenshot_as_an_image(self):
        rss = generate_rss(
            "daydayuplift",
            [{
                "id": "123",
                "url": "https://x.com/i/web/status/123",
                "text": "post",
                "screenshot_url": "http://127.0.0.1:1200/tweet-image/123.png",
            }],
        )

        self.assertIn('enclosure url="http://127.0.0.1:1200/tweet-image/123.png" type="image/png"', rss)


if __name__ == "__main__":
    unittest.main()
