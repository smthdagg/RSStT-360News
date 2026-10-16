import unittest
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.x_browser_fetcher import (
    classify_x_response,
    extract_tweets_from_timeline,
    parse_tweet_to_dict,
    sanitize_x_cookies,
    should_refresh_cache_entry,
    XAuthExpiredError,
    XBrowserFetcher,
    XBrowserError,
    XRateLimitedError,
    _cookie_action_priority,
    _tweet_article_selector,
)


class CookieConsentTests(unittest.TestCase):
    def test_prefers_rejecting_cookie_consent(self):
        self.assertGreater(
            _cookie_action_priority("拒绝非必要 Cookie"),
            _cookie_action_priority("接受所有 Cookie"),
        )

    def test_ignores_unrelated_buttons(self):
        self.assertEqual(_cookie_action_priority("回复"), 0)


class CookieSanitizingTests(unittest.TestCase):
    def test_only_accepts_exact_x_and_twitter_cookie_domains(self):
        cookies = [
            {"name": "auth_token", "value": "old", "domain": ".x.com", "path": "/"},
            {"name": "auth_token", "value": "new", "domain": ".x.com", "path": "/"},
            {"name": "ct0", "value": "csrf", "domain": "x.com", "path": "/"},
            {"name": "guest_id", "value": "guest", "domain": ".twitter.com", "path": "/"},
            {"name": "foreign", "value": "no", "domain": ".v2ex.com", "path": "/"},
            {"name": "foreign2", "value": "no", "domain": "notx.com", "path": "/"},
        ]

        sanitized = sanitize_x_cookies(cookies)

        self.assertEqual(
            {(cookie["name"], cookie["value"]) for cookie in sanitized},
            {("auth_token", "new"), ("ct0", "csrf"), ("guest_id", "guest")},
        )

    def test_normalizes_cookie_editor_fields_for_playwright(self):
        sanitized = sanitize_x_cookies(
            [
                {
                    "name": "auth_token",
                    "value": "secret",
                    "domain": ".x.com",
                    "path": "/",
                    "expirationDate": 2_000_000_000,
                    "sameSite": "no_restriction",
                    "httpOnly": True,
                    "secure": True,
                }
            ]
        )

        self.assertEqual(sanitized[0]["expires"], 2_000_000_000)
        self.assertEqual(sanitized[0]["sameSite"], "None")
        self.assertTrue(sanitized[0]["httpOnly"])
        self.assertTrue(sanitized[0]["secure"])


