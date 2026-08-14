"""Tests for v2 features: coverage boost, feedback, rerank parsing,
HTML collector, calendar, weekly path, source health."""

from datetime import date, timedelta

import pytest

from src import db
from src.collectors.html import parse_listing
from src.config import Config, Source
from src.main import upcoming_calendar_events
from src.processing.normalize import canonicalize_url, content_hash
from src.processing.ranking import coverage_points, score_article
from src.processing.rerank import _parse_ids
from src.reporting.briefing import health_section, weekly_path


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def _insert(conn, title="Title", url="https://example.com/a", source_id=None):
    return db.insert_article(
        conn, source_id=source_id, title=title, url=url,
        canonical_url=canonicalize_url(url), published_at=None,
        fetched_at=db.utcnow_iso(), author="", summary="", raw_text="",
        category="ai", content_hash=content_hash(title),
    )


# ------------------------------------------------------------------ coverage

def test_coverage_points_capped():
    assert coverage_points(0) == 0
    assert coverage_points(1) == 4
    assert coverage_points(10) == 12  # cap


def test_coverage_boosts_score():
    kwargs = dict(
        title="Some event", summary="", category="ai", source_priority=5,
        source_type="rss", published_at=None, topics={},
    )
    assert score_article(**kwargs, duplicate_count=3) > score_article(**kwargs)


# ------------------------------------------------------------------ feedback

def test_feedback_roundtrip_and_adjustment(conn):
    sources = [Source(name="S1", type="rss", category="ai", url="https://s1/f")]
    db.sync_sources(conn, sources)
    sid = sources[0].id
    aid = _insert(conn, source_id=sid)
    assert db.add_feedback(conn, article_id=aid, rating=1)
    assert db.add_feedback(conn, article_id=aid, rating=1)
    assert not db.add_feedback(conn, article_id=99999, rating=1)
    adjustments = db.source_feedback_adjustments(conn)
    assert adjustments[sid] > 0

    stats = db.feedback_stats(conn)
    assert stats[0]["n"] == 2
    assert stats[0]["ups"] == 2


def test_feedback_adjustment_affects_score():
    kwargs = dict(
        title="Some event", summary="", category="ai", source_priority=5,
        source_type="rss", published_at=None, topics={},
    )
    assert score_article(**kwargs, feedback_adjust=10) > score_article(**kwargs)
    assert score_article(**kwargs, feedback_adjust=-10) < score_article(**kwargs)


# ------------------------------------------------------------------- rerank

def test_rerank_parse_valid():
    assert _parse_ids('{"selected": [3, 1, 2]}') == [3, 1, 2]
    assert _parse_ids('Some preamble {"selected": [7]} trailing') == [7]


def test_rerank_parse_invalid():
    assert _parse_ids("no json here") is None
    assert _parse_ids('{"selected": "not a list"}') is None
    assert _parse_ids('{"other": [1]}') is None


# ------------------------------------------------------------- html collector

HTML_SAMPLE = """
<html><body>
  <nav><a href="/news">News</a></nav>
  <a href="/news/model-launch">Announcing our new frontier model for everyone</a>
  <a href="/news/model-launch">Announcing our new frontier model for everyone</a>
  <a href="/news/tiny">short</a>
  <a href="https://other.com/x">Unrelated external link with a long title here</a>
</body></html>
"""


def _html_source(selector="a[href^='/news/']"):
    return Source(name="Test HTML", type="html", category="ai",
                  url="https://example.com/news",
                  extra={"item_selector": selector})


def test_parse_listing_extracts_and_dedups():
    items = parse_listing(HTML_SAMPLE, _html_source(), max_items=10)
    assert len(items) == 1  # dup link collapsed, short title dropped, nav dropped
    assert items[0].url == "https://example.com/news/model-launch"
    assert items[0].title.startswith("Announcing our new frontier model")
    assert items[0].published_at is None


def test_parse_listing_missing_selector():
    source = Source(name="X", type="html", category="ai",
                    url="https://example.com", extra={})
    assert parse_listing(HTML_SAMPLE, source, 10) == []


# ------------------------------------------------------------------ calendar

def test_upcoming_calendar_events_filters_window():
    today = date(2026, 8, 14)
    cfg = Config(settings={}, sources=[], topics={}, calendar=[
        {"date": "2026-08-10", "title": "past", "category": "markets", "note": ""},
        {"date": "2026-08-16", "title": "soon", "category": "markets", "note": ""},
        {"date": "2026-09-30", "title": "far", "category": "markets", "note": ""},
        {"date": "invalid", "title": "bad", "category": "", "note": ""},
    ])
    events = upcoming_calendar_events(cfg, today)
    assert [e["title"] for e in events] == ["soon"]


# ------------------------------------------------------------------- weekly

def test_weekly_path_format():
    assert weekly_path("output", date(2026, 8, 16)).name == "2026-08-16-weekly-digest.md"


def test_top_articles_for_period(conn):
    a1 = _insert(conn, "One", "https://e.com/1")
    a2 = _insert(conn, "Two", "https://e.com/2")
    db.update_score(conn, a1, 40)
    db.update_score(conn, a2, 90)
    db.mark_briefed(conn, [a2])  # briefed articles still count for weekly
    rows = db.top_articles_for_period(conn, since_iso="2000-01-01T00:00:00+00:00",
                                      limit=10)
    assert [r["id"] for r in rows] == [a2, a1]


# ------------------------------------------------------------- source health

def test_source_health_consecutive_and_reset(conn):
    sources = [Source(name="S1", type="rss", category="ai", url="https://s1/f")]
    db.sync_sources(conn, sources)
    sid = sources[0].id
    assert db.update_source_health(conn, sid, "error") == 1
    assert db.update_source_health(conn, sid, "empty") == 2
    assert db.update_source_health(conn, sid, "error") == 3
    unhealthy = db.unhealthy_sources(conn, threshold=3)
    assert len(unhealthy) == 1
    assert unhealthy[0]["name"] == "S1"
    assert db.update_source_health(conn, sid, "ok") == 0
    assert db.unhealthy_sources(conn, threshold=3) == []


def test_health_section_rendering():
    text = health_section([{"name": "S1", "last_result": "error", "consecutive_bad": 4}])
    assert "S1" in text and "4×" in text
    assert health_section([]) == ""
