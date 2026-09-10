#!/usr/bin/env python3
"""
X.com (Twitter) RSS Bridge Service - Camoufox 版
====================================================
使用持久化 Camoufox 浏览器打开 X 用户页，捕获浏览器自身发出的
UserTweets 响应，再在本地转换为 RSS。

架构：
  - 后台 worker 线程独占一个持久 Camoufox 会话和稳定指纹
  - HTTP 服务器线程按需投递抓取请求到队列
  - cookies 仅从 config/x_session.json 导入 x.com/twitter.com 精确域
  - 明确识别登录失效和 HTTP 429，失败后退避并保留旧缓存

Endpoints:
  GET /twitter/user/{username}  -> RSS XML feed
  GET /health                   -> Health check
  GET /session                  -> 账号状态
  GET /users                    -> List all X users in DB
  GET /refresh                  -> Trigger immediate refresh
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
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path

# Add src to path for importing bot config
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.x_browser_fetcher import (
    XAuthExpiredError,
    XBrowserError,
    XBrowserFetcher,
    XRateLimitedError,
    should_refresh_cache_entry,
)

# ── Configuration ──────────────────────────────────────────────────────────

PORT = 1200
APP_VERSION = os.environ.get('RSSTT_APP_VERSION', 'dev')
FETCH_LIMIT = 40          # 每个 KOL 抓多少条
X_URL = "https://x.com"

# Config folder
CONFIG_DIR = Path(__file__).parent.parent / "config"
SESSION_FILE = CONFIG_DIR / "x_session.json"
BRIDGE_CONFIG = CONFIG_DIR / "xbridge_config.json"
X_BROWSER_PROFILE = CONFIG_DIR / "x_browser_profile"
TWEET_IMAGE_DIR = CONFIG_DIR / "tweet_screenshots"


def get_cache_ttl():
    """Read CACHE_TTL from config file (or env var, or default)."""
    try:
        if BRIDGE_CONFIG.exists():
            cfg = json.loads(BRIDGE_CONFIG.read_text())
            val = int(cfg.get('interval', 600))
            return max(60, val)
    except Exception:
        pass
    try:
        val = int(os.environ.get('XBRIDGE_INTERVAL', '600'))
        return max(60, val)
    except Exception:
        return 600


def get_bridge_config():
    try:
        if BRIDGE_CONFIG.exists():
            data = json.loads(BRIDGE_CONFIG.read_text())
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def configured_x_usernames() -> set[str] | None:
    """Return the optional X-user allowlist from bridge configuration."""
    users = get_bridge_config().get('users')
    if not isinstance(users, list):
        return None
    return {str(user).strip().lstrip('@').lower() for user in users if str(user).strip()}


def get_failure_backoff():
    try:
        return max(300, int(get_bridge_config().get('failure_backoff', 1800)))
    except (TypeError, ValueError):
        return 1800


# ── Globals ────────────────────────────────────────────────────────────────

cache = {}
cache_lock = threading.Lock()
fetch_queue: "queue.Queue[str]" = queue.Queue()
shutdown_event = threading.Event()
xapi_worker = None  # 全局 XAPIWorker 实例，供 HTTP handler 访问状态


# ── Database helpers ───────────────────────────────────────────────────────

def get_x_users():
    """Query the RSStT database for all X/Twitter subscriptions."""
    db_path = CONFIG_DIR / "db.sqlite3"
    if not db_path.exists():
        return []

    allowed_users = configured_x_usernames()
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
            if allowed_users is not None and username not in allowed_users:
                continue
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


def _auth_token_from_session() -> str | None:
    """从 config/x_session.json 读出 auth_token。"""
    if not SESSION_FILE.exists():
        return None
    try:
        data = json.loads(SESSION_FILE.read_text())
        cookies_list = data.get('cookies', data)
        for c in cookies_list:
            if c.get('name') == 'auth_token':
                return c['value']
        return None
    except Exception as e:
        print(f"[XBridge] 读取 session 失败: {e}")
        return None


# ── Scweet-backed fetcher ──────────────────────────────────────────────────

class XAPIWorker:
    """拥有 Camoufox 持久浏览器；只能由 cache worker 线程调用。"""

    def __init__(self):
        self.fetcher: XBrowserFetcher | None = None
        self._last_error: str | None = None
        self._last_fetch_ok: bool = False
        self._total_fetches: int = 0
        self._rate_limited_until: float = 0.0
        self._auth_expired: bool = False

    def start(self):
        """初始化持久 Camoufox 浏览器。"""
        print("[XBridge][Browser] 初始化 Camoufox 持久会话...")
        if not SESSION_FILE.exists() and not X_BROWSER_PROFILE.exists():
            print("[XBridge][Browser] ⚠ 无 X session 或浏览器 profile")
            return False
        try:
            config = get_bridge_config()
            self.fetcher = XBrowserFetcher(
                session_file=SESSION_FILE,
                profile_dir=X_BROWSER_PROFILE,
                headless=bool(config.get('headless', True)),
                timeout_seconds=max(15, int(config.get('fetch_timeout', 45))),
            )
            self.fetcher.start()
            print("[XBridge][Browser] ✓ Camoufox 就绪（持久 profile + 稳定指纹）")
            return True
        except Exception as e:
            print(f"[XBridge][Browser] ⚠ Camoufox 初始化失败: {e}")
            import traceback
            traceback.print_exc()
            self._last_error = str(e)
            return False

    def fetch_tweets(self, username: str) -> list | None:
        """通过浏览器抓取某用户最近推文。"""
        if not self.fetcher or self._auth_expired:
            return None
        now = time.time()
        if now < self._rate_limited_until:
            remaining = int((self._rate_limited_until - now) / 60)
            print(f"[XBridge][Browser] ⏸ 限流冷却中（还剩 ~{remaining} 分钟）")
            return None
        try:
            print(f"[XBridge][Browser] 打开 @{username} ...")
            parsed = self.fetcher.fetch_tweets(username)[:FETCH_LIMIT]
            for tweet in parsed:
                try:
                    self.fetcher.capture_tweet_screenshot(tweet['id'], TWEET_IMAGE_DIR)
                    tweet['screenshot_url'] = (
                        f"http://127.0.0.1:{PORT}/tweet-image/{tweet['id']}.png?v=15"
                    )
                    tweet['screenshot_time'] = datetime.now(timezone.utc)
                except Exception as exc:
                    print(f"[XBridge][Browser] 推文 {tweet['id']} 截图失败: {exc}")
            print(f"[XBridge][Browser] @{username}: 获取 {len(parsed)} 条推文")
            self._last_fetch_ok = True
            self._last_error = None
            self._total_fetches += 1
            return parsed
        except XAuthExpiredError as e:
            self._auth_expired = True
            self._last_error = 'auth_expired'
            print(f"[XBridge][Browser] ⚠ X 登录已失效: {e}")
        except XRateLimitedError as e:
            self._rate_limited_until = now + get_failure_backoff()
            self._last_error = 'rate_limited'
            print(f"[XBridge][Browser] ⚠ X 明确限流，退避 {get_failure_backoff() // 60} 分钟: {e}")
        except XBrowserError as e:
            self._last_error = str(e)
            print(f"[XBridge][Browser] 抓取 @{username} 出错: {e}")
        self._total_fetches += 1
        self._last_fetch_ok = False
        return None

    def is_cooling_down(self):
        return self._auth_expired or time.time() < self._rate_limited_until

    def get_session_status(self) -> dict:
        """供 /session 端点调用。"""
        if not self.fetcher:
            return {'has_session': False, 'is_expired': True,
                    'expires_in_human': '桥接未就绪'}
        now = time.time()
        rate_limited = now < self._rate_limited_until
        if rate_limited:
            remaining = int((self._rate_limited_until - now) / 60)
            status_text = f'限流冷却中（还剩 ~{remaining} 分钟）'
        elif self._auth_expired:
            status_text = '登录已失效，请重新建立浏览器会话'
        elif self._total_fetches == 0:
            status_text = '未抓取过'
        elif self._last_fetch_ok:
            status_text = '正常'
        else:
            status_text = f'上次出错: {self._last_error}' if self._last_error else '正常'
        return {
            'has_session': True,
            'has_auth': not self._auth_expired,
            'is_expired': self._auth_expired,
            'backend': 'Camoufox persistent browser',
            'total_fetches': self._total_fetches,
            'rate_limited': rate_limited,
            'last_error': self._last_error,
            'expires_in_human': status_text,
        }

    def stop(self):
        if self.fetcher:
            self.fetcher.close()
        print("[XBridge][Browser] Camoufox worker stopped")


# ── RSS generation ────────────────────────────────────────────────────────

def timestamp_to_rss_date(dt):
    return dt.strftime('%a, %d %b %Y %H:%M:%S +0000')


def _display_time(value):
    if not value:
        return '未知'
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime('%Y-%m-%d %H:%M:%S %z')


def _display_delay(published, delivered):
    if not published or not delivered:
        return '未知'
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    if delivered.tzinfo is None:
        delivered = delivered.replace(tzinfo=timezone.utc)
    seconds = max(0, int((delivered - published).total_seconds()))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if hours:
        parts.append(f'{hours}小时')
    if minutes:
        parts.append(f'{minutes}分钟')
    if seconds or not parts:
        parts.append(f'{seconds}秒')
    return ''.join(parts)


def _tweet_metadata(tweet, delivered_at):
    user = tweet.get('user') or {}
    handle = user.get('screen_name')
    name = user.get('name') or {'daydayuplift': '天天乐'}.get((handle or '').lower()) or '未知作者'
    author = f'{name} (@{handle})' if handle else name
    published = tweet.get('date')
    return '\n'.join((
        f'作者：{author}',
        f'发文时间：{_display_time(published)}',
        f'截图推送时间：{_display_time(delivered_at)}',
        f'时间差：{_display_delay(published, delivered_at)}',
        f'原文链接：{tweet.get("url", "")}',
    ))


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
        delivered_at = tweet.get('screenshot_time') or now
        description = _tweet_metadata(tweet, delivered_at) + '\n\n' + text

        media_links = []
        screenshot_url = tweet.get('screenshot_url')
        # The screenshot is carried by the RSS enclosure.  Do not also put it
        # in HTML content, otherwise RSStT parses and sends it twice.
        # X posts use the complete browser screenshot as the only media.
        # Sending the original X media as a second enclosure makes Telegram
        # show the wrong image or a mixed media group.
        if not screenshot_url:
            for m in tweet.get('media', []):
                if m['type'] == 'photo':
                    media_links.append(f'<img src="{xml_escape(m["url"])}" />')
                elif m['type'] == 'video':
                    media_links.append(
                        f'<video poster="{xml_escape(m["poster"])}" controls>'
                        f'<source src="{xml_escape(m["url"])}"></video>')
        if media_links:
            description += '\n\n' + '\n'.join(media_links)

        enclosures = []
        if screenshot_url:
            enclosures.append(
                f'<enclosure url="{xml_escape(screenshot_url)}" type="image/png" length="0"/>')
        if not screenshot_url:
            for m in tweet.get('media', []):
                if m['type'] == 'photo':
                    enclosures.append(
                        f'<enclosure url="{xml_escape(m["url"])}" type="image/jpeg" length="0"/>')
        enclosure_block = '\n' + '\n'.join(enclosures) if enclosures else ''

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
    attempted_at = time.time()
    with cache_lock:
        if tweets is not None:
            rss = generate_rss(username, tweets)
            cache[username] = {
                'rss': rss,
                'updated_at': attempted_at,
                'last_attempt_at': attempted_at,
                'error': None,
            }
            print(f"[XBridge] ✓ @{username}: {len(tweets)} 条推文已缓存")
        else:
            # 抓取失败：保留旧缓存内容，只标记 error，不更新时间
            if username in cache:
                cache[username]['error'] = 'fetch_failed'
                cache[username]['last_attempt_at'] = attempted_at
                print(f"[XBridge] ~ @{username}: 抓取失败，保留旧缓存")
            else:
                cache[username] = {
                    'rss': generate_rss(username, []),
                    'updated_at': 0.0,
                    'last_attempt_at': attempted_at,
                    'error': 'fetch_failed',
                }
                print(f"[XBridge] ✗ @{username}: 首次抓取失败")


# ── Worker thread ──────────────────────────────────────────────────────────

def cache_worker():
    """后台线程：独占 Camoufox，周期刷新 KOL 缓存。"""
    print("[XBridge] Cache worker starting...")
    global xapi_worker

    worker = XAPIWorker()
    xapi_worker = worker

    if not worker.start():
        print("[XBridge] ⚠ Camoufox 未就绪，cache worker 退出")
        return

    while not shutdown_event.is_set():
        try:
            # 1. 处理 on-demand 请求
            while not fetch_queue.empty() and not shutdown_event.is_set():
                if worker.is_cooling_down():
                    break
                try:
                    username = fetch_queue.get_nowait()
                except queue.Empty:
                    break
                print(f"[XBridge] On-demand fetch @{username}")
                tweets = worker.fetch_tweets(username)
                update_cache_entry(username, tweets)
                time.sleep(1)

            # 2. 从数据库取 KOL 列表
            users = get_x_users()
            if not users:
                print("[XBridge] 数据库中没有 X 用户")
                shutdown_event.wait(60)
                continue

            # 3. 刷新过期缓存
            refresh_count = 0
            for user in users:
                if shutdown_event.is_set() or worker.is_cooling_down():
                    break
                # 优先处理插队请求
                while not fetch_queue.empty():
                    try:
                        q_item = fetch_queue.get_nowait()
                    except queue.Empty:
                        break
                    if shutdown_event.is_set():
                        break
                    tweets = worker.fetch_tweets(q_item)
                    update_cache_entry(q_item, tweets)
                    time.sleep(1)

                username = user['username']
                with cache_lock:
                    entry = cache.get(username)
                    should_refresh = should_refresh_cache_entry(
                        entry,
                        now=time.time(),
                        cache_ttl=get_cache_ttl(),
                        failure_backoff=get_failure_backoff(),
                    )
                if not should_refresh:
                    continue

                print(f"[XBridge] 刷新 @{username} ...")
                tweets = worker.fetch_tweets(username)
                update_cache_entry(username, tweets)
                refresh_count += 1
                shutdown_event.wait(max(3, int(get_bridge_config().get('request_delay', 12))))

            if refresh_count > 0:
                print(f"[XBridge] 刷新 {refresh_count} 个用户，休眠 {get_cache_ttl()}s")

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
    def _now_rss(self):
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
            session_status = {}
            if xapi_worker:
                session_status = xapi_worker.get_session_status()
            self._send_json(200, {
                'status': 'ok',
                'version': APP_VERSION,
                'backend': 'Camoufox',
                'users_cached': len(cache),
                'cache_status': cache_status if len(cache) < 10 else f"{len(cache)} users",
                'session': session_status,
            })

        elif path == '/session':
            if xapi_worker:
                status = xapi_worker.get_session_status()
            else:
                status = {'has_session': False, 'is_expired': True,
                          'expires_in_human': '桥接未就绪'}
            self._send_json(200, status)

        elif path.startswith('/twitter/user/'):
            username = path.split('/')[-1]
            if not username:
                self._send_error(400, 'Missing username')
                return
            allowed_users = configured_x_usernames()
            if allowed_users is not None and username.lower() not in allowed_users:
                self._send_error(404, 'X user is not enabled')
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

            if not shutdown_event.is_set():
                try:
                    fetch_queue.put_nowait(username)
                except queue.Full:
                    pass

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

        elif path.startswith('/tweet-image/'):
            filename = path.rsplit('/', 1)[-1]
            if not re.fullmatch(r'\d+\.png', filename):
                self._send_error(404, 'Not found')
                return
            image_path = TWEET_IMAGE_DIR / filename
            if not image_path.is_file():
                self._send_error(404, 'Not found')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Cache-Control', 'public, max-age=31536000, immutable')
            self.end_headers()
            self.wfile.write(image_path.read_bytes())

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
                    try:
                        fetch_queue.put_nowait(u['username'])
                    except queue.Full:
                        break
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
            self._send_error(404, 'Not found. Endpoints: /twitter/user/{username}, /health, /session, /users, /refresh, /cache')

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
    print("  X.com RSS Bridge Service (Camoufox)")
    print("═" * 50)
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Port: {PORT}")
    print(f"  Cache TTL: {get_cache_ttl()}s")
    print("  Backend: Camoufox persistent browser")
    print(f"  Profile: {X_BROWSER_PROFILE}")
    print()

    users = get_x_users()
    if users:
        print(f"  Found {len(users)} X users:")
        for u in users:
            print(f"    • @{u['username']:20s}  [{u['tags']}]")
    else:
        print("  ⚠ No X users found in database!")

    if SESSION_FILE.exists():
        print(f"\n  ✓ X.com session found ({SESSION_FILE.name})")
    else:
        print(f"\n  ⚠ No X.com session file: {SESSION_FILE}")
        print("    Run: python3 scripts/load_x_session.py")

    print()
    print(f"  RSS: http://127.0.0.1:{PORT}/twitter/user/{{username}}")
    print(f"  Health: http://127.0.0.1:{PORT}/health")
    print()

    def shutdown_handler(sig, frame):
        print("\n[XBridge] Shutting down...")
        shutdown_event.set()
        threading.Thread(target=lambda: (
            time.sleep(5), print("[XBridge] Forced exit"), os._exit(0)
        ), daemon=True).start()

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    worker = threading.Thread(target=cache_worker, daemon=True)
    worker.start()
    time.sleep(3)

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