class TimelineParsingTests(unittest.TestCase):
    def test_extracts_and_deduplicates_reply_module_items(self):
        response = timeline_payload("456")
        instructions = response["data"]["user"]["result"]["timeline"]["timeline"]["instructions"]
        tweet = extract_tweets_from_timeline(response)[0]
        module_tweet = {
            "item": {
                "itemContent": {
                    "__typename": "TimelineTweet",
                    "tweet_results": {"result": tweet},
                }
            }
        }
        instructions[0]["entries"] = [{
            "content": {"items": [module_tweet, module_tweet]},
        }]

        extracted = extract_tweets_from_timeline(response)

        self.assertEqual([item["rest_id"] for item in extracted], ["456"])

    def test_extracts_visible_tweet_wrappers_and_parses_long_text(self):
        response = {
            "data": {
                "user": {
                    "result": {
                        "timeline": {
                            "timeline": {
                                "instructions": [
                                    {
                                        "type": "TimelineAddEntries",
                                        "entries": [
                                            {
                                                "content": {
                                                    "itemContent": {
                                                        "__typename": "TimelineTweet",
                                                        "tweet_results": {
                                                            "result": {
                                                                "__typename": "TweetWithVisibilityResults",
                                                                "tweet": {
                                                                    "__typename": "Tweet",
                                                                    "rest_id": "123",
                                                                    "note_tweet": {
                                                                        "note_tweet_results": {
                                                                            "result": {"text": "long post"}
                                                                        }
                                                                    },
                                                                    "legacy": {
                                                                        "full_text": "short post",
                                                                        "created_at": "Wed Jul 22 01:02:03 +0000 2026",
                                                                    },
                                                                },
                                                            }
                                                        },
                                                    }
                                                }
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }

        raw = extract_tweets_from_timeline(response)
        parsed = parse_tweet_to_dict(raw[0])

        self.assertEqual(len(raw), 1)
        self.assertEqual(parsed["id"], "123")
        self.assertEqual(parsed["text"], "long post")
        self.assertEqual(
            parsed["date"], datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc)
        )

    def test_skips_pinned_tweet_entries(self):
        response = {"data": {"user": {"result": {"timeline": {"timeline": {
            "instructions": [{"type": "TimelinePinEntry", "entry": {"content": {}}}]
        }}}}}}
        self.assertEqual(extract_tweets_from_timeline(response), [])


class SchedulingTests(unittest.TestCase):
    def test_failed_entries_wait_for_backoff_instead_of_refetching_immediately(self):
        entry = {"updated_at": 100.0, "last_attempt_at": 1_000.0, "error": "rate_limited"}

        self.assertFalse(
            should_refresh_cache_entry(
                entry, now=1_100.0, cache_ttl=3_600, failure_backoff=900
            )
        )
        self.assertTrue(
            should_refresh_cache_entry(
                entry, now=1_901.0, cache_ttl=3_600, failure_backoff=900
            )
        )

    def test_successful_entries_use_normal_cache_ttl(self):
        entry = {"updated_at": 1_000.0, "last_attempt_at": 1_000.0, "error": None}

        self.assertFalse(
            should_refresh_cache_entry(
                entry, now=4_599.0, cache_ttl=3_600, failure_backoff=900
            )
        )
        self.assertTrue(
            should_refresh_cache_entry(
                entry, now=4_601.0, cache_ttl=3_600, failure_backoff=900
            )
        )


class ResponseClassificationTests(unittest.TestCase):
    def test_distinguishes_auth_failure_rate_limit_and_success(self):
        self.assertEqual(classify_x_response(401, {}), "auth_expired")
        self.assertEqual(classify_x_response(429, {}), "rate_limited")
        self.assertEqual(classify_x_response(200, {"errors": [{"code": 88}]}), "rate_limited")
        self.assertEqual(classify_x_response(200, {"data": {}}), "ok")


def timeline_payload(tweet_id="456"):
    return {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "type": "TimelineAddEntries",
                                    "entries": [
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "__typename": "TimelineTweet",
                                                    "tweet_results": {
                                                        "result": {
                                                            "__typename": "Tweet",
                                                            "rest_id": tweet_id,
                                                            "legacy": {
                                                                "full_text": "browser post",
                                                                "created_at": "Wed Jul 22 01:02:03 +0000 2026",
                                                            },
                                                        }
                                                    },
                                                }
                                            }
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }
            }
        }
    }


class FakeResponse:
    def __init__(self, status=200, payload=None, operation="UserTweetsAndReplies"):
        self.status = status
        self.url = f"https://x.com/i/api/graphql/query/{operation}?variables=x"
        self._payload = payload if payload is not None else timeline_payload()

    def json(self):
        return self._payload


class FakeExpectation:
    def __init__(self, response):
        self.value = response

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeLocator:
    def all(self):
        return []


class FakePage:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.url = "about:blank"
        self.visited_urls = []
        self.default_timeout = None

    def set_default_timeout(self, value):
        self.default_timeout = value

    def expect_response(self, predicate, timeout):
        self.predicate_matched = predicate(self.response)
        self.expect_timeout = timeout
        if self.error:
            raise self.error
        if not self.predicate_matched:
            raise TimeoutError("No matching timeline response")
        return FakeExpectation(self.response)

    def goto(self, url, **_kwargs):
        self.url = url
        self.visited_urls.append(url)

    def locator(self, _selector):
        return FakeLocator()


class FakeContext:
    def __init__(self, page=None):
        self.pages = [page or FakePage()]
        self.added_cookies = []

    def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page

    def cookies(self, _urls):
        return [
            {"name": "auth_token", "value": "secret", "domain": ".x.com", "path": "/"},
            {"name": "foreign", "value": "no", "domain": ".v2ex.com", "path": "/"},
        ]


class FakeManager:
    def __init__(self, context, **options):
        self.context = context
        self.options = options
        self.exited = False

    def __enter__(self):
        return self.context

    def __exit__(self, *_args):
        self.exited = True


class BrowserFetcherTests(unittest.TestCase):
    def test_reply_screenshot_selector_matches_the_requested_status(self):
        self.assertEqual(
            _tweet_article_selector("123"),
            "article:has(a[href$='/status/123'], a[href*='/status/123/'], a[href*='/status/123?'], a[href*='/i/web/status/123'])",
        )

    def make_fetcher(self, directory, page=None):
        root = Path(directory)
        session = root / "x_session.json"
        session.write_text(
            json.dumps(
                {
                    "cookies": [
                        {"name": "auth_token", "value": "secret", "domain": ".x.com", "path": "/"},
                        {"name": "foreign", "value": "no", "domain": ".v2ex.com", "path": "/"},
                    ]
                }
            )
        )
        fetcher = XBrowserFetcher(
            session_file=session,
            profile_dir=root / "profile",
            headless=True,
            timeout_seconds=20,
        )
        context = FakeContext(page)
        return fetcher, context

    def test_start_persists_fingerprint_and_imports_clean_session_once(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher, context = self.make_fetcher(directory)
            managers = []

            def manager_factory(**options):
                manager = FakeManager(context, **options)
                managers.append(manager)
                return manager

            with patch("camoufox.sync_api.Camoufox", side_effect=manager_factory), patch(
                "camoufox.fingerprints.get_random_preset",
                return_value={"navigator": {"userAgent": "Firefox"}},
            ):
                fetcher.start()

            self.assertEqual([cookie["name"] for cookie in context.added_cookies], ["auth_token"])
            self.assertTrue((fetcher.profile_dir / "fingerprint.json").exists())
            self.assertTrue((fetcher.profile_dir / "session_import.sha256").exists())
            self.assertEqual(context.pages[0].default_timeout, 20_000)
            fetcher.close()
            self.assertTrue(managers[0].exited)

    def test_fetches_timeline_and_validates_username(self):
        with tempfile.TemporaryDirectory() as directory:
            page = FakePage(FakeResponse(payload=timeline_payload("789")))
            fetcher, context = self.make_fetcher(directory, page)
            fetcher._context = context
            fetcher._page = page

            tweets = fetcher.fetch_tweets("@Valid_User")

            self.assertEqual(tweets[0]["id"], "789")
            self.assertEqual(fetcher.last_status, 200)
            self.assertTrue(page.predicate_matched)
            self.assertEqual(
                page.visited_urls,
                ["about:blank", "https://x.com/valid_user/with_replies"],
            )
            with self.assertRaises(ValueError):
                fetcher.fetch_tweets("bad user!")

    def test_does_not_accept_the_posts_only_timeline_response(self):
        with tempfile.TemporaryDirectory() as directory:
            page = FakePage(FakeResponse(
                operation="UserTweets",
                payload=timeline_payload("posts-only"),
            ))
            fetcher, context = self.make_fetcher(directory, page)
            fetcher._context = context
            fetcher._page = page

            with self.assertRaises(XBrowserError):
                fetcher.fetch_tweets("daydayuplift")

            self.assertFalse(page.predicate_matched)

    def test_raises_typed_rate_limit_and_auth_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            rate_page = FakePage(FakeResponse(status=429, payload={}))
            fetcher, context = self.make_fetcher(directory, rate_page)
            fetcher._context = context
            fetcher._page = rate_page
            with self.assertRaises(XRateLimitedError):
                fetcher.fetch_tweets("elonmusk")

            auth_page = FakePage(FakeResponse(status=401, payload={}))
            fetcher._page = auth_page
            with self.assertRaises(XAuthExpiredError):
                fetcher.fetch_tweets("elonmusk")

    def test_saves_only_x_cookies_from_browser_context(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher, context = self.make_fetcher(directory)
            fetcher._context = context
            fetcher._page = context.pages[0]

            count = fetcher.save_session_cookies()
            saved = json.loads(fetcher.session_file.read_text())

            self.assertEqual(count, 1)
            self.assertEqual(saved["cookies"][0]["name"], "auth_token")

    def test_recaptures_images_with_stale_capture_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            image_dir = Path(directory) / "screenshots"
            image_dir.mkdir()
            (image_dir / "123.png").write_bytes(b"stale")
            (image_dir / "123.png.capture-v15").write_text("v15")
            fetcher, _ = self.make_fetcher(directory)

            with patch.object(fetcher, "start", side_effect=RuntimeError("recapture")):
                with self.assertRaisesRegex(RuntimeError, "recapture"):
                    fetcher.capture_tweet_screenshot("123", image_dir)

    def test_retries_screenshot_after_browser_target_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher, _ = self.make_fetcher(directory)
            image_dir = Path(directory) / "screenshots"
            image_path = image_dir / "123.png"
            with patch.object(
                fetcher,
                "_capture_tweet_screenshot_once",
                side_effect=[
                    RuntimeError("Locator.all: Target page, context or browser has been closed"),
                    image_path,
                ],
            ) as capture, patch.object(fetcher, "close") as close, patch.object(
                fetcher, "start"
            ) as start:
                result = fetcher.capture_tweet_screenshot("123", image_dir)

            self.assertEqual(result, image_path)
            self.assertEqual(capture.call_count, 2)
            close.assert_called_once_with()
            start.assert_called_once_with()

    def test_does_not_restart_browser_for_non_target_screenshot_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher, _ = self.make_fetcher(directory)
            image_dir = Path(directory) / "screenshots"
            with patch.object(
                fetcher,
                "_capture_tweet_screenshot_once",
                side_effect=RuntimeError("image layout timeout"),
            ) as capture, patch.object(fetcher, "close") as close, patch.object(
                fetcher, "start"
            ) as start:
                with self.assertRaisesRegex(RuntimeError, "image layout timeout"):
                    fetcher.capture_tweet_screenshot("123", image_dir)

            capture.assert_called_once()
            close.assert_not_called()
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
