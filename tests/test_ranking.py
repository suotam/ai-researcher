from datetime import datetime, timedelta, timezone

from src.processing.ranking import (
    recency_points,
    score_article,
    select_top,
)

TOPICS = {
    "ai": {
        "importance": 1.0,
        "entities": ["OpenAI", "NVIDIA"],
        "keywords": {
            "high": ["frontier model", "quantization"],
            "medium": ["inference"],
        },
        "negative": ["webinar"],
    },
    "markets": {
        "importance": 1.0,
        "entities": ["Fed"],
        "keywords": {"high": ["rate cut"], "medium": ["gold"]},
        "negative": [],
    },
}

NOW = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)


def _score(title, summary="", category="ai", priority=5, source_type="rss",
           published=NOW - timedelta(hours=2)):
    return score_article(
        title=title, summary=summary, category=category,
        source_priority=priority, source_type=source_type,
        published_at=published, topics=TOPICS, now=NOW,
    )


def test_score_in_range():
    assert 0 <= _score("boring item") <= 100
    assert 0 <= _score("OpenAI frontier model quantization inference "
                       "announcement", priority=10) <= 100


def test_keyword_match_beats_no_match():
    assert _score("OpenAI announces new frontier model") > _score(
        "Company publishes quarterly newsletter")


def test_recent_beats_old():
    fresh = _score("OpenAI frontier model", published=NOW - timedelta(hours=1))
    old = _score("OpenAI frontier model", published=NOW - timedelta(days=5))
    assert fresh > old


def test_high_priority_source_beats_low():
    assert _score("Some update", priority=10) > _score("Some update", priority=2)


def test_arxiv_penalty():
    assert _score("Quantization paper", source_type="arxiv") < _score(
        "Quantization paper", source_type="rss")


def test_negative_keyword_penalty():
    assert _score("Join our webinar about inference") < _score(
        "New inference engine released")


def test_recency_points_unknown_date_is_neutral():
    assert recency_points(None, NOW) == 5
    assert recency_points("not a date", NOW) == 5


def test_select_top_respects_limits():
    rows = [
        {"relevance_score": 90, "category": "ai"},
        {"relevance_score": 85, "category": "ai"},
        {"relevance_score": 80, "category": "ai"},
        {"relevance_score": 70, "category": "markets"},
        {"relevance_score": 10, "category": "markets"},  # below min_score
    ]
    picked = select_top(rows, top_items=3, min_score=25, max_per_category=2)
    assert len(picked) == 3
    assert sum(1 for r in picked if r["category"] == "ai") == 2
    assert all(r["relevance_score"] >= 25 for r in picked)
