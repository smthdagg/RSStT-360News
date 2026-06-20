#!/usr/bin/env python3
"""
X.com (Twitter) RSS Bridge Service
====================================
Uses Playwright (headless Chromium) to scrape tweets from X.com
and serves them as local RSS feeds for RSStT bot.

Architecture:
- Background worker thread owns the Playwright browser
  (Playwright sync API is not thread-safe)
- On-demand fetch requests are queued to the worker
- HTTP server serves cached RSS on demand
- Uses system VPN (Shadowrocket) to access x.com

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
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path

# Add src to path for importing bot config
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from playwright.sync_api import sync_playwright

# ── Configuration ──────────────────────────────────────────────────────────

PORT = 1200
# CACHE_TTL is now dynamically determined by get_cache_ttl() — see below
BROWSER_HEADLESS = True
FETCH_TIMEOUT = 35000
X_URL = "https://x.com"

# Config folder for SQLite DB
CONFIG_DIR = Path(__file__).parent.parent / "config"
SESSION_FILE = CONFIG_DIR / "x_session.json"
BRIDGE_CONFIG = CONFIG_DIR / "xbridge_config.json"
SCREENSHOT_DIR = CONFIG_DIR / "tweet_screenshots"

def get_cache_ttl():
    """Read CACHE_TTL from config file (or env var, or default).
    Can be called repeatedly to pick up config changes."""
    # 1. Try config file
    try:
        if BRIDGE_CONFIG.exists():
            cfg = json.loads(BRIDGE_CONFIG.read_text())
            val = int(cfg.get('interval', 600))
            return max(60, val)
    except:
        pass
    # 2. Try env var
    try:
        val = int(os.environ.get('XBRIDGE_INTERVAL', '600'))
        return max(60, val)
    except:
        return 600

# ── Globals ────────────────────────────────────────────────────────────────

# Cache: {username: {'rss': str, 'updated_at': float, 'error': str|None}}
cache = {}
cache_lock = threading.Lock()

# Queue for on-demand fetch requests (thread-safe)
fetch_queue = queue.Queue()

# Event to signal worker shutdown
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


# ── Timestamp parsing ─────────────────────────────────────────────────────

def parse_x_timestamp(text, tweet_id=None):
    """
    Parse X.com timestamp text into a datetime object.
    Supports English and Chinese date formats.
    Falls back to extracting timestamp from tweet Snowflake ID.
    """
    text = text.strip()
    now = datetime.now(timezone.utc)
    
    if not text:
        return _tweet_id_to_time(tweet_id) or now
    
    from datetime import timedelta as td
    
    # ── Relative timestamps ──
    # English: "1h", "2m", "30m", "1s"
    m = re.match(r'^(\d+)([smhd])$', text)
    if m:
        value = int(m.group(1))
        unit = m.group(2)
        if unit == 's': return now
        elif unit == 'm': return (now - td(minutes=value)).replace(second=0)
        elif unit == 'h': return (now - td(hours=value)).replace(second=0, minute=0)
        elif unit == 'd': return (now - td(days=value)).replace(second=0, minute=0, hour=0)
    
    # Chinese: "N小时前", "N分钟前", "N天前"
    m = re.match(r'^(\d+)(小时|分钟|分钟|秒钟|天|周)前$', text)
    if m:
        value = int(m.group(1))
        unit = m.group(2)
        if '秒' in unit: return now
        elif '分' in unit: return (now - td(minutes=value)).replace(second=0)
        elif '小时' in unit: return (now - td(hours=value)).replace(second=0, minute=0)
        elif '天' in unit: return (now - td(days=value)).replace(second=0, minute=0, hour=0)
        elif '周' in unit: return (now - td(weeks=value)).replace(second=0, minute=0, hour=0)
    
    # ── Absolute timestamps ──
    
    # English: "Mon DD, YYYY" e.g. "Apr 28, 2022"
    try:
        return datetime.strptime(text, "%b %d, %Y").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    
    # English: "Mon DD" e.g. "Jun 15" (current year)
    try:
        dt = datetime.strptime(f"{text} {now.year}", "%b %d %Y")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    
    # Chinese: "YYYY年M月D日" e.g. "2025年3月18日"
    m = re.match(r'^(\d{4})年(\d{1,2})月(\d{1,2})日$', text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                       tzinfo=timezone.utc)
    
    # Chinese: "M月D日" e.g. "6月15日" (current year)
    m = re.match(r'^(\d{1,2})月(\d{1,2})日$', text)
    if m:
        dt = datetime(now.year, int(m.group(1)), int(m.group(2)))
        return dt.replace(tzinfo=timezone.utc)
    
    # ── Fallback: extract from tweet Snowflake ID ──
    if tweet_id:
        extracted = _tweet_id_to_time(tweet_id)
        if extracted:
            return extracted
    
    return now


def _tweet_id_to_time(tweet_id):
    """
    Extract creation time from a Twitter/X Snowflake ID.
    Twitter's epoch: 1288834974657 ms (Nov 4, 2010 01:42:54 UTC)
    """
    try:
        tid = int(tweet_id)
        timestamp_ms = (tid >> 22) + 1288834974657
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


# ── Playwright worker (runs in dedicated thread) ──────────────────────────

class PlaywrightWorker:
    """Owns the Playwright browser instance and handles all fetch operations.
    Must be created and used from a single thread."""
    
    def __init__(self):
        self._pw = None
        self._browser = None
    
    def start(self):
        """Initialize Playwright and launch browser."""
        print("[XBridge][PW] Starting Playwright...")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=BROWSER_HEADLESS)
        print("[XBridge][PW] Browser ready")
    
    def stop(self):
        """Clean up browser resources."""
        print("[XBridge][PW] Stopping Playwright...")
        try:
            if self._browser:
                self._browser.close()
        except:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except:
            pass
        self._browser = None
        self._pw = None
        print("[XBridge][PW] Stopped")
    
    def fetch_tweets(self, username):
        """
        Fetch tweets for a single X.com user.
        Returns a list of tweet dicts, or None on error.
        
        Automatically retries without session if session is invalid/expired.
        """
        if not self._browser:
            print("[XBridge][PW] Browser not ready")
            return None
        
        # Try with session first, fall back to no session if invalid
        session_available = SESSION_FILE.exists()
        
        for attempt, use_session in enumerate([session_available, False]):
            if attempt == 1 and not session_available:
                # Only one attempt if no session file
                break
            
            storage_state = str(SESSION_FILE) if use_session else None
            
        for attempt, use_session in enumerate([session_available, False]):
            if attempt == 1 and not session_available:
                # Only one attempt if no session file
                break
            
            storage_state = str(SESSION_FILE) if use_session else None
            
            context = self._browser.new_context(
                viewport={'width': 1280, 'height': 4096},
                user_agent=(
                    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/125.0.0.0 Safari/537.36'
                ),
                locale='en-US',
                timezone_id='America/New_York',
                storage_state=storage_state,
            )
            page = context.new_page()
            
            try:
                page.goto(f"{X_URL}/{username}", wait_until='domcontentloaded', timeout=FETCH_TIMEOUT)
                
                # Wait for articles to appear
                try:
                    page.wait_for_selector('article', timeout=15000)
                except:
                    pass
                # Extra wait for dynamic rendering
                time.sleep(3)
                
                # Click all "Show more" buttons to expand truncated tweet content
                try:
                    expanded = page.evaluate("""
                        () => {
                            const buttons = document.querySelectorAll('button');
                            let count = 0;
                            buttons.forEach(btn => {
                                if (btn.textContent.trim() === 'Show more' && btn.offsetParent !== null) {
                                    btn.click();
                                    count++;
                                }
                            });
                            return count;
                        }
                    """)
                    if expanded > 0:
                        print(f"[XBridge][PW] Expanded {expanded} truncated tweets")
                        time.sleep(2)  # Wait for content to render after expansion
                except Exception as e:
                    print(f"[XBridge][PW] Failed to expand tweets: {e}")
                
                articles = page.query_selector_all('article')
                tweets = []
                
                # 截图目录
                screenshot_dir = CONFIG_DIR / 'tweet_screenshots'
                screenshot_dir.mkdir(parents=True, exist_ok=True)
                
                for article in articles:
                    status_links = article.query_selector_all('a[href*="/status/"]')
                    if not status_links:
                        continue
                    
                    status_link = status_links[0]
                    href = status_link.get_attribute('href') or ''
                    if not href:
                        continue
                    
                    if href.startswith('/'):
                        href = f"{X_URL}{href}"
                    
                    m = re.search(r'/status/(\d+)', href)
                    if not m:
                        continue
                    
                    tweet_id = m.group(1)
                    timestamp_text = status_link.inner_text().strip()
                    
                    full_text = article.inner_text()
                    content_lines = [l.strip() for l in full_text.split('\n') if l.strip()]
                    
                    # 对推文截图
                    screenshot_path = screenshot_dir / f"{username}_{tweet_id}.png"
                    try:
                        article.screenshot(path=str(screenshot_path), timeout=5000)
                    except Exception as e:
                        print(f"[XBridge][PW] Screenshot failed for {tweet_id}: {e}")
                        screenshot_path = None
                    
                    tweets.append({
                        'id': tweet_id,
                        'url': href,
                        'timestamp_text': timestamp_text,
                        'text': full_text,
                        'lines': content_lines,
                        'screenshot': str(screenshot_path) if screenshot_path and screenshot_path.exists() else None,
                    })
                
                # If session produced 0 articles, try without session (expired session)
                if use_session and len(tweets) == 0 and session_available:
                    session_label = "with session"
                    no_session_label = "without session"
                    print(f"[XBridge][PW] @{username}: 0 tweets {session_label}, retrying {no_session_label}")
                    # Mark session as potentially expired
                    if SESSION_FILE.exists():
                        print(f"[XBridge][PW] Session appears invalid, will retry without it")
                    page.close()
                    context.close()
                    continue  # Try next attempt (without session)
                
                return tweets
            
            except Exception as e:
                print(f"[XBridge][PW] Error fetching @{username}: {e}")
                if use_session and session_available:
                    print(f"[XBridge][PW] Retrying @{username} without session...")
                    page.close()
                    context.close()
                    continue
                return None
            
            finally:
                page.close()
                context.close()
            
            break  # Only reached if we didn't continue (success)
        
        return None  # All attempts failed
    
    def scrape_page(self, url):
        """
        Generic page scraper — uses Playwright to load any URL
        and extract main content as RSS items.
        Used for non-X.com subscriptions that need JS rendering.
        """
        if not self._browser:
            return None
        
        context = self._browser.new_context(
            viewport={'width': 1280, 'height': 4096},
            user_agent=(
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                'AppleWebKit/537.36 (KHTML, like Gecko) '
                'Chrome/125.0.0.0 Safari/537.36'
            ),
            locale='en-US',
        )
        page = context.new_page()
        
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=FETCH_TIMEOUT)
            try:
                page.wait_for_selector('article, main, .content, .post, .entry', timeout=10000)
            except:
                pass
            time.sleep(2)
            
            title = page.title() or url
            body_text = page.inner_text('body')
            
            # Try to find article elements, otherwise use body
            articles = page.query_selector_all('article')
            if not articles:
                # Fallback: use the whole page body as one item
                items_html = page.inner_html('body')
            else:
                items_html = ''.join(a.inner_html() for a in articles)
            
            # Generate RSS with the page title and content
            items = []
            if articles:
                for article in articles[:20]:
                    text = article.inner_text().strip()
                    if not text:
                        continue
                    # Find first link for the item URL
                    link_el = article.query_selector('a[href]')
                    link = link_el.get_attribute('href') if link_el else url
                    if link.startswith('/'):
                        from urllib.parse import urlparse
                        parsed = urlparse(url)
                        link = f"{parsed.scheme}://{parsed.netloc}{link}"
                    
                    lines = text.split('\n')
                    first_line = lines[0] if lines else text[:100]
                    
                    items.append(f"""    <item>
      <title>{xml_escape(first_line[:200])}</title>
      <link>{xml_escape(link)}</link>
      <guid>{xml_escape(link)}</guid>
      <description>{xml_escape(text[:1000])}</description>
    </item>""")
            else:
                # No articles found — wrap the whole page as one item
                items.append(f"""    <item>
      <title>{xml_escape(title)}</title>
      <link>{xml_escape(url)}</link>
      <guid>{xml_escape(url)}</guid>
      <description>{xml_escape(body_text[:2000])}</description>
    </item>""")
            
            rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>{xml_escape(title)}</title>
    <link>{xml_escape(url)}</link>
    <description>Scraped from {xml_escape(url)}</description>
    <language>en</language>
    <lastBuildDate>{self._now_rss()}</lastBuildDate>
{chr(10).join(items)}
  </channel>
</rss>"""
            return rss
        
        except Exception as e:
            print(f"[XBridge][PW] Error scraping {url}: {e}")
            return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Scrape Error: {xml_escape(url)}</title>
    <link>{xml_escape(url)}</link>
    <description>Failed to scrape: {xml_escape(str(e))}</description>
    <language>en</language>
    <lastBuildDate>{self._now_rss()}</lastBuildDate>
  </channel>
