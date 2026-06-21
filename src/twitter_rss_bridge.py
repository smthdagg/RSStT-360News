#!/usr/bin/env python3
"""
X.com (Twitter) RSS Bridge Service
===================================
Uses X internal GraphQL API (via httpx/urllib + SOCKS5 proxy) to fetch tweets
and serves them as local RSS feeds for RSStT bot.

Architecture:
- Background worker thread owns the X API client
- On-demand fetch requests are queued to the worker
- HTTP server serves cached RSS on demand
- Uses SOCKS5 proxy (Shadowrocket port 7897) to access x.com
- Uses cookies from config/x_session.json for authentication

Endpoints:
  GET /twitter/user/{username}  → RSS XML feed
  GET /health                   → Health check
  GET /users                    → List all X users in DB
  GET /refresh                  → Trigger immediate refresh
"""

import os
import sys
import json
import time
import re
import threading
import queue
import signal
import sqlite3
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path

# Add src to path for importing bot config
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# ── Monkey-patch httpx for twscrape compatibility ──────────────────────────
# twscrape expects httpx >= 0.28 with AsyncHTTPTransport, but we have 0.13.3.
# We use a custom client anyway, but twscrape parsers are imported.
try:
    import httpx
    from httpcore import AsyncConnectionPool
    class _CompatAsyncHTTPTransport(AsyncConnectionPool):
        def __init__(self, retries=3, **kwargs):
            super().__init__(**kwargs)
    httpx.AsyncHTTPTransport = _CompatAsyncHTTPTransport
except Exception:
    pass

# ── Configuration ──────────────────────────────────────────────────────────

PORT = 1200
FETCH_TIMEOUT = 30  # seconds for API calls
X_URL = "https://x.com"

# Config folder
CONFIG_DIR = Path(__file__).parent.parent / "config"
SESSION_FILE = CONFIG_DIR / "x_session.json"
BRIDGE_CONFIG = CONFIG_DIR / "xbridge_config.json"

# SOCKS5 proxy for Shadowrocket VPN
SOCKS5_PROXY = "socks5://127.0.0.1:7897"

# X API constants
X_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"

# GraphQL query IDs (extracted from X.com JS bundles, via twscrape)
QUERY_USER_BY_SCREEN_NAME = "681MIj51w00Aj6dY0GXnHw"
QUERY_USER_TWEETS = "RyDU3I9VJtPF-Pnl6vrRlw"

# Feature switches required by UserTweets (all enabled)
USER_TWEETS_FEATURES = {
    "rweb_video_screen_enabled": True,
    "rweb_cashtags_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": True,
    "premium_content_api_read_enabled": True,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": True,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "rweb_conversational_replies_downvote_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_enhance_cards_enabled": True,
}

# Field toggles for UserTweets
USER_TWEETS_FIELD_TOGGLES = {
    "withPayments": True,
    "withAuxiliaryUserLabels": True,
    "withArticleRichContentState": True,
    "withArticlePlainText": True,
    "withArticleSummaryText": True,
    "withArticleVoiceOver": True,
    "withGrokAnalyze": True,
    "withDisallowedReplyControls": True,
}

# Features for UserByScreenName
USER_BY_SCREEN_NAME_FEATURES = {
    "hidden_profile_subscriptions_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": True,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}


def get_cache_ttl():
    """Read CACHE_TTL from config file (or env var, or default)."""
    try:
        if BRIDGE_CONFIG.exists():
            cfg = json.loads(BRIDGE_CONFIG.read_text())
            val = int(cfg.get('interval', 600))
            return max(60, val)
    except:
        pass
    try:
        val = int(os.environ.get('XBRIDGE_INTERVAL', '600'))
        return max(60, val)
    except:
        return 600


# ── Globals ────────────────────────────────────────────────────────────────

cache = {}
cache_lock = threading.Lock()
fetch_queue = queue.Queue()
shutdown_event = threading.Event()


# ── Database helpers ───────────────────────────────────────────────────────

