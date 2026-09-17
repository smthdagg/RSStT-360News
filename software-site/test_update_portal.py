import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from update_portal import build_payload, parse_feed, refresh, write_payload


RSS_SAMPLE = b'''<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title><![CDATA[AI &amp; useful tools]]></title>
    <link>https://example.com/one</link>
    <description><![CDATA[<p>A short <b>brief</b> about AI.</p>]]></description>
    <pubDate>Tue, 15 Sep 2026 01:02:03 GMT</pubDate>
  </item>
</channel></rss>'''

ATOM_SAMPLE = b'''<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Cloud infrastructure update</title>
    <link href="https://example.com/two" />
    <summary>Reliable systems, measured simply.</summary>
    <updated>2026-09-14T12:00:00Z</updated>
  </entry>
</feed>'''


class PortalUpdaterTests(unittest.TestCase):
    def test_parse_rss_removes_markup_and_normalizes_date(self):
        items = parse_feed(RSS_SAMPLE, "Example", "ai")

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "AI & useful tools")
        self.assertEqual(items[0]["summary"], "A short brief about AI.")
        self.assertEqual(items[0]["published"], "2026-09-15T01:02:03Z")
        self.assertEqual(items[0]["source"], "Example")

    def test_parse_atom_uses_link_attribute(self):
        items = parse_feed(ATOM_SAMPLE, "Example", "tech")

        self.assertEqual(items[0]["link"], "https://example.com/two")
        self.assertEqual(items[0]["category"], "tech")

    def test_build_payload_deduplicates_and_keeps_latest_categories(self):
        first = parse_feed(RSS_SAMPLE, "A", "ai")[0]
        duplicate = dict(first, source="B", title="Same story")
        second = parse_feed(ATOM_SAMPLE, "C", "tech")[0]

        payload = build_payload(
            [
                {"name": "A", "category": "ai", "url": "https://a", "ok": True, "items": [first]},
                {"name": "B", "category": "ai", "url": "https://b", "ok": True, "items": [duplicate]},
                {"name": "C", "category": "tech", "url": "https://c", "ok": True, "items": [second]},
            ],
            "2026-09-15T02:00:00Z",
        )

        self.assertEqual(payload["updated_at"], "2026-09-15T02:00:00Z")
        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual({item["category"] for item in payload["items"]}, {"ai", "tech"})

    def test_write_payload_replaces_file_with_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "data" / "news.json"
            write_payload({"items": [{"title": "ok"}]}, destination)

            self.assertEqual(json.loads(destination.read_text())["items"][0]["title"], "ok")
            self.assertFalse(list(destination.parent.glob("*.tmp")))

    def test_refresh_keeps_previous_snapshot_when_all_sources_fail(self):
        previous = {"updated_at": "old", "items": [{"title": "keep"}], "sources": []}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "news.json"
            write_payload(previous, destination)
            with patch("update_portal.fetch_feed", side_effect=OSError("offline")):
                result = refresh(destination)

            self.assertEqual(result, previous)
            self.assertEqual(json.loads(destination.read_text()), previous)

    def test_refresh_writes_a_successful_source_snapshot(self):
        source = {"name": "Example", "category": "ai", "url": "https://example.com/feed"}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "news.json"
            with patch("update_portal.SOURCES", (source,)), patch("update_portal.fetch_feed", return_value=RSS_SAMPLE):
                result = refresh(destination)

            self.assertEqual(len(result["items"]), 1)
            self.assertTrue(json.loads(destination.read_text())["sources"][0]["ok"])


if __name__ == "__main__":
    unittest.main()
