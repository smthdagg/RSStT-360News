#!/usr/bin/env python3
"""Refresh the public portal's small, source-linked news index."""

import argparse
import html
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


USER_AGENT = "OpenAIClub-Portal/1.0 (+https://www.example.com/)"
MAX_ITEMS_PER_CATEGORY = 8
SOURCES = (
    {"name": "OpenAI News", "category": "ai", "url": "https://openai.com/news/rss.xml"},
    {"name": "Google AI", "category": "ai", "url": "https://blog.google/innovation-and-ai/technology/ai/rss/"},
    {"name": "Microsoft Blog", "category": "tech", "url": "https://blogs.microsoft.com/feed/"},
    {"name": "CNBC Finance", "category": "markets", "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html"},
    {"name": "Yahoo Finance", "category": "markets", "url": "https://finance.yahoo.com/news/rssindex"},
    {"name": "Federal Reserve", "category": "markets", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"name": "SEC Press Releases", "category": "markets", "url": "https://www.sec.gov/news/pressreleases.rss"},
)


def _local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _element_text(element):
    return "".join(element.itertext()).strip() if element is not None else ""


def _value(record, names):
    for child in record:
        if _local_name(child.tag) in names:
            value = _element_text(child) or child.attrib.get("href", "")
            if value:
                return value
    return ""


def _clean_summary(value):
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:240].rstrip() + ("…" if len(value) > 240 else "")


def _normalise_date(value):
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _is_http_url(value):
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def parse_feed(payload, source, category):
    root = ET.fromstring(payload.lstrip(b"\xef\xbb\xbf"))
    records = [element for element in root.iter() if _local_name(element.tag) in {"item", "entry"}]
    parsed = []
    for record in records:
        title = html.unescape(_value(record, {"title"})).strip()
        link = _value(record, {"link", "guid"}).strip()
        if not title or not _is_http_url(link):
            continue
        parsed.append(
            {
                "category": category,
                "title": title[:180],
                "summary": _clean_summary(_value(record, {"description", "summary", "encoded"})),
                "link": link,
                "source": source,
                "published": _normalise_date(_value(record, {"pubdate", "published", "updated", "date"})),
            }
        )
    return parsed


def _sort_key(item):
    value = item.get("published", "")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def build_payload(source_results, updated_at):
    unique = {}
    statuses = []
    for result in source_results:
        statuses.append(
            {
                "name": result["name"],
                "category": result["category"],
                "url": result["url"],
                "ok": bool(result.get("ok")),
                "count": len(result.get("items", [])),
            }
        )
        for item in result.get("items", []):
            if item["link"] not in unique:
                unique[item["link"]] = item

    counts = {}
    items = []
    for item in sorted(unique.values(), key=_sort_key, reverse=True):
        category = item["category"]
        if counts.get(category, 0) >= MAX_ITEMS_PER_CATEGORY:
            continue
        counts[category] = counts.get(category, 0) + 1
        items.append(item)
    return {"updated_at": updated_at, "items": items, "sources": statuses}


def write_payload(payload, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=destination.parent, prefix=".news-", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def fetch_feed(url):
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, text/xml"})
    with urlopen(request, timeout=20) as response:
        return response.read()


def refresh(destination):
    results = []
    for source in SOURCES:
        try:
            items = parse_feed(fetch_feed(source["url"]), source["name"], source["category"])
            results.append({**source, "ok": True, "items": items})
        except Exception as error:  # one publisher must not stop the other feeds
            results.append({**source, "ok": False, "items": [], "error": str(error)[:120]})

    payload = build_payload(results, datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    destination = Path(destination)
    if payload["items"] or not destination.exists():
        write_payload(payload, destination)
        return payload

    # ponytail: keep the last good snapshot when every upstream is unavailable; add a cache layer only if freshness needs it.
    return json.loads(destination.read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", default=str(Path(__file__).with_name("data") / "news.json"))
    args = parser.parse_args(argv)
    payload = refresh(args.output)
    print(f"updated_at={payload.get('updated_at', '')} items={len(payload.get('items', []))}")


if __name__ == "__main__":
    main()
