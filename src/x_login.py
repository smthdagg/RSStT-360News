#!/usr/bin/env python3
"""
X.com 会话提取工具
==================
直接从你本机 Chrome 浏览器提取 X.com 的登录状态，
保存为桥接服务可用的会话文件。

使用方法：
  1. 先在 Chrome 中登录 X.com（如果还没登录）
  2. 关闭所有 Chrome 窗口
  3. 运行: .venv/bin/python3 src/x_login.py

原理：Playwright 使用你本机 Chrome 的用户数据目录，
继承已有的登录状态，无需重复输入密码。
"""

import os
import sys
import json
import time
from pathlib import Path

CONFIG_DIR = Path(__file__).parent.parent / "config"
SESSION_FILE = CONFIG_DIR / "x_session.json"

from playwright.sync_api import sync_playwright


def main():
    print("=" * 50)
    print("  X.com 会话提取工具")
    print("=" * 50)
    print()
    
    if SESSION_FILE.exists():
        print(f"  已有会话文件: {SESSION_FILE}")
        resp = input("  重新提取？(y/N): ").strip().lower()
        if resp != 'y':
            print("  使用现有会话。")
            return
    
    # Find Chrome profile
    chrome_profile = (
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Default"
    )
    
    if not chrome_profile.exists():
        print(f"  ❌ 找不到 Chrome 配置目录")
        print(f"     预期路径: {chrome_profile}")
        print()
        print("  请先安装 Google Chrome 并登录 X.com。")
        return
    
    print(f"  ✓ 找到 Chrome 配置: {chrome_profile}")
    print()
    
    # Check if Chrome is running
    import subprocess
    try:
        result = subprocess.run(
            ['pgrep', '-f', 'Google Chrome'],
            capture_output=True, text=True
        )
        chrome_running = result.returncode == 0 and len(result.stdout.strip()) > 0
    except:
        chrome_running = False
    
    if chrome_running:
        print("  ⚠ Chrome 正在运行！")
        print("    需要先完全退出 Chrome（Cmd+Q），否则无法读取配置。")
        print("    请:\n    1. 先在 Chrome 中确认已登录 X.com")
        print("    2. 完全退出 Chrome (Cmd+Q)")
        print("    3. 重新运行此脚本")
        return
    
    print("  正在启动 Chrome（无头模式）读取登录状态...")
    print()
    
    with sync_playwright() as p:
        try:
            # Launch persistent context with the user's Chrome profile
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(chrome_profile),
                headless=True,
                viewport={'width': 1280, 'height': 800},
                args=['--window-size=1280,800'],
            )
            
            page = context.pages[0] if context.pages else context.new_page()
            
            # Go to X.com home to check login status
            page.goto('https://x.com/home', wait_until='domcontentloaded')
            page.wait_for_timeout(3000)
            
            # Check if logged in
            title = page.title()
            body = page.inner_text('body')
            
            if 'What is happening' in body or 'Home' in title:
                print("  ✓ 检测到 X.com 已登录！")
            elif 'login' in page.url.lower() or 'Log in' in body:
                print("  ⚠ 未检测到登录状态。请先在 Chrome 中登录 X.com。")
                print()
                print("  步骤：")
                print("    1. 打开 Chrome，访问 x.com")
                print("    2. 登录你的账号")
                print("    3. 完全退出 Chrome (Cmd+Q)")
                print("    4. 重新运行此脚本")
                context.close()
                return
            
            # Save storage state
            context.storage_state(path=str(SESSION_FILE))
            session_size = SESSION_FILE.stat().st_size if SESSION_FILE.exists() else 0
            print(f"  ✓ 会话已保存! ({session_size:,} bytes)")
            
            # Verify: try to access a restricted account
            page.goto('https://x.com/cailianpress', wait_until='domcontentloaded')
            page.wait_for_timeout(2000)
            articles = page.query_selector_all('article')
            
            if len(articles) > 0:
                print(f"  ✓ 验证成功：可查看 @cailianpress 的 {len(articles)} 条推文")
            else:
                page.evaluate('window.scrollBy(0, 2000)')
                page.wait_for_timeout(2000)
                articles = page.query_selector_all('article')
                print(f"  ✓ 会话有效 (@cailianpress: {len(articles)} 篇文章)")
            
            context.close()
            
        except Exception as e:
            print(f"  ❌ 错误: {e}")
            return
    
    print()
    print("  ✅ 完成！现在可以启动桥接服务了。")
    print(f"     会话文件: {SESSION_FILE}")


if __name__ == '__main__':
    main()
