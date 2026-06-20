#!/usr/bin/env python3
"""
RSStT DB 查询桥 — Electron 通过子进程调用此脚本操作 SQLite
用法: python db_query.py <action> [args...]
"""
import sys
import json
import sqlite3
from pathlib import Path

RSSTT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = RSSTT_ROOT / 'config'
DB_FILE = CONFIG_DIR / 'db.sqlite3'
VENV_PYTHON = RSSTT_ROOT / '.venv' / 'bin' / 'python'


def get_db():
    if not DB_FILE.exists():
        return None
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def action_list():
    """列出所有订阅（含分类）"""
    db = get_db()
    if not db:
        return {'ok': False, 'error': '数据库未就绪'}
    rows = db.execute('''
        SELECT
            sub.id, sub.state, sub.title AS sub_title, sub.tags AS category,
            feed.id AS feed_id, feed.link, feed.title AS feed_title
        FROM sub
        JOIN feed ON sub.feed_id = feed.id
        ORDER BY sub.tags, feed.title
    ''').fetchall()
    db.close()
    return {'ok': True, 'data': [dict(r) for r in rows]}


def action_toggle(sub_id):
    """切换订阅启用/禁用"""
    db = get_db()
    if not db:
        return {'ok': False, 'error': '数据库未就绪'}
    row = db.execute('SELECT state FROM sub WHERE id = ?', (sub_id,)).fetchone()
    if not row:
        db.close()
        return {'ok': False, 'error': '订阅不存在'}
    new_state = 0 if row['state'] == 1 else 1
    db.execute('UPDATE sub SET state = ? WHERE id = ?', (new_state, sub_id))
    db.commit()
    db.close()
    return {'ok': True, 'state': new_state}


def action_delete(sub_id):
    """删除订阅，如果 feed 无其他订阅则一并删除"""
    db = get_db()
    if not db:
        return {'ok': False, 'error': '数据库未就绪'}
    sub = db.execute('SELECT feed_id FROM sub WHERE id = ?', (sub_id,)).fetchone()
    if not sub:
        db.close()
        return {'ok': False, 'error': '订阅不存在'}
    db.execute('DELETE FROM sub WHERE id = ?', (sub_id,))
    cnt = db.execute('SELECT COUNT(*) AS c FROM sub WHERE feed_id = ?', (sub['feed_id'],)).fetchone()
    if cnt['c'] == 0:
        db.execute('DELETE FROM feed WHERE id = ?', (sub['feed_id'],))
    db.commit()
    db.close()
    return {'ok': True}


def action_add(url, title, category, manager_id):
    """添加订阅"""
    db = get_db()
    if not db:
        return {'ok': False, 'error': '数据库未就绪'}
    if not url or not manager_id:
        db.close()
        return {'ok': False, 'error': 'URL 和 MANAGER 必填'}
    # 查找或创建 feed
    feed = db.execute('SELECT id FROM feed WHERE link = ?', (url,)).fetchone()
    if feed:
        feed_id = feed['id']
    else:
        cur = db.execute('INSERT INTO feed (state, link, title) VALUES (1, ?, ?)', (url, title or url))
        feed_id = cur.lastrowid
    # 检查是否已订阅
    existing = db.execute('SELECT id FROM sub WHERE user_id = ? AND feed_id = ?', (manager_id, feed_id)).fetchone()
    if existing:
        db.close()
        return {'ok': False, 'error': '已订阅过该源'}
    db.execute(
        'INSERT INTO sub (state, user_id, feed_id, title, tags) VALUES (1, ?, ?, ?, ?)',
        (manager_id, feed_id, title or url, category or '自定义')
    )
    db.commit()
    db.close()
    return {'ok': True}


def action_reimport():
    """从 OPML 重新导入"""
    import subprocess
    seed_script = RSSTT_ROOT / 'scripts' / 'seed_opml.py'
    result = subprocess.run(
        [str(VENV_PYTHON), str(seed_script), '--force'],
        capture_output=True, text=True, cwd=str(RSSTT_ROOT)
    )
    return {
        'ok': result.returncode == 0,
        'output': result.stdout + result.stderr,
        'code': result.returncode
    }


def action_get_manager():
    """从 .env 读取 MANAGER"""
    env_file = CONFIG_DIR / '.env'
    if not env_file.exists():
        return {'ok': False, 'error': '.env 不存在'}
    import re
    for line in env_file.read_text().splitlines():
        m = re.match(r'^\s*MANAGER\s*=\s*(\d+)', line)
        if m:
            return {'ok': True, 'manager_id': int(m.group(1))}
    return {'ok': False, 'error': 'MANAGER 未配置'}


# === 推送历史 ===

HISTORY_DB = CONFIG_DIR / 'push_history.db'


