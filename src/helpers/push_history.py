"""
推送历史管理器 (ZCode)
记录每条推送的文章历史，3 天自动清理，支持一键清空和逐条管理
"""
from __future__ import annotations

import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger('RSStT.push_history')

# 历史保留天数
RETENTION_DAYS = 3

# DB 文件路径（RSStT 配置目录）
HISTORY_DB = None  # 由 init() 设置


def init(config_dir: str):
    """初始化历史数据库"""
    global HISTORY_DB
    HISTORY_DB = Path(config_dir) / 'push_history.db'
    _ensure_table()


def _ensure_table():
    if HISTORY_DB is None or not HISTORY_DB.parent.exists():
        return
    conn = sqlite3.connect(str(HISTORY_DB))
    conn.execute('''
        CREATE TABLE IF NOT EXISTS push_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_title TEXT,
            post_title TEXT,
            post_url TEXT,
            post_time TEXT,
            push_time TEXT,
            feed_id INTEGER,
            category TEXT DEFAULT ''
        )
    ''')
    conn.commit()
    conn.close()
    _cleanup()


def _cleanup():
    """删除超过 RETENTION_DAYS 的记录"""
    if HISTORY_DB is None or not HISTORY_DB.exists():
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        conn = sqlite3.connect(str(HISTORY_DB))
        cur = conn.execute('DELETE FROM push_history WHERE push_time < ?', (cutoff,))
        deleted = cur.rowcount
        if deleted > 0:
            logger.debug(f'Cleaned up {deleted} old push history entries')
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f'Push history cleanup failed: {e}')


async def record(feed_title: str, post_title: Optional[str], post_url: Optional[str],
                 pub_time: Optional[datetime], feed_id: int = 0, category: str = ''):
    """记录一条推送"""
    if HISTORY_DB is None or not HISTORY_DB.parent.exists():
        return
    _ensure_table()
    try:
        conn = sqlite3.connect(str(HISTORY_DB))
        conn.execute(
            'INSERT INTO push_history (feed_title, post_title, post_url, post_time, push_time, feed_id, category) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (
                feed_title[:200] if feed_title else '',
                post_title[:500] if post_title else '',
                post_url[:1000] if post_url else '',
                pub_time.isoformat() if pub_time else '',
                datetime.now(timezone.utc).isoformat(),
                feed_id,
                category[:100] if category else ''
            )
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f'Failed to record push history: {e}')


# === 查询接口（供 Electron 调用）===

def list_recent(limit: int = 200) -> list[dict]:
    """获取最近推送记录"""
    if HISTORY_DB is None or not HISTORY_DB.exists():
        return []
    _cleanup()
    try:
        conn = sqlite3.connect(str(HISTORY_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT * FROM push_history ORDER BY push_time DESC LIMIT ?',
            (limit,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning(f'Failed to list push history: {e}')
        return []


def count_recent() -> int:
    """统计最近 3 天的推送数量"""
    if HISTORY_DB is None or not HISTORY_DB.exists():
        return 0
    _cleanup()
    try:
        conn = sqlite3.connect(str(HISTORY_DB))
        row = conn.execute('SELECT COUNT(*) AS c FROM push_history').fetchone()
        conn.close()
        return row[0] if row else 0
    except:
        return 0


def delete_entry(entry_id: int) -> bool:
    """删除单条记录"""
    if HISTORY_DB is None or not HISTORY_DB.exists():
        return False
    try:
        conn = sqlite3.connect(str(HISTORY_DB))
        conn.execute('DELETE FROM push_history WHERE id = ?', (entry_id,))
        conn.commit()
        conn.close()
        return True
    except:
        return False


def clear_all() -> bool:
    """清空所有记录"""
    if HISTORY_DB is None or not HISTORY_DB.exists():
        return False
    try:
        conn = sqlite3.connect(str(HISTORY_DB))
        conn.execute('DELETE FROM push_history')
        conn.commit()
        conn.close()
        return True
    except:
        return False
