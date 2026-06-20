"""
翻译辅助模块 v2 (ZCode)
为 RSStT 提供英文→中文翻译能力（Google Translate 免费接口）
v2: 移除长度限制、自动重试、多后端回退
"""
from __future__ import annotations

import re
import logging
import asyncio
import hashlib
from typing import Optional, Callable

logger = logging.getLogger('RSStT.translate')

# 中文检测正则（更宽松：包含任何 CJK 字符即视为中文）
CN_PATTERN = re.compile(r'[\u3000-\u9fff\uff00-\uffef]')

# 缓存：LRU 字典
_translation_cache: dict[str, str] = {}
_CACHE_MAX = 2000

# 限流信号量：每次只允许 1 个请求（避免触发 Google 限流）
_rate_limiter = asyncio.Semaphore(1)

# 重试配置
_MAX_RETRIES = 2  # 减少重试次数，避免雪上加霜
_BACKOFF = 2.0  # 增加初始等待到 2 秒


def contains_chinese(text: str) -> bool:
    """检查是否包含中文字符"""
    return bool(CN_PATTERN.search(text))


def should_translate(text: Optional[str]) -> bool:
    """判断是否需要翻译 — 仅非中文文本才需要翻译"""
    if not text or not text.strip():
        return False
    
    # 剥离 HTML 标签后再判断
    clean = re.sub(r'<[^>]+>', '', text).strip()
    if not clean:
        return False
    
    # 如果包含中文字符，不翻译
    if contains_chinese(clean):
        return False
    
    return True


def _cache_key(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()


# === 翻译后端 ===

async def _translate_google(text: str, target: str = 'zh-CN') -> Optional[str]:
    """Google Translate (deep_translator)，自动重试"""
    for attempt in range(_MAX_RETRIES):
        try:
            def _sync():
                from deep_translator import GoogleTranslator
                t = GoogleTranslator(source='auto', target=target)
                return t.translate(text)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _sync)
            if result and result.strip():
                return result
        except Exception as e:
            logger.debug(f'Google Translate attempt {attempt + 1} failed: {e}')
            if attempt < _MAX_RETRIES - 1:
                await asyncio.sleep(_BACKOFF * (attempt + 1))
    return None


async def _translate_fallback(text: str, target: str = 'zh-CN') -> Optional[str]:
    """回退后端：googletrans（另一个库，备胎）"""
    try:
        def _sync():
            from googletrans import Translator
            t = Translator()
            return t.translate(text, dest=target).text
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _sync)
        if result and result.strip():
            return result
    except Exception as e:
        logger.debug(f'Fallback translate failed: {e}')
    return None


async def translate(text: str, target: str = 'zh-CN') -> str:
    """翻译文本：先 GoogleTranslate，失败则回退到 googletrans"""
    if not should_translate(text):
        return text

    ck = _cache_key(text)
    if ck in _translation_cache:
        return _translation_cache[ck]

    async with _rate_limiter:
        result = await _translate_google(text, target)
        if not result:
            logger.info('Google Translate 失败，尝试回退后端...')
            result = await _translate_fallback(text, target)
        if not result:
            logger.warning('所有翻译后端均失败，返回原文')
            result = text

    # 缓存
    if len(_translation_cache) >= _CACHE_MAX:
        _translation_cache.clear()
    _translation_cache[ck] = result

    if result != text:
        logger.debug(f'Translated: "{text[:40]}..." → "{result[:40]}..."')
    return result


async def translate_post(title: Optional[str], content: Optional[str]) -> tuple[str, str]:
    """翻译一篇文章的标题和正文"""
    tasks = []
    if should_translate(title):
        tasks.append(translate(title))
    else:
        tasks.append(asyncio.sleep(0, result=(title or '')))

    if should_translate(content):
        tasks.append(translate(content))
    else:
        tasks.append(asyncio.sleep(0, result=(content or '')))

    results = await asyncio.gather(*tasks)
    return results[0], results[1]