def get_x_users():
    """Query the RSStT database for all X/Twitter subscriptions."""
    db_path = CONFIG_DIR / "db.sqlite3"
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT s.id, s.tags, s.title, f.link
            FROM sub s
            JOIN feed f ON s.feed_id = f.id
            WHERE f.link LIKE '%127.0.0.1%'
            ORDER BY s.tags, s.title
        """)
        rows = cur.fetchall()

        users = []
        seen_usernames = set()
        for row in rows:
            link = row['link']
            username = extract_username_from_local(link)
            if username and username not in seen_usernames:
                seen_usernames.add(username)
                users.append({
                    'username': username,
                    'tags': row['tags'] or '',
                    'title': row['title'] or username,
                })
        return users
    finally:
        conn.close()


def extract_username_from_local(link):
    """Extract X username from local bridge URL."""
    m = re.search(r'/twitter/user/([^/]+)', link)
    if m:
        return m.group(1).lower()
    return None


# ── X API Client ─────────────────────────────────────────────────────────────

class XAPIClient:
    """
    Synchronous X GraphQL API client using urllib + SOCKS5 proxy.
    No Playwright, no async, no complex dependencies.
    """

    def __init__(self):
        self._cookie_str = None
        self._ct0 = None
        self._opener = None

    def start(self):
        """Initialize the API client with cookies from session file."""
        print("[XBridge][API] Initializing X API client...")
        self._load_session()
        self._setup_opener()
        print("[XBridge][API] Client ready")

    def _load_session(self):
        """Load cookies from session file."""
        if not SESSION_FILE.exists():
            print("[XBridge][API] ⚠ No session file found, API calls may fail")
            self._cookie_str = ""
            self._ct0 = ""
            return

        with open(SESSION_FILE) as f:
            data = json.load(f)

        cookies_list = data.get('cookies', data)
        cookies_dict = {}
        for c in cookies_list:
            name = c.get('name', '')
            value = c.get('value', '')
            cookies_dict[name] = value

        self._ct0 = cookies_dict.get('ct0', '')
        self._cookie_str = '; '.join([f'{k}={v}' for k, v in cookies_dict.items()])

        has_auth = bool(cookies_dict.get('auth_token'))
        print(f"[XBridge][API] Session loaded: {len(cookies_dict)} cookies, auth={has_auth}")

    def _setup_opener(self):
        """Create urllib opener with SOCKS5 proxy."""
        proxy_support = urllib.request.ProxyHandler({
            'http': SOCKS5_PROXY,
            'https': SOCKS5_PROXY,
        })
        self._opener = urllib.request.build_opener(proxy_support)

    def _graphql(self, query_id: str, operation_name: str, variables: dict,
                 features: dict = None) -> dict:
        """Make a GraphQL POST request to X API."""
        body = json.dumps({
            "variables": variables,
            "features": features or {},
        }).encode('utf-8')

        url = f'https://x.com/i/api/graphql/{query_id}/{operation_name}'

        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/131.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {X_BEARER_TOKEN}',
            'X-Csrf-Token': self._ct0,
            'X-Twitter-Auth-Type': 'OAuth2Session',
            'X-Twitter-Active-User': 'yes',
            'X-Twitter-Client-Language': 'en',
            'Origin': 'https://x.com',
            'Referer': 'https://x.com/',
        }
        if self._cookie_str:
            headers['Cookie'] = self._cookie_str

        req = urllib.request.Request(url, data=body, headers=headers)

        try:
            r = self._opener.open(req, timeout=FETCH_TIMEOUT)
            resp_body = r.read().decode('utf-8')
            return json.loads(resp_body)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8', errors='replace')
            print(f"[XBridge][API] HTTP {e.code} for {operation_name}: {error_body[:200]}")
            return json.loads(error_body) if error_body else {}
        except Exception as e:
            print(f"[XBridge][API] Error in {operation_name}: {e}")
            raise

    def get_user_by_screen_name(self, screen_name: str) -> dict:
        """
        Get user information by screen name.
        Returns the user result dict, or None if not found.
        """
        data = self._graphql(
            QUERY_USER_BY_SCREEN_NAME,
            "UserByScreenName",
            {"screen_name": screen_name, "withSafetyModeUserFields": True},
            USER_BY_SCREEN_NAME_FEATURES,
        )
        try:
            return data['data']['user']['result']
        except (KeyError, TypeError):
            print(f"[XBridge][API] Failed to parse user data for @{screen_name}")
            return None

    def get_user_tweets(self, user_id: str, count: int = 40) -> dict:
        """
        Get tweets for a user by their user ID (string).
        Returns the raw API response dict.
        """
        variables = {
            "userId": str(user_id),
            "count": count,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Video": True,
        }
        features = {**USER_TWEETS_FEATURES, **USER_TWEETS_FIELD_TOGGLES}
        return self._graphql(
            QUERY_USER_TWEETS,
            "UserTweets",
            variables,
            features,
        )

    def stop(self):
        """Clean up resources."""
        print("[XBridge][API] Client stopped")


# ── Tweet parsing ───────────────────────────────────────────────────────────

def extract_tweets_from_timeline(api_response: dict) -> list:
    """
    Extract tweet result dicts from the UserTweets API response.
    Returns a flat list of tweet result dicts (the 'result' object inside each entry).
    Also returns users dict for reference.
    """
    tweets = []  # list of tweet result dicts
    users = {}   # user_id -> user result dict

    try:
        instructions = api_response['data']['user']['result']['timeline']['timeline']['instructions']
    except (KeyError, TypeError):
        print("[XBridge][API] Unexpected API response structure")
        return tweets, users

    for instr in instructions:
        if instr.get('type') == 'TimelineAddEntries':
            for entry in instr.get('entries', []):
                tweet_result = _extract_tweet_from_entry(entry)
                if tweet_result:
                    tweets.append(tweet_result)
                    # Also collect user data if present
                    _collect_users_from_tweet(tweet_result, users)

    return tweets, users


def _extract_tweet_from_entry(entry: dict) -> dict:
    """Extract a tweet result dict from a timeline entry."""
    try:
        content = entry.get('content', {})
        item_content = content.get('itemContent', {})

        # Direct tweet result
        if item_content.get('__typename') == 'TimelineTweet':
            result = item_content.get('tweet_results', {}).get('result')
            if result and result.get('__typename') in ('Tweet', 'TweetWithVisibilityResults'):
                return result

        # Entry might have tweet directly in content
        tweet_results = content.get('tweet_results', {}).get('result')
        if tweet_results and tweet_results.get('__typename') in ('Tweet', 'TweetWithVisibilityResults'):
            return tweet_results

    except Exception:
        pass
    return None


def _collect_users_from_tweet(tweet: dict, users: dict):
    """Extract user data from a tweet result."""
    try:
        user_result = tweet.get('core', {}).get('user_results', {}).get('result')
        if user_result and user_result.get('__typename') == 'User':
            uid = user_result.get('rest_id') or user_result.get('id_str') or \
                  tweet.get('core', {}).get('user_results', {}).get('result', {}).get('rest_id')
            if uid:
                users[uid] = user_result
    except Exception:
        pass


def parse_tweet_to_dict(tweet_result: dict) -> dict:
    """
    Convert a raw X API tweet result into a flat dict for RSS generation.
    Handles both 'legacy' (old) and 'core' (new) response formats.
    """
    result = {}

    # Tweet ID
    result['id'] = tweet_result.get('rest_id', '')
    if not result['id']:
        legacy = tweet_result.get('legacy', {})
        result['id'] = legacy.get('id_str', '')

    # Tweet URL
    result['url'] = f"https://x.com/i/web/status/{result['id']}"

    # ── Text content ──
    # Try note_tweet first (long-form), then legacy full_text
    full_text = ''
    note_tweet = tweet_result.get('note_tweet', {})
    if note_tweet:
        note_results = note_tweet.get('note_tweet_results', {})
        if note_results:
            note_result = note_results.get('result', {})
            full_text = note_result.get('text', '')

    if not full_text:
        legacy = tweet_result.get('legacy', {})
        full_text = legacy.get('full_text', '') or legacy.get('text', '')

    result['text'] = full_text

    # ── Date ──
    legacy = tweet_result.get('legacy', {})
    created_at = legacy.get('created_at', '')
    if created_at:
        try:
            result['date'] = datetime.strptime(
                created_at, '%a %b %d %H:%M:%S %z %Y'
            )
        except ValueError:
            result['date'] = datetime.now(timezone.utc)
    else:
        result['date'] = datetime.now(timezone.utc)

    result['date_rss'] = result['date'].strftime('%a, %d %b %Y %H:%M:%S +0000')

    # ── User info ──
    # Note: In the new X API, screen_name/name moved from legacy to core
    user_result = tweet_result.get('core', {}).get('user_results', {}).get('result', {})
    user_core = user_result.get('core', {})
    legacy_user = user_result.get('legacy', {})
    result['user'] = {
        'screen_name': (
            user_core.get('screen_name', '')
            or legacy_user.get('screen_name', '')
            or user_result.get('screen_name', '')
        ),
        'name': (
            user_core.get('name', '')
            or legacy_user.get('name', '')
            or user_result.get('name', '')
        ),
        'profile_image': legacy_user.get('profile_image_url_https', ''),
    }

    # ── Media (images, videos) ──
    result['media'] = []
    extended_media = legacy.get('extended_entities', {}).get('media', [])
    if not extended_media:
        extended_media = legacy.get('entities', {}).get('media', [])

    for m in extended_media:
        media_type = m.get('type', '')
        media_url = m.get('media_url_https', '') or m.get('media_url', '')
        if media_type == 'photo' and media_url:
            result['media'].append({
                'type': 'photo',
                'url': media_url,
                'width': m.get('original_info', {}).get('width', 0),
                'height': m.get('original_info', {}).get('height', 0),
            })
        elif media_type == 'video' and media_url:
            # Get the best quality video
            variants = m.get('video_info', {}).get('variants', [])
            best_variant = None
            best_bitrate = -1
            for v in variants:
                if v.get('bitrate', 0) > best_bitrate:
                    best_bitrate = v.get('bitrate', 0)
                    best_variant = v
            result['media'].append({
                'type': 'video',
                'url': best_variant.get('url', media_url) if best_variant else media_url,
                'poster': media_url,
                'duration': m.get('video_info', {}).get('duration_millis', 0),
            })

    # ── Engagement stats ──
    result['stats'] = {
        'reply_count': legacy.get('reply_count', 0),
        'retweet_count': legacy.get('retweet_count', 0),
        'favorite_count': legacy.get('favorite_count', 0),
        'view_count': legacy.get('view_count', 0) or legacy.get('ext_tweet_view_count', {}).get('state', '0'),
    }

    # ── Links ──
    result['links'] = []
    for url_entity in legacy.get('entities', {}).get('urls', []):
        expanded = url_entity.get('expanded_url', '')
        display = url_entity.get('display_url', '')
        if expanded:
            result['links'].append({'url': expanded, 'display': display})

    # ── Retweet / Quote handling ──
    # Check if it's a retweet
    rt_legacy = legacy.get('retweeted_status_result', {}).get('result', {})
    if rt_legacy:
        result['is_retweet'] = True
        result['retweeted_tweet'] = parse_tweet_to_dict(rt_legacy)
    else:
        result['is_retweet'] = False
        result['retweeted_tweet'] = None

    # Check for quoted tweet
    qt_legacy = legacy.get('quoted_status_result', {}).get('result', {})
    if qt_legacy:
        result['is_quote'] = True
        result['quoted_tweet'] = parse_tweet_to_dict(qt_legacy)
    else:
        result['is_quote'] = False
        result['quoted_tweet'] = None

    # ── Language ──
    result['lang'] = legacy.get('lang', 'en')

    # ── Possibly sensitive ──
    result['possibly_sensitive'] = legacy.get('possibly_sensitive', False)

    return result


# ── X API Worker ────────────────────────────────────────────────────────────

class XAPIWorker:
    """
    Worker that owns the X API client and handles tweet fetching.
    Must be created and used from a single thread.
    """

    def __init__(self):
        self._client = None

    def start(self):
        """Initialize the API client."""
        self._client = XAPIClient()
        self._client.start()

    def stop(self):
        """Clean up."""
        if self._client:
            self._client.stop()
        self._client = None
        print("[XBridge][Worker] Stopped")

    def fetch_tweets(self, username: str) -> list:
        """
        Fetch tweets for a single X.com user.
        Returns a list of parsed tweet dicts, or None on error.
        """
        if not self._client:
            print("[XBridge][Worker] Client not ready")
            return None

        try:
            # Step 1: Get user info by screen name
            print(f"[XBridge][API] Fetching user info for @{username}...")
            user_data = self._client.get_user_by_screen_name(username)

            if not user_data:
                print(f"[XBridge][API] ⚠ Could not find user @{username}")
                return None

            # Extract user ID - might be in rest_id or id
            user_id = user_data.get('rest_id') or user_data.get('id', '')
            if not user_id:
                # Try to extract from legacy data
                legacy = user_data.get('legacy', {})
                user_id = legacy.get('id_str', '')
            if not user_id:
                # Extract from the base64 ID format
                uid = user_data.get('id', '')
                if uid and uid.startswith('VXNlcjo'):
                    try:
                        import base64
                        decoded = base64.b64decode(uid.replace('VXNlcjo', ''))
                        user_id = decoded.decode('utf-8')
                    except:
                        pass

            if not user_id:
                print(f"[XBridge][API] ⚠ Could not determine user ID for @{username}")
                return None

            # screen_name may be in core, not legacy, in the new API
            user_core = user_data.get('core', {})
            user_legacy = user_data.get('legacy', {})
            user_screen_name = (
                user_core.get('screen_name', '')
                or user_legacy.get('screen_name', '')
                or username
            )
            print(f"[XBridge][API] @{username} -> user_id={user_id}, screen_name={user_screen_name}")

            # Step 2: Get user tweets
            print(f"[XBridge][API] Fetching tweets for @{username}...")
            api_response = self._client.get_user_tweets(user_id, count=40)

            # Step 3: Extract and parse tweets
            raw_tweets, _ = extract_tweets_from_timeline(api_response)

            if not raw_tweets:
                print(f"[XBridge][API] ⚠ No tweets found for @{username} (timeline empty)")
                # This could be due to rate limiting, suspended account, etc.
                # Check for errors in the response
                if 'errors' in api_response:
                    for err in api_response['errors']:
                        print(f"[XBridge][API]   Error: {err.get('message', str(err))}")
                return []

            # Step 4: Parse each tweet
            parsed_tweets = []
            for raw_tweet in raw_tweets:
                try:
                    parsed = parse_tweet_to_dict(raw_tweet)
                    if parsed['id'] and parsed['text']:
                        parsed_tweets.append(parsed)
                except Exception as e:
                    print(f"[XBridge][API]   Warning: failed to parse tweet: {e}")

            print(f"[XBridge][API] @{username}: {len(parsed_tweets)} tweets parsed")
            return parsed_tweets

        except Exception as e:
            print(f"[XBridge][API] Error fetching @{username}: {e}")
            import traceback
            traceback.print_exc()
            return None


# ── RSS generation ────────────────────────────────────────────────────────

def timestamp_to_rss_date(dt):
    """Convert datetime to RSS pubDate format."""
    return dt.strftime('%a, %d %b %Y %H:%M:%S +0000')


def generate_rss(username: str, tweets: list) -> str:
    """Generate RSS XML from a list of parsed tweet dicts."""
    now = datetime.now(timezone.utc)
    now_rss = timestamp_to_rss_date(now)

    items = []
    seen_ids = set()

    for tweet in tweets:
        tid = tweet['id']
        if tid in seen_ids:
            continue
        seen_ids.add(tid)

        pub_date = tweet.get('date_rss', now_rss)
        text = tweet['text']

        # Build description from tweet text + media links
        description = text

        # Add media links to description
        media_links = []
        for m in tweet.get('media', []):
            if m['type'] == 'photo':
                media_links.append(f'<img src="{xml_escape(m["url"])}" />')
            elif m['type'] == 'video':
                media_links.append(f'<video poster="{xml_escape(m["poster"])}" controls><source src="{xml_escape(m["url"])}"></video>')
        if media_links:
            description += '\n\n' + '\n'.join(media_links)

        # Add link to quoted tweet if present
        quoted = tweet.get('quoted_tweet')
        if quoted and quoted.get('url'):
            description += f'\n\n🔗 Quote: {quoted["url"]}'

        # Handle retweets
        rt = tweet.get('retweeted_tweet')
        if rt:
            rt_user = rt.get('user', {}).get('screen_name', '')
            rt_text = rt.get('text', '')
            description = f'🔁 RT @{rt_user}: {rt_text}'
            if rt.get('media'):
                for m in rt['media']:
                    if m['type'] == 'photo':
                        description += f'\n<img src="{xml_escape(m["url"])}" />'

        # Build enclosure tags for media
        enclosures = []
        media_list = tweet.get('media', [])
        if not media_list and rt:
            media_list = rt.get('media', [])

        for m in media_list:
            if m['type'] == 'photo':
                enclosures.append(
                    f'<enclosure url="{xml_escape(m["url"])}" type="image/jpeg" length="0"/>'
                )

        enclosure_block = '\n' + '\n'.join(enclosures) if enclosures else ''

        # Title: first line or truncated text
        title = text.split('\n')[0] if text else '(no text)'
        if len(title) > 200:
            title = title[:197] + '...'

        item = f"""    <item>
      <title>{xml_escape(title)}</title>
      <link>{xml_escape(tweet['url'])}</link>
      <guid isPermaLink="true">{xml_escape(tweet['url'])}</guid>
      <pubDate>{pub_date}</pubDate>
      <description>{xml_escape(description)}</description>{enclosure_block}
    </item>"""
        items.append(item)

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>X.com - @{username}</title>
    <link>https://x.com/{username}</link>
    <description>Recent tweets from @{username}</description>
    <language>en</language>
    <lastBuildDate>{now_rss}</lastBuildDate>
    <atom:link href="http://127.0.0.1:{PORT}/twitter/user/{username}" rel="self" type="application/rss+xml"/>
{chr(10).join(items)}
  </channel>
</rss>"""
    return rss