</rss>"""
        
        finally:
            page.close()
            context.close()
    
    def _now_rss(self):
        """Return current UTC time in RSS format."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')


# ── RSS generation ────────────────────────────────────────────────────────

def timestamp_to_rss_date(dt):
    """Convert datetime to RSS pubDate format."""
    return dt.strftime('%a, %d %b %Y %H:%M:%S +0000')


def generate_rss(username, tweets):
    """Generate RSS XML from a list of tweet dicts."""
    now = datetime.now(timezone.utc)
    now_rss = timestamp_to_rss_date(now)
    
    items = []
    seen_ids = set()
    
    for tweet in tweets:
        tid = tweet['id']
        if tid in seen_ids:
            continue
        seen_ids.add(tid)
        
        pub_date = timestamp_to_rss_date(parse_x_timestamp(tweet['timestamp_text'], tweet['id']))
        
        lines = tweet['lines']
        # Filter out metadata lines to get tweet content
        content_parts = []
        for line in lines:
            if re.match(r'^@\w+', line):
                continue  # @handle
            if re.match(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+', line):
                continue  # English date line
            if line == 'Article':
                continue  # "Article" label in reposts
            # English engagement stats: "7.2M Views", "1.5K replies", "Read 1.5K replies"
            if re.match(r'^\d[\d.]*[MK]?\s*(Views|replies|likes|views|Retweets|Likes)', line):
                continue
            if '·' in line and re.search(r'(Views|views|replies|likes)', line):
                continue
            if re.match(r'^Read\s+\d+[\d,]*\s+repl', line):
                continue
            # Chinese engagement stats: "1.3万次查看", "11万次查看"
            if re.match(r'^[\d.]+万', line):
                continue  # "1.3万次查看", "11万次查看"
            if re.search(r'次查看|次播放|次浏览|查看次数', line):
                continue  # View counts in Chinese
            if re.search(r'[回复评论][数:：]\s*\d', line):
                continue  # "回复数 1.5K", "回复: 123"
            if re.match(r'^[\d.]+[万亿]', line):
                continue  # Just number + 万/亿
            # Chinese timestamp lines: "下午11:17 · 2025年3月18日"
            if re.search(r'\d{4}年\d{1,2}月\d{1,2}日', line):
                continue
            content_parts.append(line)
        
        title_text = content_parts[-1] if content_parts else tweet['text']
        if len(title_text) > 200:
            title_text = title_text[:197] + '...'
        
        description = '\n\n'.join(content_parts) if len(content_parts) > 1 else title_text
        
        # 截图 enclosure — 通过 HTTP 提供
        screenshot = tweet.get('screenshot')
        enclosure_tag = ''
        if screenshot:
            screen_url = f"http://127.0.0.1:{PORT}/screenshots/{username}_{tweet['id']}.png"
            enclosure_tag = f'\n      <enclosure url="{xml_escape(screen_url)}" type="image/png" length="{Path(screenshot).stat().st_size}"/>'
        
        item = f"""    <item>
      <title>{xml_escape(title_text)}</title>
      <link>{xml_escape(tweet['url'])}</link>
      <guid isPermaLink="true">{xml_escape(tweet['url'])}</guid>
      <pubDate>{pub_date}</pubDate>
      <description>{xml_escape(description)}</description>{enclosure_tag}
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
    1. Owns the Playwright browser
    2. Periodically refreshes all X user caches
    3. Processes on-demand fetch requests from the queue
    """
    print("[XBridge] Cache worker starting...")
    pw = PlaywrightWorker()
    
    try:
        pw.start()
    except Exception as e:
        print(f"[XBridge] Failed to start Playwright: {e}")
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
                
                # item 可以是 string（X 用户名）或 ('scrape', url) 元组
                if isinstance(item, tuple) and item[0] == 'scrape':
                    _, scrape_url = item
                    print(f"[XBridge] Scraping URL: {scrape_url}")
                    rss = pw.scrape_page(scrape_url)
                    if rss:
                        with cache_lock:
                            key = f"_scrape_{scrape_url}"
                            cache[key] = {'rss': rss, 'updated_at': time.time(), 'error': None}
                else:
                    username = item
                    print(f"[XBridge] On-demand fetch for @{username}")
                    tweets = pw.fetch_tweets(username)
                    update_cache_entry(username, tweets)
                processed += 1
                time.sleep(3)
            
            # 2. Get users from database
            users = get_x_users()
            if not users:
                print("[XBridge] No X users found in database")
                shutdown_event.wait(60)
                continue
            
            # 3. Refresh stale caches (check queue between each user)
            refresh_count = 0
            for user in users:
                if shutdown_event.is_set():
                    break
                
                # Check queue first (prioritize on-demand requests)
                processed_queue = 0
                while not fetch_queue.empty():
                    try:
                        q_item = fetch_queue.get_nowait()
                    except queue.Empty:
                        break
                    if shutdown_event.is_set():
                        break
                    
                    if isinstance(q_item, tuple) and q_item[0] == 'scrape':
                        _, scrape_url = q_item
                        print(f"[XBridge] Priority scrape: {scrape_url}")
                        rss = pw.scrape_page(scrape_url)
                        if rss:
                            with cache_lock:
                                key = f"_scrape_{scrape_url}"
                                cache[key] = {'rss': rss, 'updated_at': time.time(), 'error': None}
                    else:
                        q_username = q_item
                        print(f"[XBridge] Priority fetch for @{q_username}")
                        tweets = pw.fetch_tweets(q_username)
                        update_cache_entry(q_username, tweets)
                    processed_queue += 1
                    time.sleep(3)
                
                username = user['username']
                
                with cache_lock:
                    if username in cache:
                        age = time.time() - cache[username]['updated_at']
                        if age < get_cache_ttl():
                            continue
                
                print(f"[XBridge] Refreshing @{username}...")
                tweets = pw.fetch_tweets(username)
                update_cache_entry(username, tweets)
                refresh_count += 1
                time.sleep(3)  # Rate limiting between users
            
            if refresh_count > 0:
                print(f"[XBridge] Refreshed {refresh_count} users, sleeping {get_cache_ttl()}s")
            else:
                # No refresh needed, sleep shorter and check queue
                pass
            
            # Wait for next cycle or shutdown
            shutdown_event.wait(30)  # Check every 30 seconds for queue items
        
        except Exception as e:
            print(f"[XBridge] Cache worker error: {e}")
            shutdown_event.wait(10)
    
    # Cleanup
    pw.stop()
    print("[XBridge] Cache worker stopped")


# ── HTTP Server ───────────────────────────────────────────────────────────

class RSSBridgeHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the RSS bridge."""
    
    def _now_rss(self):
        """Return current UTC time in RSS format."""
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
            
            # Try cache first
            with cache_lock:
                if username in cache and cache[username]['rss']:
                    entry = cache[username]
                    self.send_response(200)  # Always return 200, even if last fetch errored
                    self.send_header('Content-Type', 'application/rss+xml; charset=utf-8')
                    self.send_header('Cache-Control', f'max-age={get_cache_ttl()}')
                    self.send_header('Access-Control-Allow-Origin', '*')
                    self.end_headers()
                    self.wfile.write(entry['rss'].encode('utf-8'))
                    return
            
            # Not in cache - return empty RSS (200) and queue a fetch
            # Returning 200 with empty RSS prevents RSStT from incrementing error_count
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
            """Trigger immediate refresh of all users."""
            users = get_x_users()
            for u in users:
                if not shutdown_event.is_set():
                    fetch_queue.put_nowait(u['username'])
            self._send_json(200, {
                'status': 'refresh_started',
                'users_queued': len(users),
            })
        
        elif path == '/cache':
            """Show cache contents."""
            with cache_lock:
                info = {}
                for k, v in cache.items():
                    info[k] = {
                        'age_seconds': int(time.time() - v['updated_at']),
                        'error': v.get('error'),
                        'rss_size': len(v['rss']),
                    }
            self._send_json(200, info)
        
        elif path.startswith('/screenshots/'):
            """提供推文截图"""
            filename = path.split('/')[-1]
            if not filename.endswith('.png'):
                self._send_error(400, 'Only .png allowed')
                return
            safe_path = SCREENSHOT_DIR / filename
            try:
                safe_path = safe_path.resolve()
                if not str(safe_path).startswith(str(SCREENSHOT_DIR.resolve())):
                    self._send_error(403, 'Forbidden')
                    return
            except:
                self._send_error(403, 'Forbidden')
                return
            if safe_path.exists():
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Cache-Control', 'max-age=86400')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                with open(safe_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self._send_error(404, 'Screenshot not found')
        
        elif path == '/scrape':
            """通用网页爬虫端点 — 用 Playwright 抓取任意 URL 并生成 RSS"""
            parsed = urlparse(self.path)
            qs = dict(__import__('urllib.parse').parse_qsl(parsed.query))
            target_url = qs.get('url', None)
            if not target_url:
                self._send_error(400, 'Missing ?url= parameter')
                return
            
            # 加入抓取队列（异步处理）
            if not shutdown_event.is_set():
                fetch_queue.put_nowait(('scrape', target_url))
            
            empty_rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>Scraping: {xml_escape(target_url)}</title>
    <link>{xml_escape(target_url)}</link>
    <description>Waiting for first scrape...</description>
    <language>en</language>
    <lastBuildDate>{self._now_rss()}</lastBuildDate>
  </channel>
</rss>"""
            self.send_response(200)
            self.send_header('Content-Type', 'application/rss+xml; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(empty_rss.encode('utf-8'))
        
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
        """Suppress default HTTP log; use our own."""
        path = str(args[0]) if args else ''
        if '/health' not in path:
            print(f"[XBridge HTTP] {self.client_address[0]} - {args[0]} {args[1]}")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    print("═" * 50)
    print("  X.com RSS Bridge Service (Playwright)")
    print("═" * 50)
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Port: {PORT}")
    print(f"  Cache TTL: {get_cache_ttl()}s")
    print()
    
    # List X users from database
    users = get_x_users()
    if users:
        print(f"  Found {len(users)} X users:")
        for u in users:
            print(f"    • @{u['username']:20s}  [{u['tags']}]")
    else:
        print("  ⚠ No X users found in database!")
    
    # Check login status
    if SESSION_FILE.exists():
        print(f"  ✓ X.com 登录会话已加载 ({SESSION_FILE.name})")
    else:
        print(f"  ⚠ 未登录 X.com！部分账号可能无法抓取推文。")
        print(f"     运行: .venv/bin/python3 src/x_login.py")
    
    print()
    print(f"  RSS: http://127.0.0.1:{PORT}/twitter/user/{{username}}")
    print(f"  Health: http://127.0.0.1:{PORT}/health")
    print()
    
    # Register signal handler for graceful shutdown
    def shutdown_handler(sig, frame):
        print("\n[XBridge] Shutting down...")
        shutdown_event.set()
        # Force exit after timeout
        threading.Thread(target=lambda: (
            time.sleep(5), print("[XBridge] Forced exit"), os._exit(0)
        ), daemon=True).start()
    
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    
    # Start background cache worker (owns Playwright)
    worker = threading.Thread(target=cache_worker, daemon=True)
    worker.start()
    
    # Small delay to let Playwright initialize
    time.sleep(3)
    
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
