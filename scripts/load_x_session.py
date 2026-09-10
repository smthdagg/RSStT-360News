#!/usr/bin/env python3
"""Import or renew the persistent Camoufox X session, then restart the bridge.

Examples:
  .venv/bin/python scripts/load_x_session.py
  .venv/bin/python scripts/load_x_session.py --interactive-login
  .venv/bin/python scripts/load_x_session.py --account elonmusk --no-restart
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.x_browser_fetcher import XBrowserFetcher, sanitize_x_cookies


SESSION_FILE = ROOT / "config" / "x_session.json"
PROFILE_DIR = ROOT / "config" / "x_browser_profile"
DEFAULT_EXPORT = Path.home() / "Downloads" / "cookies.json"
LOG_FILE = Path("/private/tmp/xbridge_camoufox.log")


def parse_netscape_cookie_export(raw: str) -> list[dict[str, object]]:
    """Parse the standard seven-column browser cookie export format."""
    cookies = []
    for line in raw.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if line.startswith("#") and not http_only:
            continue
        fields = line.removeprefix("#HttpOnly_").split("\t")
        if len(fields) != 7:
            continue
        domain, _, path, secure, expires, name, value = fields
        try:
            expires_at = int(expires)
        except ValueError:
            continue
        cookies.append(
            {
                "domain": domain,
                "path": path or "/",
                "secure": secure.upper() == "TRUE",
                "httpOnly": http_only,
                "expires": expires_at,
                "name": name,
                "value": value,
            }
        )
    return cookies


def parse_cookie_header(raw: str) -> list[dict[str, object]]:
    """Parse a DevTools ``name=value; name2=value2`` cookie header."""
    cookies = []
    for part in raw.split(';'):
        if '=' not in part:
            continue
        name, value = part.split('=', 1)
        if name.strip():
            cookies.append({'domain': '.x.com', 'path': '/', 'secure': True,
                            'name': name.strip(), 'value': value.strip()})
    return cookies


def import_cookie_export(path: Path) -> int:
    text = path.read_text()
    verification_error = None
    try:
        raw = json.loads(text)
        source = raw.get("cookies", raw) if isinstance(raw, dict) else raw
    except json.JSONDecodeError:
        source = parse_netscape_cookie_export(text)
        if not source and '=' in text:
            source = parse_cookie_header(text)
    if not isinstance(source, list):
        raise ValueError("cookie export must be JSON or Netscape cookie text")
    cookies = sanitize_x_cookies(source)
    names = {cookie["name"] for cookie in cookies}
    if "auth_token" not in names or "ct0" not in names:
        raise ValueError("cookie export is missing auth_token or ct0")
    SESSION_FILE.write_text(json.dumps({"cookies": cookies, "origins": []}, indent=2))
    return len(cookies)


def stop_bridge() -> None:
    subprocess.run(["pkill", "-f", "src/twitter_rss_bridge.py"], capture_output=True)
    time.sleep(2)


def restart_bridge() -> int:
    log = LOG_FILE.open("a")
    process = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"), "-u", str(ROOT / "src" / "twitter_rss_bridge.py")],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log.close()
    return process.pid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cookies", type=Path, default=DEFAULT_EXPORT)
    parser.add_argument("--account", default="elonmusk", help="account used for session verification")
    parser.add_argument("--interactive-login", action="store_true")
    parser.add_argument("--no-restart", action="store_true")
    args = parser.parse_args()

    print("=" * 58)
    print("  X.com Camoufox 持久会话导入 / 更新")
    print("=" * 58)
    stop_bridge()

    if args.cookies.exists():
        count = import_cookie_export(args.cookies)
        print(f"✓ 已从 {args.cookies} 导入 {count} 个 X 域 cookie（已去重和过滤）")
    elif not SESSION_FILE.exists() and not args.interactive_login:
        print(f"✗ 未找到 {args.cookies}，也没有旧会话")
        print("  请导出 X cookie，或使用 --interactive-login")
        return 1
    else:
        print("ℹ 未发现新 cookie 导出，继续使用持久 profile")

    fetcher = XBrowserFetcher(
        session_file=SESSION_FILE,
        profile_dir=PROFILE_DIR,
        headless=not args.interactive_login,
        timeout_seconds=60,
    )
    try:
        if args.interactive_login:
            fetcher.open_page("https://x.com/home")
            input("请在 Camoufox 窗口完成 X 登录，确认首页可见后按回车……")
            count = fetcher.save_session_cookies()
            print(f"✓ 已从持久浏览器保存 {count} 个 X cookie")

        tweets = fetcher.fetch_tweets(args.account)
        if not tweets:
            raise RuntimeError(f"@{args.account} 时间线为空")
        print(f"✓ 会话验证成功：@{args.account} 获取 {len(tweets)} 条，HTTP {fetcher.last_status}")
    except Exception as exc:
        verification_error = exc
        print(f"⚠ 会话已导入，但 @{args.account} 验证未完成：{exc}")
    finally:
        fetcher.close()

    if args.no_restart:
        print("ℹ 已按 --no-restart 跳过 Bridge 重启")
        return 0

    pid = restart_bridge()
    print(f"✓ Camoufox Bridge 已重启（PID {pid}）")
    print("  健康检查: http://127.0.0.1:1200/health")
    print(f"  日志: {LOG_FILE}")
    return 1 if verification_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