# ── Cache management ──────────────────────────────────────────────────────

def update_cache_entry(username, tweets):
    """Update cache with fetched tweets."""
    with cache_lock:
        if tweets is not None:
            rss = generate_rss(username, tweets)
            cache[username] = {
                'rss': rss,
                'updated_at': time.time(),
                'error': None,
            }
            print(f"[XBridge] ✓ @{username}: {len(tweets)} tweets cached")
        else:
            if username in cache:
                cache[username]['error'] = 'Fetch failed'
            else:
                empty_rss = generate_rss(username, [])
                cache[username] = {
                    'rss': empty_rss,
                    'updated_at': time.time(),
                    'error': 'Fetch failed',
                }
            print(f"[XBridge] ✗ @{username}: fetch failed")


def cache_worker():
    """
    Background worker thread that:
    1. Owns the X API client
    2. Periodically refreshes all X user caches
    3. Processes on-demand fetch requests from the queue
    """
    print("[XBridge] Cache worker starting...")
    worker = XAPIWorker()

    try:
        worker.start()
    except Exception as e:
        print(f"[XBridge] Failed to start X API worker: {e}")
        import traceback
        traceback.print_exc()
        return

    while not shutdown_event.is_set():
        try:
            # 1. Process any on-demand requests first
            processed = 0
            while not fetch_queue.empty() and not shutdown_event.is_set():
                try:
                    item = fetch_queue.get_nowait()
                except queue.Empty:
                    break

                username = item
                print(f"[XBridge] On-demand fetch for @{username}")
                tweets = worker.fetch_tweets(username)
                update_cache_entry(username, tweets)
                processed += 1
                time.sleep(2)  # Small delay between requests

            # 2. Get users from database
            users = get_x_users()
            if not users:
                print("[XBridge] No X users found in database")
                shutdown_event.wait(60)
                continue

            # 3. Refresh stale caches
            refresh_count = 0
            for user in users:
                if shutdown_event.is_set():
                    break

                # Check queue first
                while not fetch_queue.empty():
                    try:
                        q_item = fetch_queue.get_nowait()
                    except queue.Empty:
                        break
                    if shutdown_event.is_set():
                        break
                    print(f"[XBridge] Priority fetch for @{q_item}")
                    tweets = worker.fetch_tweets(q_item)
                    update_cache_entry(q_item, tweets)
                    time.sleep(2)

                username = user['username']

                with cache_lock:
                    if username in cache:
                        age = time.time() - cache[username]['updated_at']
                        if age < get_cache_ttl():
                            continue

                print(f"[XBridge] Refreshing @{username}...")
                tweets = worker.fetch_tweets(username)
                update_cache_entry(username, tweets)
                refresh_count += 1
                time.sleep(2)

            if refresh_count > 0:
                print(f"[XBridge] Refreshed {refresh_count} users, sleeping {get_cache_ttl()}s")

            # Wait for next cycle
            shutdown_event.wait(30)

        except Exception as e:
            print(f"[XBridge] Cache worker error: {e}")
            import traceback
            traceback.print_exc()
            shutdown_event.wait(10)

    worker.stop()
    print("[XBridge] Cache worker stopped")


