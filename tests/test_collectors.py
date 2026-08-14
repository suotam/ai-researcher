"""Collector parsing tests — no live network, feeds are inline fixtures."""

from src.collectors.github import _parse_repo, parse_releases
from src.collectors.rss import parse_feed_text
from src.config import Source

RSS_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>OpenAI launches GPT-5</title>
      <link>https://example.com/gpt5</link>
      <description>&lt;p&gt;A &lt;b&gt;big&lt;/b&gt; launch.&lt;/p&gt;</description>
      <pubDate>Fri, 14 Aug 2026 06:00:00 GMT</pubDate>
    </item>
    <item>
      <title></title>
      <link>https://example.com/no-title</link>
    </item>
  </channel>
</rss>"""


def _source(type_="rss"):
    return Source(name="Test", type=type_, category="ai", url="https://x.test/feed")


def test_parse_feed_text_basic():
    items = parse_feed_text(RSS_SAMPLE, _source(), max_items=10)
    assert len(items) == 1  # entry without title is dropped
    item = items[0]
    assert item.title == "OpenAI launches GPT-5"
    assert item.url == "https://example.com/gpt5"
    assert item.summary == "A big launch."
    assert item.published_at is not None
    assert item.published_at.year == 2026


def test_parse_feed_text_malformed():
    assert parse_feed_text("this is not xml at all", _source(), 10) == []


def test_parse_repo_variants():
    assert _parse_repo("ggml-org/llama.cpp") == "ggml-org/llama.cpp"
    assert _parse_repo("https://github.com/ollama/ollama/") == "ollama/ollama"
    assert _parse_repo("") is None
    assert _parse_repo("justonepart") is None


def test_parse_releases():
    payload = [
        {
            "tag_name": "b1234",
            "name": "b1234",
            "html_url": "https://github.com/ggml-org/llama.cpp/releases/tag/b1234",
            "published_at": "2026-08-14T05:00:00Z",
            "body": "Speculative decoding improvements",
            "author": {"login": "ggerganov"},
        },
        {"tag_name": "draft1", "draft": True},
    ]
    items = parse_releases(payload, _source("github"), "ggml-org/llama.cpp", 10)
    assert len(items) == 1
    assert items[0].title == "ggml-org/llama.cpp release: b1234"
    assert "Speculative decoding" in items[0].raw_text
    assert items[0].published_at.year == 2026
