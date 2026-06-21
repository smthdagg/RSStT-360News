#!/usr/bin/env python3
"""一键加载 X.com cookies 并重启桥接。
用法: 导出 cookies.json 到 Downloads 后立即运行此脚本。"""

import json, subprocess, time, sys
from pathlib import Path

cookies_path = Path.home() / 'Downloads' / 'cookies.json'
session_path = Path(__file__).parent.parent / 'config' / 'x_session.json'

if not cookies_path.exists():
    print(f"❌ 未找到 {cookies_path}")
    print("请先在 Chrome 中用 EditThisCookie 导出 cookies.json 到 Downloads")
    sys.exit(1)

raw = json.loads(cookies_path.read_text())
raw = [c for c in raw if c['name'] not in ('g_state', '__cf_bm')]

cookies = []
for c in raw:
    cookies.append({
        'name': c['name'], 'value': c['value'],
        'domain': c['domain'], 'path': c.get('path', '/'),
        'expires': c.get('expirationDate', -1),
        'httpOnly': c.get('httpOnly', False),
        'secure': c.get('secure', False),
        'sameSite': {'unspecified': 'None', 'no_restriction': 'None', 'lax': 'Lax'}.get(c.get('sameSite', ''), 'None'),
    })

session_path.write_text(json.dumps({'cookies': cookies, 'origins': []}))
auth = [c['value'][:20] for c in cookies if c['name'] == 'auth_token']
print(f"✅ {len(cookies)} cookies 已保存 (auth_token: {auth[0] if auth else 'N/A'}...)")

# 验证 cookies
import urllib.request
test_headers = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Cookie': f'auth_token={auth[0]}; ct0={[c["value"] for c in cookies if c["name"]=="ct0"][0] if [c["value"] for c in cookies if c["name"]=="ct0"] else ""}',
}
try:
    resp = urllib.request.urlopen(urllib.request.Request('https://x.com/cailianpress', headers=test_headers), timeout=10)
    html = resp.read().decode(errors='replace')
    if 'entry-client-logged-in' in html:
        print("✅ cookies 有效（已登录版本）")
    elif 'entry-client-logged-out' in html:
        print("⚠️  cookies 返回了未登录页面，请重新导出")
        sys.exit(1)
    else:
        print("⚠️  页面状态异常，重试...")
        sys.exit(1)
except Exception as e:
    print(f"❌ cookies 验证失败: {e}")
    sys.exit(1)

# 重启桥接
print("\n重启桥接服务...")
subprocess.run(['pkill', '-f', 'twitter_rss_bridge'], capture_output=True)
time.sleep(2)
proc = subprocess.Popen(
    [sys.executable, str(Path(__file__).parent.parent / 'src' / 'twitter_rss_bridge.py')],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
)
print(f"✅ 桥接已重启 (PID: {proc.pid})")
print("   会话已就绪，X 推文将正常抓取")