# ── HTTP Server ───────────────────────────────────────────────────────────

class RSSBridgeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the RSS bridge."""

    def _now_rss(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')

    def do_GET(self):
        path = urlparse(self.path).path.rstrip('/')

        if path == '/health':
            with cache_lock:
                cache_status = {k: {
                    'cached': True,
                    'age': int(time.time() - v['updated_at']),
                    'error': v.get('error'),
                } for k, v in cache.items()}
            self._send_json(200, {
                'status': 'ok',
                'users_cached': len(cache),
                'cache_status': cache_status if len(cache) < 10 else f"{len(cache)} users",
            })

        elif path.startswith('/twitter/user/'):
            username = path.split('/')[-1]
            if not username:
                self._send_error(400, 'Missing username')
                return

            with cache_lock:
                if username in cache and cache[username]['rss']:
                    entry = cache[username]
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/rss+xml; charset=utf-8')
                    self.send_header('Cache-Control', f'max-age={get_cache_ttl()}')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(entry['rss'].encode('utf-8'))
                    return

            # Not in cache - queue a fetch
            if not shutdown_event.is_set():
                fetch_queue.put_nowait(username)

            empty_rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>X.com - @{username}</title>
    <link>https://x.com/{username}</link>
    <description>Waiting for first fetch...</description>
    <language>en</language>
    <lastBuildDate>{self._now_rss()}</lastBuildDate>
    <atom:link href="http://127.0.0.1:{PORT}/twitter/user/{username}" rel="self" type="application/rss+xml"/>
  </channel>
</rss>"""
            self.send_response(200)
            self.send_header('Content-Type', 'application/rss+xml; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(empty_rss.encode('utf-8'))

        elif path == '/users':
            users = get_x_users()
            self._send_json(200, {
                'count': len(users),
                'users': [u['username'] for u in users],
            })

        elif path == '/refresh':
            users = get_x_users()
            for u in users:
                if not shutdown_event.is_set():
                    fetch_queue.put_nowait(u['username'])
            self._send_json(200, {
                'status': 'refresh_started',
                'users_queued': len(users),
            })

        elif path == '/cache':
            with cache_lock:
                info = {}
                for k, v in cache.items():
                    info[k] = {
                        'age_seconds': int(time.time() - v['updated_at']),
                        'error': v.get('error'),
                        'rss_size': len(v['rss']),
                    }
            self._send_json(200, info)

        else:
            self._send_error(404, 'Not found. Endpoints: /twitter/user/{username}, /health, /users, /refresh, /cache')

    def _send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False, indent=2)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body.encode('utf-8'))

    def _send_error(self, status, message):
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(message.encode('utf-8'))

    def log_message(self, format, *args):
        path = str(args[0]) if args else ''
        if '/health' not in path:
            print(f"[XBridge HTTP] {self.client_address[0]} - {args[0]} {args[1]}")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    print("═" * 50)
    print("  X.com RSS Bridge Service (X API)")
    print("═" * 50)
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Port: {PORT}")
    print(f"  Cache TTL: {get_cache_ttl()}s")
    print(f"  Proxy: {SOCKS5_PROXY}")
    print()

    # List X users from database
    users = get_x_users()
    if users:
        print(f"  Found {len(users)} X users:")
        for u in users:
            print(f"    • @{u['username']:20s}  [{u['tags']}]")
    else:
        print("  ⚠ No X users found in database!")
        print("  (add subscriptions via RSStT bot first)")

    # Check login status
    if SESSION_FILE.exists():
        print(f"  ✓ X.com session loaded ({SESSION_FILE.name})")
    else:
        print(f"  ⚠ No X.com session file found!")
        print(f"      Run: python3 scripts/load_x_session.py")

    print()
    print(f"  RSS: http://127.0.0.1:{PORT}/twitter/user/{{username}}")
    print(f"  Health: http://127.0.0.1:{PORT}/health")
    print()

    # Register signal handler for graceful shutdown
    def shutdown_handler(sig, frame):
        print("\n[XBridge] Shutting down...")
        shutdown_event.set()
        threading.Thread(target=lambda: (
            time.sleep(5), print("[XBridge] Forced exit"), os._exit(0)
        ), daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Start background cache worker (owns X API client)
    worker = threading.Thread(target=cache_worker, daemon=True)
    worker.start()

    # Small delay to let worker initialize
    time.sleep(2)

    # Start HTTP server
    server = HTTPServer(('127.0.0.1', PORT), RSSBridgeHandler)
    print(f"[XBridge] Server listening on http://127.0.0.1:{PORT}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[XBridge] Interrupted")
    finally:
        print("[XBridge] Stopping server...")
        server.server_close()
        shutdown_event.set()
        print("[XBridge] Goodbye!")


if __name__ == '__main__':
    main()
