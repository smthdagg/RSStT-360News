"""Browser-native X timeline fetching for the local RSS bridge.

The browser performs X's own GraphQL requests.  The bridge only observes the
timeline response and turns it into RSS data; it does not synthesize private API
headers, rotate identities, or attempt to solve access challenges.
"""

from __future__ import annotations

import json
import hashlib
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ALLOWED_COOKIE_DOMAINS = {"x.com", "twitter.com"}
CAPTURE_VERSION = "v22"

_COOKIE_CONSENT_RE = re.compile(
    r"(?:cookie|cookies|隐私|个人数据|个性化|privacy|personal data)", re.IGNORECASE
)
_COOKIE_REJECT_RE = re.compile(
    r"(?:拒绝|拒收|不要|decline|reject|necessary only|仅必要)", re.IGNORECASE
)
_COOKIE_CLOSE_RE = re.compile(r"(?:关闭|close|not now|稍后)", re.IGNORECASE)
_COOKIE_ACCEPT_RE = re.compile(r"(?:接受|同意|允许|accept|agree|allow)", re.IGNORECASE)


def _cookie_action_priority(label: str) -> int:
    """Rank consent actions, preferring the least permissive choice."""
    normalized = re.sub(r"\s+", " ", str(label or "")).strip()
    if not normalized:
        return 0
    if _COOKIE_REJECT_RE.search(normalized):
        return 3
    if _COOKIE_CLOSE_RE.search(normalized):
        return 2
    if _COOKIE_ACCEPT_RE.search(normalized):
        return 1
    return 0


def _dismiss_cookie_consent(page: Any) -> bool:
    """Dismiss X's consent banner before it can cover a tweet screenshot."""
    try:
        return bool(page.evaluate(
            """() => {
                const consent = /(?:cookie|cookies|隐私|个人数据|个性化|privacy|personal data)/i;
                const reject = /(?:拒绝|拒收|不要|decline|reject|necessary only|仅必要)/i;
                const close = /(?:关闭|close|not now|稍后)/i;
                const accept = /(?:接受|同意|允许|accept|agree|allow)/i;
                const containers = [...document.querySelectorAll(
                    '[role="dialog"], [aria-modal="true"], [data-testid*="cookie" i], div, section, aside'
                )].filter((el) => {
                    if (!consent.test(el.innerText || '')) return false;
                    if (el.matches('[role="dialog"], [aria-modal="true"], [data-testid*="cookie" i]')) return true;
                    const style = getComputedStyle(el);
                    return ['fixed', 'sticky', 'absolute'].includes(style.position)
                        && el.querySelector('button,[role="button"]');
                })
                  .sort((a, b) => (a.innerText || '').length - (b.innerText || '').length);
                let changed = false;
                for (const container of containers.slice(0, 6)) {
                    const buttons = [...container.querySelectorAll('button,[role="button"]')];
                    const score = (button) => {
                        const label = (button.innerText || button.getAttribute('aria-label') || '').trim();
                        if (reject.test(label)) return 3;
                        if (close.test(label)) return 2;
                        if (accept.test(label)) return 1;
                        return 0;
                    };
                    buttons.sort((a, b) => score(b) - score(a));
                    const target = buttons.find((button) => score(button) > 0);
                    if (target) {
                        target.click();
                        changed = true;
                    }
                    container.style.setProperty('display', 'none', 'important');
                    changed = true;
                }
                return changed;
            }"""
        ))
    except Exception:
        pass

    # Keep a locator-based fallback for browser builds where evaluate() is blocked.
    try:
        best = None
        for dialog in page.locator('[role="dialog"], [aria-modal="true"]').all():
            if not _COOKIE_CONSENT_RE.search(dialog.inner_text() or ''):
                continue
            for button in dialog.locator('button, [role="button"]').all():
                label = button.get_attribute('aria-label') or button.inner_text()
                priority = _cookie_action_priority(label)
                if priority and (best is None or priority > best[0]):
                    best = priority, button
        if best:
            best[1].click(timeout=2_000)
            return True
    except Exception:
        pass
    return False


