import pytest

from src import db
from src.config import Source
from src.processing.normalize import canonicalize_url, content_hash


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def _insert(conn, title="Title", url="https://example.com/a", **kw):
    defaults = dict(
        source_id=None,
        title=title,
        url=url,
        canonical_url=canonicalize_url(url),
        published_at=None,
        fetched_at=db.utcnow_iso(),
        author="",
        summary="",
        raw_text="",
        category="ai",
        content_hash=content_hash(title),
    )
    defaults.update(kw)
    return db.insert_article(conn, **defaults)


def test_insert_and_read_article(conn):
    article_id = _insert(conn, title="Hello", url="https://example.com/hello")
    row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    assert row["title"] == "Hello"
    assert row["status"] == "new"


def test_sync_sources_upserts_and_sets_ids(conn):
    sources = [Source(name="A", type="rss", category="ai", url="https://a.com/f")]
    db.sync_sources(conn, sources)
    assert sources[0].id is not None
    # second sync updates instead of duplicating
    sources[0].priority = 9
    db.sync_sources(conn, sources)
    rows = conn.execute("SELECT * FROM sources").fetchall()
    assert len(rows) == 1
    assert rows[0]["priority"] == 9


def test_candidates_and_mark_briefed(conn):
    aid = _insert(conn)
    db.update_score(conn, aid, 80)
    rows = db.candidates_for_briefing(conn, since_iso="2000-01-01T00:00:00+00:00")
    assert [r["id"] for r in rows] == [aid]
    db.mark_briefed(conn, [aid])
    rows = db.candidates_for_briefing(conn, since_iso="2000-01-01T00:00:00+00:00")
    assert rows == []


def test_briefing_insert_with_items(conn):
    a1 = _insert(conn, url="https://example.com/1", title="One")
    a2 = _insert(conn, url="https://example.com/2", title="Two")
    briefing_id = db.insert_briefing(
        conn, period_start="2026-08-13T06:00:00+00:00",
        period_end="2026-08-14T06:00:00+00:00",
        file_path="output/2026-08-14-morning-brief.md",
        article_ids=[a2, a1],
    )
    items = conn.execute(
        "SELECT * FROM briefing_items WHERE briefing_id = ? ORDER BY rank",
        (briefing_id,)).fetchall()
    assert [(i["article_id"], i["rank"]) for i in items] == [(a2, 1), (a1, 2)]


def test_learning_topics_roundtrip(conn):
    db.record_learning_topic(conn, "speculative decoding")
    db.record_learning_topic(conn, "speculative decoding")
    row = conn.execute("SELECT * FROM learning_topics").fetchone()
    assert row["times_used"] == 2
    assert db.recent_learning_topics(conn, days=1) == ["speculative decoding"]
