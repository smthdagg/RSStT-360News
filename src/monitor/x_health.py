#!/usr/bin/env python3
"""
X Bridge 健康检查 + Telegram 红色告警
=====================================
定时检查本地 X RSS Bridge 的 /session 端点，
发现持久浏览器会话失效或持续限流时，发红色告警到 MANAGER。

告警触发条件：
  1. bridge 未运行（/session 不可达）
  2. Camoufox 持久会话失效（has_auth=False）
  3. 持续限流超过 30 分钟

为避免重复告警，每次告警后 2 小时内不重复发。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from .. import env

logger = logging.getLogger('RSStT.x_health')

# Bridge 端点
BRIDGE_SESSION_URL = 'http://127.0.0.1:1200/session'
BRIDGE_HEALTH_URL = 'http://127.0.0.1:1200/health'

# 告警冷却：同一种告警 2 小时内不重复
_ALERT_COOLDOWN_S = 2 * 60 * 60
_last_alert_time: dict[str, float] = {}  # alert_type -> timestamp

# 持续限流阈值：连续限流超过这个时间（秒）才告警
_SUSTAINED_RATE_LIMIT_S = 30 * 60  # 30 分钟
_rate_limit_since: Optional[float] = None  # 开始限流的时间点


def _should_alert(alert_type: str) -> bool:
    """检查某类告警是否过了冷却期。"""
    now = time.time()
    last = _last_alert_time.get(alert_type, 0)
    if now - last > _ALERT_COOLDOWN_S:
        _last_alert_time[alert_type] = now
        return True
    return False


async def _fetch_bridge_session() -> Optional[dict]:
    """异步获取 bridge /session 状态。"""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(BRIDGE_SESSION_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        pass
    return None


async def _send_alert(message: str):
    """发红色告警到 MANAGER。"""
    if not env.bot:
        logger.warning('bot 未就绪，无法发送 X bridge 告警')
        return
    # 红色圆点 + 加粗标题，醒目
    alert_text = f'🔴 <b>{message}</b>'
    for manager_id in env.MANAGER:
        try:
            await env.bot.send_message(
                manager_id,
                alert_text,
                parse_mode='html',
            )
            logger.info(f'X bridge 告警已发送到 {manager_id}: {message}')
        except Exception as e:
            logger.error(f'发送 X bridge 告警失败: {e}')


async def check_x_bridge_health():
    """检查 X bridge 健康，异常时发 Telegram 告警。"""
    global _rate_limit_since

    status = await _fetch_bridge_session()

    # ── 检查 1: bridge 不可达 ──
    if status is None:
        if _should_alert('bridge_down'):
            await _send_alert(
                'X Bridge 不可达！\n\n'
                '本地桥接服务 (127.0.0.1:1200) 无响应，X 推文抓取已中断。\n'
                '请检查 bridge 进程是否运行：\n'
                '  cd RSStT && .venv/bin/python -u src/twitter_rss_bridge.py'
            )
        return

    has_auth = status.get('has_auth', False)
    rate_limited = status.get('rate_limited', False)
    last_error = status.get('last_error', '')
    total_fetches = status.get('total_fetches', 0)

    # ── 检查 2: 持久浏览器会话失效 ──
    if not has_auth and _should_alert('auth_invalid'):
        await _send_alert(
            'X 浏览器会话已失效！\n\n'
            'X 推文抓取已中断。\n'
            '请重新建立 Camoufox 会话：\n'
            '  .venv/bin/python3 scripts/load_x_session.py --interactive-login'
        )
        return

    # ── 检查 3: 持续限流超过 30 分钟（可能 cookie 被风控）──
    if rate_limited:
        if _rate_limit_since is None:
            _rate_limit_since = time.time()
        elif time.time() - _rate_limit_since > _SUSTAINED_RATE_LIMIT_S:
            if _should_alert('sustained_rate_limit'):
                expires = status.get('expires_in_human', '未知')
                await _send_alert(
                    f'X 持续限流超 30 分钟！\n\n'
                    f'状态: {expires}\n'
                    f'已抓取次数: {total_fetches}\n'
                    f'上次错误: {last_error}\n\n'
                    f'可能原因：\n'
                    f'  1. X 限流窗口尚未恢复\n'
                    f'  2. 当前账号会话被风控\n\n'
                    f'如持续不恢复，请手动更新会话：\n'
                    f'  .venv/bin/python3 scripts/load_x_session.py --interactive-login'
                )
    else:
        _rate_limit_since = None  # 限流恢复，重置
