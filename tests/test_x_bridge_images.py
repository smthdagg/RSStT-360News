import unittest
from datetime import datetime, timezone
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

    def test_x_screenshot_is_the_only_media_enclosure(self):
        rss = generate_rss("daydayuplift", [{
            "id": "126", "url": "https://x.com/i/web/status/126", "text": "post",
            "screenshot_url": "http://127.0.0.1:1200/tweet-image/126.png?v=15",
            "media": [{"type": "photo", "url": "https://pbs.twimg.com/media/original.jpg"}],
        }])
        self.assertIn("tweet-image/126.png?v=15", rss)
        self.assertNotIn("original.jpg", rss)

    def test_rss_keeps_only_main_tweet_text(self):
        rss = generate_rss("daydayuplift", [{
            "id": "124", "url": "https://x.com/i/web/status/124", "text": "main",
            "quoted_tweet": {"url": "https://x.com/i/web/status/old"},
            "retweeted_tweet": {"text": "old retweet", "user": {"screen_name": "someone"}},
        }])
        self.assertIn("main", rss)
        self.assertNotIn("old retweet", rss)
        self.assertNotIn("Quote:", rss)

    def test_rss_includes_x_tweet_metadata_with_delivery_delay(self):
        rss = generate_rss("daydayuplift", [{
            "id": "125", "url": "https://x.com/i/web/status/125", "text": "main",
            "date": datetime(2026, 9, 10, 4, 0, tzinfo=timezone.utc),
            "screenshot_time": datetime(2026, 9, 10, 4, 5, 7, tzinfo=timezone.utc),
            "user": {"name": "天天乐", "screen_name": "daydayuplift"},
        }])
        self.assertIn("作者：天天乐 (@daydayuplift)", rss)
        self.assertIn("截图推送时间：", rss)
        self.assertIn("时间差：5分钟7秒", rss)
        self.assertIn("原文链接：https://x.com/i/web/status/125", rss)


if __name__ == "__main__":
    unittest.main()
