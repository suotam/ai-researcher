from datetime import date
from pathlib import Path

from src.reporting.briefing import (
    briefing_path,
    extract_learning_topic,
    render_brief,
    render_fallback_brief,
)

ARTICLES = [
    {
        "id": 12,
        "ref_id": "ARTICLE_12",
        "title": "OpenAI launches GPT-5",
        "url": "https://example.com/gpt5",
        "source_name": "OpenAI News",
        "category": "ai",
        "published_at": "2026-08-14T06:00:00+00:00",
        "summary": "Big launch.",
        "relevance_score": 92,
    },
]


def test_briefing_path_format():
    path = briefing_path("output", date(2026, 8, 14))
    assert path == Path("output") / "2026-08-14-morning-brief.md"


def test_extract_learning_topic():
    text = "## Dnes se nauč\nLEARNING_TOPIC: speculative decoding\nText..."
    assert extract_learning_topic(text) == "speculative decoding"
    assert extract_learning_topic("no topic here") is None


def test_render_brief_links_references_and_sources():
    llm_output = "## AI\nVelká událost [ARTICLE_12]."
    result = render_brief(llm_output, ARTICLES, date(2026, 8, 14))
    assert "[[12]](https://example.com/gpt5)" in result
    assert "## Sources" in result
    assert "https://example.com/gpt5" in result
    assert result.startswith("# Morning Brief — 2026-08-14")


def test_render_brief_replaces_learning_topic_marker():
    llm_output = "LEARNING_TOPIC: yield curve\nVysvětlení..."
    result = render_brief(llm_output, ARTICLES, date(2026, 8, 14))
    assert "LEARNING_TOPIC:" not in result
    assert "yield curve" in result


def test_fallback_brief_contains_items_and_sources():
    result = render_fallback_brief(ARTICLES, date(2026, 8, 14), "--no-llm")
    assert "OpenAI launches GPT-5" in result
    assert "https://example.com/gpt5" in result
    assert "## Sources" in result


def test_fallback_brief_empty_selection():
    result = render_fallback_brief([], date(2026, 8, 14), "test")
    assert "Nic významného" in result