class XBrowserError(RuntimeError):
    """Base class for browser fetch failures."""


class XAuthExpiredError(XBrowserError):
    """The persistent X session is no longer authenticated."""


class XRateLimitedError(XBrowserError):
    """X explicitly rate-limited the browser session."""


TARGET_DEAD_MARKERS = (
    "target crashed",
    "target page, context or browser has been closed",
    "target closed",
)


def is_target_dead_error(exc: BaseException | None) -> bool:
    """True when the failure means the page/browser target is gone.

    A crashed content process (for example after the kernel OOM-killed a
    bloated tab) is not the same as a closed page: retrying into the dead
    target only produces the same error, so the caller must recycle the
    whole browser instead.
    """
    if exc is None:
        return False
    text = str(exc).lower()
    return any(marker in text for marker in TARGET_DEAD_MARKERS)


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


def _tweet_article_selector(tweet_id: str) -> str:
    return (
        f"article:has(a[href$='/status/{tweet_id}'], "
        f"a[href*='/status/{tweet_id}/'], a[href*='/status/{tweet_id}?'], "
        f"a[href*='/i/web/status/{tweet_id}'])"
    )


def _conversation_clip(
    target_rect: dict[str, float] | None,
    ancestors: list[dict[str, float]],
    *,
    max_ancestors: int = 1,
) -> dict[str, float] | None:
    """Clip rect covering a reply and its direct parent tweet (CSS px).

    A reply screenshot that shows only the reply hides who it answers, so the
    capture widens to exactly one parent tweet above it in the same
    conversation column. Returns None when there is nothing above the target.
    """
    if not target_rect or not ancestors:
        return None
    scope = ancestors[-max_ancestors:] + [target_rect]
    top = min(r["y"] for r in scope)
    bottom = target_rect["y"] + target_rect["height"]
    left = min(r["x"] for r in scope)
    right = max(r["x"] + r["width"] for r in scope)
    clip = {"x": left, "y": top, "width": right - left, "height": bottom - top}
    if clip["width"] <= 0 or clip["height"] <= 0:
        return None
    return clip


# Collected in document coordinates: X scrolls the focused reply to the
# viewport top on load, so ancestors sit at negative viewport y positions.
_CONVERSATION_SCOPE_JS = """() => {
    const scrollY = window.scrollY;
    const target = document.querySelector("article[data-x-capture-target='true']");
    if (!target) return null;
    const tr = target.getBoundingClientRect();
    const root = target.closest('section')
        || document.querySelector("[data-testid='primaryColumn']")
        || document;
    const ancestors = [];
    for (const node of root.querySelectorAll('article')) {
        if (node.contains(target) || target.contains(node)) continue;
        const r = node.getBoundingClientRect();
        if (r.height <= 0 || r.bottom + scrollY > tr.top + scrollY + 1) continue;
        if (r.right < tr.left || r.left > tr.right) continue;
        ancestors.push({x: r.left, y: r.top + scrollY, width: r.width, height: r.height});
    }
    return {
        target: {x: tr.left, y: tr.top + scrollY, width: tr.width, height: tr.height},
        ancestors,
    };
}"""


def _tweet_results_from_content(content: dict[str, Any]) -> list[Any]:
    results = []
    for item_content in (content.get("itemContent") or {}, content):
        if not isinstance(item_content, dict):
            continue
        tweet_results = item_content.get("tweet_results") or {}
        if isinstance(tweet_results, dict) and tweet_results.get("result") is not None:
            results.append(tweet_results["result"])
    module_items = content.get("items") or []
    if isinstance(module_items, list):
        for module_item in module_items:
            if not isinstance(module_item, dict):
                continue
            item = module_item.get("item") or {}
            if not isinstance(item, dict):
                continue
            item_content = item.get("itemContent") or {}
            if not isinstance(item_content, dict):
                continue
            tweet_results = item_content.get("tweet_results") or {}
            if isinstance(tweet_results, dict) and tweet_results.get("result") is not None:
                results.append(tweet_results["result"])
    return results


