"""Browser-native X timeline fetching for the local RSS bridge.

The browser performs X's own GraphQL requests.  The bridge only observes the
timeline response and turns it into RSS data; it does not synthesize private API
headers, rotate identities, or attempt to solve access challenges.
"""

from __future__ import annotations

import json
import hashlib
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ALLOWED_COOKIE_DOMAINS = {"x.com", "twitter.com"}
TIMELINE_OPERATIONS = ("UserTweets", "UserTweetsAndReplies")


class XBrowserError(RuntimeError):
    """Base class for browser fetch failures."""


class XAuthExpiredError(XBrowserError):
    """The persistent X session is no longer authenticated."""


class XRateLimitedError(XBrowserError):
    """X explicitly rate-limited the browser session."""


def _is_allowed_cookie_domain(domain: str) -> bool:
    normalized = domain.strip().lower().lstrip(".")
    return normalized in ALLOWED_COOKIE_DOMAINS


def sanitize_x_cookies(cookies: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deduplicated Playwright cookies for X-owned domains only."""
    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    same_site_map = {
        "unspecified": "None",
        "no_restriction": "None",
        "none": "None",
        "lax": "Lax",
        "strict": "Strict",
    }

    for raw in cookies:
        domain = str(raw.get("domain", ""))
        name = str(raw.get("name", ""))
        value = raw.get("value")
        if not name or value is None or not _is_allowed_cookie_domain(domain):
            continue

        path = str(raw.get("path") or "/")
        cookie: dict[str, Any] = {
            "name": name,
            "value": str(value),
            "domain": domain,
            "path": path,
            "httpOnly": bool(raw.get("httpOnly", False)),
            "secure": bool(raw.get("secure", False)),
        }
        expires = raw.get("expirationDate", raw.get("expires"))
        if isinstance(expires, (int, float)) and expires > 0:
            cookie["expires"] = expires
        same_site = str(raw.get("sameSite", "")).lower()
        if same_site in same_site_map:
            cookie["sameSite"] = same_site_map[same_site]

        # Cookie-Editor exports can contain repeated auth_token/ct0 entries.  The
        # final occurrence is the newest browser value and intentionally wins.
        deduplicated[(name, domain.lower(), path)] = cookie

    return list(deduplicated.values())


def load_x_cookies(session_file: Path) -> list[dict[str, Any]]:
    if not session_file.exists():
        return []
    raw = json.loads(session_file.read_text())
    cookies = raw.get("cookies", raw) if isinstance(raw, dict) else raw
    if not isinstance(cookies, list):
        raise ValueError("X session must contain a cookie list")
    return sanitize_x_cookies(cookies)


def classify_x_response(status: int, payload: dict[str, Any]) -> str:
    if status in (401, 403):
        return "auth_expired"
    if status == 429:
        return "rate_limited"
    for error in payload.get("errors", []) if isinstance(payload, dict) else []:
        if error.get("code") in (32, 89, 239):
            return "auth_expired"
        if error.get("code") in (88, 344):
            return "rate_limited"
    return "ok"


def _unwrap_tweet_result(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet", result)
    if result.get("__typename") == "Tweet":
        return result
    return None


def extract_tweets_from_timeline(api_response: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract raw Tweet objects from current and legacy UserTweets layouts."""
    try:
        instructions = api_response["data"]["user"]["result"]["timeline"]["timeline"][
            "instructions"
        ]
    except (KeyError, TypeError):
        return []

    tweets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for instruction in instructions:
        entries = instruction.get("entries", [])
        if instruction.get("type") == "TimelinePinEntry" and instruction.get("entry"):
            entries = [instruction["entry"]]
        for entry in entries:
            content = entry.get("content", {})
            item_content = content.get("itemContent", {})
            result = item_content.get("tweet_results", {}).get("result")
            if result is None:
                result = content.get("tweet_results", {}).get("result")
            tweet = _unwrap_tweet_result(result)
            if not tweet:
                continue
            tweet_id = str(tweet.get("rest_id", ""))
            if tweet_id and tweet_id not in seen_ids:
                seen_ids.add(tweet_id)
                tweets.append(tweet)
    return tweets


def parse_tweet_to_dict(tweet_result: dict[str, Any]) -> dict[str, Any]:
    """Convert an X GraphQL Tweet object to the bridge's stable RSS shape."""
    tweet_result = _unwrap_tweet_result(tweet_result) or tweet_result
    legacy = tweet_result.get("legacy", {}) or {}
    tweet_id = str(tweet_result.get("rest_id") or legacy.get("id_str") or "")
    note_result = (
        tweet_result.get("note_tweet", {})
        .get("note_tweet_results", {})
        .get("result", {})
    )
    text = note_result.get("text") or legacy.get("full_text") or legacy.get("text") or ""

    try:
        date = datetime.strptime(legacy.get("created_at", ""), "%a %b %d %H:%M:%S %z %Y")
    except (TypeError, ValueError):
        date = datetime.now(timezone.utc)

    user_result = (
        tweet_result.get("core", {}).get("user_results", {}).get("result", {}) or {}
    )
    user_core = user_result.get("core", {}) or {}
    user_legacy = user_result.get("legacy", {}) or {}
    media: list[dict[str, Any]] = []
    media_items = legacy.get("extended_entities", {}).get("media", [])
    if not media_items:
        media_items = legacy.get("entities", {}).get("media", [])
    for item in media_items:
        media_type = item.get("type", "")
        media_url = item.get("media_url_https") or item.get("media_url") or ""
        if media_type == "photo" and media_url:
            media.append(
                {
                    "type": "photo",
                    "url": media_url,
                    "width": item.get("original_info", {}).get("width", 0),
                    "height": item.get("original_info", {}).get("height", 0),
                }
            )
        elif media_type in ("video", "animated_gif") and media_url:
            variants = item.get("video_info", {}).get("variants", [])
            mp4_variants = [v for v in variants if v.get("content_type") == "video/mp4"]
            best = max(mp4_variants, key=lambda v: v.get("bitrate", 0), default={})
            media.append(
                {
                    "type": "video",
                    "url": best.get("url", media_url),
                    "poster": media_url,
                    "duration": item.get("video_info", {}).get("duration_millis", 0),
                }
            )

    def nested_tweet(key: str) -> dict[str, Any] | None:
        nested = legacy.get(key, {}).get("result")
        unwrapped = _unwrap_tweet_result(nested)
        return parse_tweet_to_dict(unwrapped) if unwrapped else None

    links = []
    for entity in legacy.get("entities", {}).get("urls", []):
        expanded = entity.get("expanded_url")
        if expanded:
            links.append({"url": expanded, "display": entity.get("display_url", "")})

    retweeted = nested_tweet("retweeted_status_result")
    quoted = nested_tweet("quoted_status_result")
    return {
        "id": tweet_id,
        "url": f"https://x.com/i/web/status/{tweet_id}",
        "text": text,
        "date": date,
        "date_rss": date.strftime("%a, %d %b %Y %H:%M:%S +0000"),
        "user": {
            "screen_name": user_core.get("screen_name")
            or user_legacy.get("screen_name")
            or user_result.get("screen_name", ""),
            "name": user_core.get("name")
            or user_legacy.get("name")
            or user_result.get("name", ""),
            "profile_image": user_legacy.get("profile_image_url_https", ""),
        },
        "media": media,
        "stats": {
            "reply_count": legacy.get("reply_count", 0),
            "retweet_count": legacy.get("retweet_count", 0),
            "favorite_count": legacy.get("favorite_count", 0),
            "view_count": legacy.get("view_count", 0),
        },
        "links": links,
        "is_retweet": retweeted is not None,
        "retweeted_tweet": retweeted,
        "is_quote": quoted is not None,
        "quoted_tweet": quoted,
        "lang": legacy.get("lang", "en"),
        "possibly_sensitive": legacy.get("possibly_sensitive", False),
    }


def should_refresh_cache_entry(
    entry: dict[str, Any] | None,
    *,
    now: float,
    cache_ttl: int,
    failure_backoff: int,
) -> bool:
    if not entry:
        return True
    if entry.get("error"):
        return now - float(entry.get("last_attempt_at", 0)) >= failure_backoff
    return now - float(entry.get("updated_at", 0)) >= cache_ttl


class XBrowserFetcher:
    """Single-threaded Camoufox session used by the bridge worker."""

    SCREENSHOT_FONT_CSS = (
        '"PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif'
    )

    def __init__(
        self,
        *,
        session_file: Path,
        profile_dir: Path,
        headless: bool = True,
        timeout_seconds: int = 45,
    ):
        self.session_file = session_file
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout_seconds = timeout_seconds
        self._manager: Any = None
        self._context: Any = None
        self._page: Any = None
        self.last_error: str | None = None
        self.last_status: int | None = None
        self.last_success_at: float | None = None

    def _load_or_create_fingerprint(self) -> dict[str, Any]:
        fingerprint_file = self.profile_dir / "fingerprint.json"
        if fingerprint_file.exists():
            return json.loads(fingerprint_file.read_text())
        from camoufox.fingerprints import get_random_preset

        preset = get_random_preset(os="macos")
        if not preset:
            raise XBrowserError("Camoufox did not provide a macOS fingerprint preset")
        fingerprint_file.write_text(json.dumps(preset, indent=2))
        return preset

    def _import_session_if_changed(self) -> None:
        if not self.session_file.exists():
            return
        digest = hashlib.sha256(self.session_file.read_bytes()).hexdigest()
        marker = self.profile_dir / "session_import.sha256"
        if marker.exists() and marker.read_text().strip() == digest:
            return
        cookies = load_x_cookies(self.session_file)
        if cookies:
            self._context.add_cookies(cookies)
            marker.write_text(digest)

    def start(self) -> None:
        if self._context is not None:
            return
        try:
            from camoufox.sync_api import Camoufox
        except ImportError as exc:
            raise XBrowserError(
                "Camoufox is not installed; run the project browser setup command"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = self._load_or_create_fingerprint()
        self._manager = Camoufox(
            persistent_context=True,
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            os="macos",
            fingerprint_preset=fingerprint,
            humanize=True,
            locale="zh-CN",
            enable_cache=True,
        )
        try:
            self._context = self._manager.__enter__()
            self._import_session_if_changed()
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.set_default_timeout(self.timeout_seconds * 1_000)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _is_timeline_response(response: Any) -> bool:
        return any(f"/{operation}" in response.url for operation in TIMELINE_OPERATIONS)

    def _fetch_tweets_from_dom(self) -> list[dict[str, Any]]:
        """Fallback for X pages whose current GraphQL operation is renamed."""
        tweets = []
        seen_ids = set()
        for article in self._page.locator("article").all():
            for button in article.locator("button").all():
                try:
                    label = button.inner_text().strip().lower()
                    if label in {"显示更多", "show more"}:
                        button.click(timeout=1_000)
                except Exception:
                    continue
            href = next(
                (
                    link.get_attribute("href")
                    for link in article.locator("a[href*='/status/']").all()
                    if link.get_attribute("href")
                ),
                None,
            )
            match = re.search(r"/status/(\d+)", href or "")
            if not match or match.group(1) in seen_ids:
                continue
            tweet_id = match.group(1)
            media = []
            for image in article.locator("img").all():
                image_url = image.get_attribute("src") or ""
                if "pbs.twimg.com/media/" in image_url:
                    media.append({"type": "photo", "url": image_url})
            tweets.append(
                {
                    "id": tweet_id,
                    "url": f"https://x.com/i/web/status/{tweet_id}",
                    "text": article.inner_text().strip(),
                    "date": datetime.now(timezone.utc),
                    "date_rss": datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
                    "media": media,
                }
            )
            seen_ids.add(tweet_id)
        return tweets

    def fetch_tweets(self, username: str) -> list[dict[str, Any]]:
        self.start()
        username = username.strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-z0-9_]{1,15}", username):
            raise ValueError(f"Invalid X username: {username!r}")

        try:
            self._page.goto("about:blank")
            with self._page.expect_response(
                self._is_timeline_response, timeout=self.timeout_seconds * 1_000
            ) as response_info:
                self._page.goto(
                    f"https://x.com/{username}",
                    wait_until="domcontentloaded",
                    timeout=self.timeout_seconds * 1_000,
                )
            response = response_info.value
            self.last_status = response.status
            payload = response.json()
            classification = classify_x_response(response.status, payload)
            if classification == "auth_expired":
                raise XAuthExpiredError("X browser session is no longer authenticated")
            if classification == "rate_limited":
                raise XRateLimitedError("X rate-limited the browser timeline request")

            raw_tweets = extract_tweets_from_timeline(payload)
            tweets = [parse_tweet_to_dict(tweet) for tweet in raw_tweets]
            tweets = [tweet for tweet in tweets if tweet["id"] and tweet["text"]]
            if not tweets and payload.get("errors"):
                raise XBrowserError(f"X timeline error: {payload['errors'][0].get('message', 'unknown')}")
            self.last_error = None
            self.last_success_at = time.time()
            return tweets
        except (XAuthExpiredError, XRateLimitedError):
            raise
        except Exception as exc:
            current_url = str(getattr(self._page, "url", ""))
            if "/login" in current_url or "/i/flow/login" in current_url:
                raise XAuthExpiredError("X redirected the persistent session to login") from exc
            fallback_tweets = self._fetch_tweets_from_dom()
            if fallback_tweets:
                self.last_error = None
                self.last_success_at = time.time()
                return fallback_tweets
            self.last_error = str(exc)
            raise XBrowserError(f"Failed to fetch @{username}: {exc}") from exc

    def open_page(self, url: str = "https://x.com/home") -> Any:
        """Open a page in the persistent session for an explicit manual login."""
        self.start()
        self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1_000)
        return self._page

    def capture_tweet_screenshot(self, tweet_id: str, image_dir: Path) -> Path:
        """Save one fully rendered tweet image; existing captures are reused."""
        if not re.fullmatch(r"\d+", tweet_id):
            raise ValueError(f"Invalid tweet id: {tweet_id!r}")
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / f"{tweet_id}.png"
        if image_path.exists():
            return image_path
        self.start()
        page = self._context.new_page()
        try:
            page.set_default_timeout(self.timeout_seconds * 1_000)
            page.goto(
                f"https://x.com/i/web/status/{tweet_id}",
                wait_until="domcontentloaded",
                timeout=self.timeout_seconds * 1_000,
            )
            article = page.locator("article").first
            article.wait_for(state="visible", timeout=self.timeout_seconds * 1_000)
            page.add_style_tag(content=f"article, article * {{ font-family: {self.SCREENSHOT_FONT_CSS} !important; }}")

            # X streams quoted tweets and media after the outer article is visible.
            # Expand collapsed text before waiting for the final layout.
            for button in article.locator("button").all():
                try:
                    if button.inner_text().strip().lower() in {"显示更多", "show more"}:
                        button.click(timeout=1_000)
                except Exception:
                    continue
            try:
                page.wait_for_function(
                    """article => [...article.querySelectorAll('img')]
                    .filter(img => img.src.includes('pbs.twimg.com'))
                    .every(img => img.complete && img.naturalWidth > 0)""",
                    article,
                    timeout=8_000,
                )
            except Exception:
                # Some tweets have no media, and X can keep background requests open.
                pass
            page.wait_for_timeout(2_000)
            article.screenshot(path=str(image_path))
            return image_path
        finally:
            page.close()

    def save_session_cookies(self) -> int:
        """Save only X-owned browser cookies to the bridge session file."""
        self.start()
        cookies = sanitize_x_cookies(self._context.cookies(["https://x.com"]))
        self.session_file.write_text(json.dumps({"cookies": cookies, "origins": []}, indent=2))
        return len(cookies)

    def close(self) -> None:
        manager, self._manager = self._manager, None
        self._page = None
        self._context = None
        if manager is not None:
            manager.__exit__(None, None, None)
