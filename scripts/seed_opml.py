#!/usr/bin/env python3
"""
RSStT OPML 种子脚本 (v2 — 含分类标签)
读取 subscriptions.opml，自动导入数据库，支持分类标签
"""
import sys
import os
import xml.etree.ElementTree as ET
import sqlite3
from pathlib import Path
from collections import OrderedDict

RSSTT_ROOT = Path(os.path.dirname(os.path.abspath(__file__))).parent
CONFIG_DIR = RSSTT_ROOT / 'config'
OPML_FILE = CONFIG_DIR / 'subscriptions.opml'
DB_FILE = CONFIG_DIR / 'db.sqlite3'


def get_manager():
    """从 .env 读取 MANAGER"""
    env_file = CONFIG_DIR / '.env'
    if not env_file.exists():
        print(f'[ERROR] .env 文件不存在: {env_file}')
        sys.exit(1)
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith('MANAGER=') and not line.startswith('#'):
            val = line.split('=', 1)[1].strip()
            val = val.split('#')[0].strip()
            if val and not val.startswith('__FILL'):
                try:
                    return int(val.split(';')[0])
                except ValueError:
                    pass
    print('[ERROR] .env 中 MANAGER 未填写或格式错误')
    sys.exit(1)


def parse_opml_with_categories(path):
    """解析 OPML，返回 (category, title, url) 列表"""
    feeds = []
    tree = ET.parse(path)
    root = tree.getroot()
    body = root.find('body')
    if body is None:
        return feeds

    for cat_outline in body.findall('outline'):
        category = (cat_outline.get('title') or cat_outline.get('text', '未分类')).strip()
        for feed_outline in cat_outline.findall('outline'):
            xml_url = feed_outline.get('xmlUrl')
            if xml_url:
                title = feed_outline.get('title') or feed_outline.get('text') or xml_url
                feeds.append({
                    'category': category,
                    'title': title,
                    'url': xml_url,
                })
    return feeds


def seed(db_path, manager_id, feeds, force_reimport=False):
    """将订阅导入数据库"""
    if not db_path.exists():
        print(f'[ERROR] 数据库不存在: {db_path}')
        return False

    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()

    # 检查 manager
    c.execute('SELECT id FROM "user" WHERE id = ?', (manager_id,))
    if not c.fetchone():
        c.execute('INSERT INTO "user" (id, state, lang) VALUES (?, 100, "zh-Hans")', (manager_id,))
        print(f'[INFO] 创建管理员用户: {manager_id}')

    if force_reimport:
        print('[INFO] 清空旧订阅重新导入...')
        c.execute('DELETE FROM sub')
        c.execute('DELETE FROM feed')
        conn.commit()

    stats = {'added': 0, 'existing': 0, 'errors': 0, 'skipped': 0}

    for feed in feeds:
        url = feed['url']
        title = feed['title']
        category = feed['category']
        try:
            # 检查 feed 是否已存在
            c.execute('SELECT id FROM feed WHERE link = ?', (url,))
            feed_row = c.fetchone()
            if feed_row:
                feed_id = feed_row[0]
                # 检查是否已订阅
                c.execute('SELECT id FROM sub WHERE user_id = ? AND feed_id = ?', (manager_id, feed_id))
                sub_row = c.fetchone()
                if sub_row:
                    # 更新分类标签（如果变了）
                    c.execute('UPDATE sub SET tags = ? WHERE id = ?', (category, sub_row[0]))
                    stats['existing'] += 1
                    continue
            else:
                # 创建 feed
                c.execute('INSERT INTO feed (state, link, title) VALUES (1, ?, ?)', (url, title))
                feed_id = c.lastrowid

            # 创建订阅（分类存入 tags 字段）
            c.execute(
                'INSERT INTO sub (state, user_id, feed_id, title, tags) VALUES (1, ?, ?, ?, ?)',
                (manager_id, feed_id, title, category)
            )
            stats['added'] += 1
        except Exception as e:
            stats['errors'] += 1
            if stats['errors'] <= 5:
                print(f'  [WARN] 跳过: {url} — {e}')

    conn.commit()
    conn.close()
    return stats


if __name__ == '__main__':
    print('=== RSStT 订阅导入工具 v2 (含分类标签) ===')
    print()

    if not OPML_FILE.exists():
        print(f'[ERROR] OPML 文件不存在: {OPML_FILE}')
        sys.exit(1)

    manager_id = get_manager()
    print(f'[INFO] 管理员 ID: {manager_id}')

    feeds = parse_opml_with_categories(OPML_FILE)
    print(f'[INFO] OPML 解析完成: {len(feeds)} 条订阅')

    if not feeds:
        print('[ERROR] 未找到任何 RSS 订阅')
        sys.exit(1)

    # 按分类统计
    cats = {}
    for f in feeds:
        cat = f['category']
        cats[cat] = cats.get(cat, 0) + 1
    print(f'[INFO] 分类: {len(cats)} 组')
    for cat, count in sorted(cats.items()):
        print(f'       {cat}: {count} 条')

    # 检查是否需要重新导入
    force = '--force' in sys.argv or '-f' in sys.argv
    stats = seed(DB_FILE, manager_id, feeds, force_reimport=force)

    print()
    print(f'✅ 完成！')
    print(f'   新增订阅: {stats["added"]}')
    print(f'   已存在:   {stats["existing"]}')
    if stats['errors']:
        print(f'   跳过:     {stats["errors"]}')
    print()

    # 分类汇总
    if stats['added'] > 0 or stats['existing'] > 0:
        conn = sqlite3.connect(str(DB_FILE))
        c = conn.cursor()
        c.execute('''
            SELECT sub.tags, COUNT(*) FROM sub
            JOIN "user" ON sub.user_id = "user".id
            WHERE "user".id = ?
            GROUP BY sub.tags ORDER BY sub.tags
        ''', (manager_id,))
        rows = c.fetchall()
        conn.close()
        if rows:
            print('当前订阅分类:')
            for tag, cnt in rows:
                print(f'  {tag}: {cnt} 条')
            total = sum(r[1] for r in rows)
            print(f'  总计: {total} 条')
