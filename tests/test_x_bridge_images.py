import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image as PILImage

from src.twitter_rss_bridge import XAPIWorker, configured_x_usernames, generate_rss
from src.parsing.medium import UploadedImage
from src.parsing.post_formatter import PostFormatter


class XBridgeImageTests(unittest.TestCase):
    def test_tweets_without_screenshots_are_not_published(self):
        worker = XAPIWorker()
        worker.fetcher = Mock()
        worker.fetcher.fetch_tweets.return_value = [
            {"id": "127", "url": "https://x.com/i/web/status/127", "text": "missed"},
            {"id": "128", "url": "https://x.com/i/web/status/128", "text": "captured"},
        ]
        worker.fetcher.capture_tweet_screenshot.side_effect = [
            RuntimeError("browser target closed"),
            Path("/screenshots/128.png"),
        ]

        with patch("src.twitter_rss_bridge.TWEET_IMAGE_DIR", Path("/screenshots")):
            tweets = worker.fetch_tweets("daydayuplift")

        self.assertEqual([tweet["id"] for tweet in tweets], ["128"])
        self.assertIn("/tweet-image/128.png", tweets[0]["screenshot_url"])

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

    def test_local_screenshot_is_uploaded_from_shared_file(self):
        payload = BytesIO()
        PILImage.new("RGB", (4, 4), "white").save(payload, format="PNG")
        with TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "tweet_screenshots" / "123.png"
            image_path.parent.mkdir()
            image_path.write_bytes(payload.getvalue())
            with patch("src.parsing.medium.env.config_folder_path", tmp):
                medium = PostFormatter(html="", feed_link="http://127.0.0.1:1200/twitter/user/daydayuplift") \
                    ._make_local_image("http://127.0.0.1:1200/tweet-image/123.png?v=15")

        self.assertIsInstance(medium, UploadedImage)
        self.assertEqual(medium.file, payload.getvalue())

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