def action_history_list():
    """列出最近推送历史"""
    if not HISTORY_DB.exists():
        return {'ok': True, 'data': []}
    try:
        conn = sqlite3.connect(str(HISTORY_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            'SELECT * FROM push_history ORDER BY push_time DESC LIMIT 200'
        ).fetchall()
        conn.close()
        return {'ok': True, 'data': [dict(r) for r in rows]}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def action_history_count():
    """统计历史条数"""
    if not HISTORY_DB.exists():
        return {'ok': True, 'count': 0}
    try:
        conn = sqlite3.connect(str(HISTORY_DB))
        row = conn.execute('SELECT COUNT(*) AS c FROM push_history').fetchone()
        conn.close()
        return {'ok': True, 'count': row[0] if row else 0}
    except:
        return {'ok': False, 'count': 0}


def action_history_delete(entry_id):
    """删除单条历史"""
    if not HISTORY_DB.exists():
        return {'ok': False, 'error': '无历史记录'}
    try:
        conn = sqlite3.connect(str(HISTORY_DB))
        conn.execute('DELETE FROM push_history WHERE id = ?', (entry_id,))
        conn.commit()
        conn.close()
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


def action_history_clear():
    """清空所有历史"""
    if not HISTORY_DB.exists():
        return {'ok': True}
    try:
        conn = sqlite3.connect(str(HISTORY_DB))
        conn.execute('DELETE FROM push_history')
        conn.commit()
        conn.close()
        return {'ok': True}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


# === 用户管理 ===

def action_users_list():
    """列出所有用户"""
    db = get_db()
    if not db:
        return {'ok': False, 'error': '数据库未就绪'}
    try:
        rows = db.execute('SELECT id, state, lang FROM "user" ORDER BY id').fetchall()
        db.close()
        return {'ok': True, 'data': [dict(r) for r in rows]}
    except Exception as e:
        db.close()
        return {'ok': False, 'error': str(e)}


def action_user_add(user_id):
    """添加用户"""
    db = get_db()
    if not db:
        return {'ok': False, 'error': '数据库未就绪'}
    try:
        existing = db.execute('SELECT id FROM "user" WHERE id = ?', (user_id,)).fetchone()
        if existing:
            db.close()
            return {'ok': False, 'error': f'用户 {user_id} 已存在'}
        db.execute('INSERT INTO "user" (id, state, lang) VALUES (?, 1, "zh-Hans")', (user_id,))
        db.commit()
        db.close()
        return {'ok': True}
    except Exception as e:
        db.close()
        return {'ok': False, 'error': str(e)}


def action_user_remove(user_id):
    """移除用户（同时删除其订阅）"""
    db = get_db()
    if not db:
        return {'ok': False, 'error': '数据库未就绪'}
    try:
        # 检查是否是最后一个管理员
        env_file = CONFIG_DIR / '.env'
        managers = []
        if env_file.exists():
            import re
            for line in env_file.read_text().splitlines():
                m = re.match(r'^\s*MANAGER\s*=\s*(\d+(?:;\d+)*)', line)
                if m:
                    managers = m.group(1).split(';')
        if str(user_id) in managers:
            db.close()
            return {'ok': False, 'error': f'用户 {user_id} 是管理员，无法移除（请先修改 .env 中的 MANAGER）'}
        db.execute('DELETE FROM sub WHERE user_id = ?', (user_id,))
        db.execute('DELETE FROM "user" WHERE id = ?', (user_id,))
        db.commit()
        db.close()
        return {'ok': True}
    except Exception as e:
        db.close()
        return {'ok': False, 'error': str(e)}


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(json.dumps({'ok': False, 'error': 'usage: db_query.py <action> [args...]'}))
        sys.exit(1)

    action = sys.argv[1]

    if action == 'list':
        result = action_list()
    elif action == 'toggle':
        result = action_toggle(int(sys.argv[2]))
    elif action == 'delete':
        result = action_delete(int(sys.argv[2]))
    elif action == 'add':
        result = action_add(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None,
                            sys.argv[4] if len(sys.argv) > 4 else None,
                            int(sys.argv[5]) if len(sys.argv) > 5 else None)
    elif action == 'reimport':
        result = action_reimport()
    elif action == 'get_manager':
        result = action_get_manager()
    elif action == 'history_list':
        result = action_history_list()
    elif action == 'history_count':
        result = action_history_count()
    elif action == 'history_delete':
        result = action_history_delete(int(sys.argv[2]))
    elif action == 'history_clear':
        result = action_history_clear()
    elif action == 'users_list':
        result = action_users_list()
    elif action == 'user_add':
        result = action_user_add(int(sys.argv[2]))
    elif action == 'user_remove':
        result = action_user_remove(int(sys.argv[2]))
    else:
        result = {'ok': False, 'error': f'未知动作: {action}'}

    print(json.dumps(result, ensure_ascii=False))