def extract_tweets_from_timeline(api_response: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract tweets from UserTweetsAndReplies entries and reply modules."""
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
        # Pinned tweets can be years old; they are not new timeline posts.
        if instruction.get("type") == "TimelinePinEntry":
            continue
        for entry in entries:
            content = entry.get("content", {})
            if not isinstance(content, dict):
                continue
            for result in _tweet_results_from_content(content):
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
        nested = tweet_result.get(key) or legacy.get(key) or {}
        if not isinstance(nested, dict):
            return None
        nested = nested.get("result", nested)
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
        "in_reply_to_status_id": legacy.get("in_reply_to_status_id_str"),
        "links": links,
        "is_retweet": retweeted is not None,
        "retweeted_tweet": retweeted,
        "is_quote": quoted is not None,
        "quoted_tweet": quoted,
        "lang": legacy.get("lang", "en"),
        "possibly_sensitive": legacy.get("possibly_sensitive", False),
    }


def _count_nested_media(tweet: dict[str, Any] | None) -> int:
    """Count media in the tweet and its visible quote/retweet cards."""
    if not tweet:
        return 0
    return (
        len(tweet.get("media") or [])
        + _count_nested_media(tweet.get("quoted_tweet"))
        + _count_nested_media(tweet.get("retweeted_tweet"))
    )


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

    # A browser that runs for a long time accumulates memory in its content
    # process until the kernel OOM-killer destroys it (observed at 2.6 GB).
    # Recycle the whole browser well before that can happen again.
    RECYCLE_AFTER_SECONDS = 6 * 3600
    # After this many consecutive failed fetches, assume the browser itself
    # is degraded and relaunch it before the next attempt.
    FAILURE_RECYCLE_THRESHOLD = 2

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
        self._started_monotonic: float | None = None
        self._consecutive_failures = 0
        self.last_error: str | None = None
        self.last_status: int | None = None
        self.last_success_at: float | None = None

    def _browser_needs_recycle(self, now: float | None = None) -> bool:
        if self._context is None:
            return False
        moment = time.monotonic() if now is None else now
        return self._started_monotonic is None or (
            moment - self._started_monotonic >= self.RECYCLE_AFTER_SECONDS
        )

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
        self.profile_dir.mkdir(parents=True, exist_ok=True)
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
            if self._browser_needs_recycle():
                self.close()
            else:
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
            device_scale_factor=2,
            # Bound the in-browser caches. Firefox's defaults let media
            # buffers and the memory cache grow almost unbounded, which was
            # a main contributor to the 2.6 GB content process that the
            # kernel OOM killer destroyed on 2026-09-26.
            firefox_user_prefs={
                "browser.cache.memory.capacity": 262144,
                "browser.cache.memory.max_entry_size": 8192,
                "media.memory_caches_combined_limit_kb": 262144,
                "media.cache_readahead_limit": 60,
                "media.cache_resume_threshold": 60,
            },
        )
        try:
            self._context = self._manager.__enter__()
            self._import_session_if_changed()
            self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
            self._page.set_default_timeout(self.timeout_seconds * 1_000)
        except BaseException:
            self.close()
            raise
        self._started_monotonic = time.monotonic()

    @staticmethod
    def _timeline_operation(url: str) -> str | None:
        match = re.search(r"/i/api/graphql/[^/?]+/([A-Za-z0-9_]+)", url)
        return match.group(1) if match else None

    def _is_user_posts_response(self, response: Any) -> bool:
        # X split the old UserTweetsAndReplies operation: the profile posts
        # tab now answers via UserOriginalsTimeline (earlier builds used
        # UserTweets) and is the only near-realtime source for fresh tweets
        # and quotes.
        return self._timeline_operation(response.url) in {
            "UserOriginalsTimeline",
            "UserTweets",
        }

    def _is_user_replies_response(self, response: Any) -> bool:
        # The replies tab answers via UserRepliesTimeline since the split
        # (older deployments served UserTweetsAndReplies); both are accepted.
        return self._timeline_operation(response.url) in {
            "UserRepliesTimeline",
            "UserTweetsAndReplies",
        }

    def _page_is_closed(self) -> bool:
        page = self._page
        if page is None:
            return True
        try:
            return bool(page.is_closed())
        except Exception:
            return True

    def _park_page(self) -> None:
        """Move the persistent tab off x.com once a fetch attempt is done.

        The timeline tab used to stay on x.com for the whole refresh cycle,
        keeping the X single-page app alive (timers, background polling,
        growing JS heap) between fetches; a tab parked like that for days was
        the surface whose content process grew to 2.6 GB and drew the kernel
        OOM kill. Parking on about:blank releases it after every attempt.
        """
        page = self._page
        if page is None:
            return
        try:
            if page.is_closed():
                return
            if str(getattr(page, "url", "")) == "about:blank":
                return
            page.goto("about:blank", wait_until="commit",
                      timeout=min(self.timeout_seconds, 15) * 1_000)
        except Exception:
            # Parking is opportunistic; never turn it into a fetch failure.
            pass

    def _fetch_tweets_from_dom(self, username: str) -> list[dict[str, Any]]:
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
            author = ""
            author_link = article.locator("a[href^='/']").first
            try:
                href = author_link.get_attribute("href") or ""
                if re.fullmatch(r"/[A-Za-z0-9_]{1,15}", href):
                    author = href[1:].lower()
            except Exception:
                pass
            if author and author != username:
                continue
            time_node = article.locator("time").first
            timestamp = time_node.get_attribute("datetime") if time_node else None
            try:
                date = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if timestamp else None
            except (TypeError, ValueError):
                date = None
            if date is None:
                # Without X's absolute timestamp, do not fabricate a current date.
                continue
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
                    "date": date,
                    "date_rss": date.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
                    "user": {"screen_name": author or username},
                    "media": media,
                }
            )
            seen_ids.add(tweet_id)
        return tweets

    def fetch_tweets(
        self, username: str, *, _browser_retry: bool = False
    ) -> list[dict[str, Any]]:
        if (
            self._context is not None
            and not _browser_retry
            and self._consecutive_failures >= self.FAILURE_RECYCLE_THRESHOLD
        ):
            # Repeated failures usually mean the browser itself is degraded;
            # relaunching it beats retrying into the same sick target.
            self.close()
        self.start()
        self.reload_session()
        username = username.strip().lstrip("@").lower()
        if not re.fullmatch(r"[a-z0-9_]{1,15}", username):
            raise ValueError(f"Invalid X username: {username!r}")

        try:
            return self._fetch_tweets_attempt(username, _browser_retry=_browser_retry)
        finally:
            # Whatever the outcome, do not leave the tab sitting on x.com.
            self._park_page()

    def _fetch_tweets_attempt(
        self, username: str, *, _browser_retry: bool
    ) -> list[dict[str, Any]]:
        try:
            self._page.goto("about:blank")
            # X split the old combined timeline: the profile posts tab
            # (UserTweets) is near-realtime for tweets and quotes, while the
            # replies tab (UserRepliesTimeline) carries reply conversations.
            # Fetch both and merge so neither source's lag hides content.
            payloads: list[dict[str, Any]] = []
            statuses: list[int] = []
            fetch_errors: list[str] = []
            for tab, url, predicate in (
                ("posts", f"https://x.com/{username}?_p={random.randrange(10 ** 8)}",
                 self._is_user_posts_response),
                ("replies", f"https://x.com/{username}/with_replies?_p={random.randrange(10 ** 8)}",
                 self._is_user_replies_response),
            ):
                try:
                    with self._page.expect_response(
                        predicate, timeout=self.timeout_seconds * 1_000
                    ) as response_info:
                        # A per-request query token keeps X's frontend from
                        # serving a stale in-memory timeline when the
                        # persistent tab revisits the same profile page.
                        self._page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=self.timeout_seconds * 1_000,
                        )
                    response = response_info.value
                    statuses.append(response.status)
                    payloads.append(response.json())
                except Exception as exc:
                    fetch_errors.append(f"{tab}: {exc}")
            if not payloads:
                raise XBrowserError(
                    f"Failed to fetch @{username} timelines: {'; '.join(fetch_errors)}"
                )
            profile_name = ""
            try:
                for selector in (
                    "[data-testid='UserName']",
                    "article [data-testid='User-Name']",
                    f"a[href='/{username}']",
                ):
                    try:
                        profile_text = self._page.locator(selector).first.inner_text(timeout=2_000)
                    except Exception:
                        continue
                    profile_name = next(
                        (line.strip() for line in profile_text.splitlines()
                         if line.strip() and not line.strip().startswith("@")),
                        "",
                    )
                    if profile_name:
                        break
            except Exception:
                pass
            if not profile_name:
                try:
                    title = self._page.title()
                    profile_name = re.split(r"\s+\(@|\s+/\s+X$", title, maxsplit=1)[0].strip()
                except Exception:
                    pass
            self.last_status = statuses[-1] if statuses else None
            for status, payload in zip(statuses, payloads):
                profile_result = (((payload.get("data") or {}).get("user") or {}).get("result") or {})
                profile_legacy = profile_result.get("legacy") or {}
                profile_core = profile_result.get("core") or {}
                profile_name = (
                    profile_name
                    or profile_core.get("name")
                    or profile_legacy.get("name")
                    or profile_result.get("name")
                    or ""
                )
            for status, payload in zip(statuses, payloads):
                classification = classify_x_response(status, payload)
                if classification == "auth_expired":
                    raise XAuthExpiredError("X browser session is no longer authenticated")
                if classification == "rate_limited":
                    raise XRateLimitedError("X rate-limited the browser timeline request")

            tweets: list[dict[str, Any]] = []
            seen_tweet_ids: set[str] = set()
            for payload in payloads:
                for raw_tweet in extract_tweets_from_timeline(payload):
                    tweet = parse_tweet_to_dict(raw_tweet)
                    if not tweet.get("id") or tweet["id"] in seen_tweet_ids:
                        continue
                    seen_tweet_ids.add(tweet["id"])
                    tweets.append(tweet)
            tweets = [tweet for tweet in tweets if tweet["id"] and tweet["text"]]
            tweets = [
                tweet for tweet in tweets
                if not tweet.get("user", {}).get("screen_name")
                or tweet.get("user", {}).get("screen_name", "").lower() == username
            ]
            for tweet in tweets:
                if profile_name and not tweet.get("user", {}).get("name"):
                    tweet.setdefault("user", {})["name"] = profile_name
            tweets.sort(key=lambda tweet: tweet.get("date") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
            if not tweets:
                for payload in payloads:
                    if payload.get("errors"):
                        raise XBrowserError(f"X timeline error: {payload['errors'][0].get('message', 'unknown')}")
            self.last_error = None
            self.last_success_at = time.time()
            self._consecutive_failures = 0
            return tweets
        except (XAuthExpiredError, XRateLimitedError):
            self._consecutive_failures += 1
            raise
        except Exception as exc:
            if self._page_is_closed() or is_target_dead_error(exc):
                if not _browser_retry:
                    self.close()
                    self.start()
                    return self.fetch_tweets(username, _browser_retry=True)
                self.last_error = str(exc)
                self._consecutive_failures += 1
                raise XBrowserError(
                    f"X browser page closed while fetching @{username}: {exc}"
                ) from exc
            current_url = str(getattr(self._page, "url", ""))
            if "/login" in current_url or "/i/flow/login" in current_url:
                self._consecutive_failures += 1
                raise XAuthExpiredError("X redirected the persistent session to login") from exc
            try:
                fallback_tweets = self._fetch_tweets_from_dom(username)
            except Exception as fallback_exc:
                # The DOM fallback runs on the very page that just failed; when
                # the target crashed underneath it, the raw Playwright error
                # used to escape unwrapped and the dead browser stayed in place
                # for every later cycle. Recycle the browser and retry once.
                self.last_error = str(fallback_exc)
                if not _browser_retry and (
                    self._page_is_closed() or is_target_dead_error(fallback_exc)
                ):
                    self.close()
                    self.start()
                    return self.fetch_tweets(username, _browser_retry=True)
                self._consecutive_failures += 1
                raise XBrowserError(
                    f"Failed to fetch @{username}: {fallback_exc}"
                ) from fallback_exc
            if fallback_tweets:
                self.last_error = None
                self.last_success_at = time.time()
                self._consecutive_failures = 0
                return fallback_tweets
            self.last_error = str(exc)
            self._consecutive_failures += 1
            raise XBrowserError(f"Failed to fetch @{username}: {exc}") from exc

    def open_page(self, url: str = "https://x.com/home") -> Any:
        """Open a page in the persistent session for an explicit manual login."""
        self.start()
        self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_seconds * 1_000)
        return self._page

    def reload_session(self) -> None:
        """Apply a newly imported session file without restarting Camoufox."""
        if self._context is not None:
            self._import_session_if_changed()

    def capture_tweet_screenshot(
        self,
        tweet_id: str,
        image_dir: Path,
        *,
        expected_media_count: int = 0,
        is_reply: bool = False,
    ) -> Path:
        """Retry once after a crashed or closed browser target."""
        for attempt in range(2):
            try:
                return self._capture_tweet_screenshot_once(
                    tweet_id,
                    image_dir,
                    expected_media_count=expected_media_count,
                    is_reply=is_reply,
                )
            except Exception as exc:
                if attempt or not is_target_dead_error(exc):
                    raise
                self.close()
                self.start()
        raise XBrowserError(f"Screenshot retry exhausted for tweet {tweet_id}")

    def _capture_tweet_screenshot_once(
        self,
        tweet_id: str,
        image_dir: Path,
        *,
        expected_media_count: int = 0,
        is_reply: bool = False,
    ) -> Path:
        """Save one fully rendered tweet image; existing captures are reused."""
        if not re.fullmatch(r"\d+", tweet_id):
            raise ValueError(f"Invalid tweet id: {tweet_id!r}")
        expected_media_count = max(0, int(expected_media_count))
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / f"{tweet_id}.png"
        version_marker = image_dir / f"{tweet_id}.png.capture-{CAPTURE_VERSION}"
        # v21 post captures already target the correct article; only replies
        # need a one-time recapture to include the conversation above them.
        previous_marker = image_dir / f"{tweet_id}.png.capture-v21"
        if image_path.exists() and (version_marker.exists() or (not is_reply and previous_marker.exists())):
            return image_path
        self.start()
        self.reload_session()
        page = self._context.new_page()
        try:
            page.set_default_timeout(self.timeout_seconds * 1_000)
            try:
                page.emulate_media(color_scheme="dark")
            except Exception:
                pass
            page.goto(
                f"https://x.com/i/web/status/{tweet_id}",
                wait_until="domcontentloaded",
                timeout=self.timeout_seconds * 1_000,
            )
            page.wait_for_timeout(750)
            _dismiss_cookie_consent(page)
            article = page.locator(_tweet_article_selector(tweet_id)).first
            article.wait_for(state="visible", timeout=self.timeout_seconds * 1_000)
            article.evaluate("element => element.setAttribute('data-x-capture-target', 'true')")
            _dismiss_cookie_consent(page)
            # A reply must show what it answers: the capture widens to the
            # tweets above the target in the same conversation column. Media
            # loading keeps moving the layout, so the clip itself is measured
            # again after the media settles; here only the scope top is needed
            # so the scroll pass also covers the ancestors' lazy media.
            reply_scope_top = None
            if is_reply:
                layout = page.evaluate(_CONVERSATION_SCOPE_JS)
                if layout:
                    clip = _conversation_clip(
                        layout.get("target"), layout.get("ancestors") or []
                    )
                    reply_scope_top = clip["y"] if clip else layout["target"]["y"]
            # Keep the complete outer tweet, including quote/reply cards. Remove
            # fixed page chrome and height clamps that otherwise crop long posts.
            page.add_style_tag(content="""
                html, body { background: #000 !important; }
                [data-testid='TopNavBar'], [data-testid='AppTabBar'],
                div[role='banner'], button[aria-label*='返回'],
                button[aria-label*='Back'] { display: none !important; }
                article { background: #000 !important; color: #e7e9ea !important; padding-left: 16px !important; padding-right: 16px !important; }
                article, article * { font-family: __SCREENSHOT_FONT__, "Apple Color Emoji", "Segoe UI Emoji", sans-serif !important; }
                article [data-testid='User-Name'],
                article [data-testid='User-Name'] * { color: #e7e9ea !important; }
                article [data-testid='User-Name'] a[href^='/'] { color: #71767b !important; }
                article [data-testid='User-Name'] svg { color: #1d9bf0 !important; fill: #1d9bf0 !important; }
                article, article * { color: #e7e9ea !important; }
                article div[dir='auto'], article span[dir='auto'] { color: #e7e9ea !important; }
                article > div div, article > div span { color: #e7e9ea !important; opacity: 1 !important; }
                article [data-testid='User-Name'] a[href^='/'] { color: #71767b !important; }
                article [data-testid='User-Name'] svg { color: #1d9bf0 !important; fill: #1d9bf0 !important; }
                article time { color: #71767b !important; }
                article, article * { max-height: none !important; }
                article { overflow: visible !important; }
            """.replace("__SCREENSHOT_FONT__", self.SCREENSHOT_FONT_CSS))
            page.evaluate("""() => {
                document.querySelectorAll('button[aria-label*="返回"], button[aria-label*="Back"]').forEach((el) => el.style.display = 'none');
                document.querySelectorAll('[role="heading"]').forEach((el) => {
                    if (/^(帖子|post)$/i.test((el.innerText || '').trim())) el.style.display = 'none';
                });
                const article = document.querySelector("article[data-x-capture-target='true']");
                document.querySelectorAll('*').forEach((el) => {
                    const style = getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    if ((style.position === 'fixed' || style.position === 'sticky') && !el.contains(article) &&
                        rect.top <= 2 && rect.height > 0 && rect.height < 120 && rect.width > 300) {
                        el.style.display = 'none';
                    }
                });
            }""")

            # X streams quoted tweets and media after the outer article is visible.
            # Expand collapsed text before waiting for the final layout.
            for _ in range(3):
                expanded = False
                for button in article.locator("button").all():
                    try:
                        if button.inner_text().strip().lower() in {
                            "显示更多", "show more", "展开", "read more"
                        }:
                            button.click(timeout=1_000)
                            expanded = True
                    except Exception:
                        continue
                if not expanded:
                    break
                page.wait_for_timeout(400)

            # Scroll through the capture scope once so X lazy-loads quoted posts,
            # conversation parents, and media below the initial viewport.
            scope_top = reply_scope_top
            page.evaluate("""async (scopeTop) => {
                const article = document.querySelector("article[data-x-capture-target='true']");
                if (!article) return;
                const original = window.scrollY;
                const rect = article.getBoundingClientRect();
                const top = Math.max(0, (scopeTop === null ? rect.top : scopeTop) + original - 80);
                let bottom = top + (rect.bottom - (scopeTop === null ? rect.top : scopeTop));
                for (let y = top; y < bottom; y += Math.max(320, window.innerHeight * 0.8)) {
                    window.scrollTo(0, y);
                    await new Promise((resolve) => setTimeout(resolve, 180));
                    bottom = Math.max(bottom, top + article.getBoundingClientRect().height);
                }
                window.scrollTo(0, original);
            }""", scope_top)
            _dismiss_cookie_consent(page)
            # Touch X's nested media containers so lazy-loaded quote images
            # enter the viewport before the readiness check below.
            page.evaluate("""() => {
                const article = document.querySelector("article[data-x-capture-target='true']");
                if (!article) return;
                article.querySelectorAll(
                    "[data-testid='tweetPhoto'], [data-testid='videoPlayer'], video"
                ).forEach((node) => node.scrollIntoView({block: "center"}));
            }""")
            page.wait_for_timeout(500)
            # Give failed media one explicit retry. If X still reports a
            # failure, keep that panel in the screenshot so the tweet remains
            # complete instead of silently deleting part of its content.
            page.evaluate("""() => {
                const article = document.querySelector("article[data-x-capture-target='true']");
                if (!article) return;
                const retry = /^(重新载入|重试|reload|try again)$/i;
                for (const el of article.querySelectorAll('button,[role="button"],a')) {
                    const label = (el.innerText || el.getAttribute('aria-label') || '').trim();
                    if (retry.test(label)) el.click();
                }
            }""")
            try:
                page.wait_for_function(
                    """() => {
                        const article = document.querySelector("article[data-x-capture-target='true']");
                        if (!article) return false;
                        const expected_media_count = __EXPECTED_MEDIA_COUNT__;
                        const images_ready = [...article.querySelectorAll('img')]
                            .filter(img => img.src && !img.src.startsWith('data:'))
                            .every(img => img.complete && (img.naturalWidth > 0
                                || !img.src.includes('pbs.twimg.com')));
                        const videos_ready = [...article.querySelectorAll('video')].every(video =>
                            video.readyState >= 2 || Boolean(video.error));
                        const media_nodes = [...new Set(article.querySelectorAll(
                            "[data-testid='tweetPhoto'], [data-testid='videoPlayer'], video"
                        ))];
                        const loaded_media = media_nodes.filter(node => {
                            const image_ready = [...node.querySelectorAll('img')]
                                .some(img => img.complete && img.naturalWidth > 0);
                            const video = node.matches('video') ? node : node.querySelector('video');
                            return image_ready || Boolean(video && video.readyState >= 2);
                        }).length;
                        const media_failed = [...article.querySelectorAll('*')].some(el => {
                            const text = (el.innerText || '').trim();
                            return text.length > 0 && text.length < 80
                                && /无法播放该媒体|media could not be played/i.test(text);
                        });
                        const fonts_ready = !document.fonts || document.fonts.status === 'loaded';
                        const nested_media_ready = expected_media_count === 0
                            || loaded_media >= expected_media_count
                            || media_failed;
                        return images_ready && (videos_ready || media_failed)
                            && nested_media_ready && fonts_ready;
                    }""".replace("__EXPECTED_MEDIA_COUNT__", str(expected_media_count)),
                    timeout=min(self.timeout_seconds * 1_000, 30_000),
                )
            except Exception:
                # A slow or broken media must still be captured with the full
                # article; the visible X error panel is useful diagnostic data.
                pass
            _dismiss_cookie_consent(page)
            page.wait_for_timeout(1_500)
            # Use device pixels so macOS/Retina captures retain the same sharp
            # typography and roughly 2x resolution as the reference screenshot.
            # Replies keep their conversation context by clipping the column
            # from the nearest ancestor tweets down through the target tweet.
            # Measure the clip only after media has settled the layout, then
            # use fullPage: long conversation chains exceed the viewport
            # height, and X's virtual list unloads articles outside it.
            capture_clip = None
            if is_reply:
                layout = page.evaluate(_CONVERSATION_SCOPE_JS)
                if layout:
                    capture_clip = _conversation_clip(
                        layout.get("target"), layout.get("ancestors") or []
                    )
            if capture_clip:
                page.screenshot(
                    path=str(image_path),
                    clip=capture_clip,
                    full_page=True,
                    scale="device",
                )
            else:
                article.screenshot(path=str(image_path), scale="device")
            version_marker.write_text(CAPTURE_VERSION, encoding="utf-8")
            return image_path
        except Exception as exc:
            if is_target_dead_error(exc):
                # The browser died mid-capture; do not leave the corpse in
                # place. The next start() launches a fresh browser.
                self.close()
            raise
        finally:
            try:
                page.close()
            except Exception:
                pass

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
