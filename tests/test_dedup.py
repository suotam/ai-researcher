import pytest

from src import db
from src.processing.deduplicate import Deduplicator
from src.processing.normalize import canonicalize_url, content_hash


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


def _insert(conn, title, url):
    return db.insert_article(
        conn,
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


def test_exact_url_duplicate(conn):
    aid = _insert(conn, "Some title", "https://example.com/a")
    dedup = Deduplicator(conn)
    match = dedup.check(title="Different title entirely, no overlap",
                        url="https://example.com/a")
    assert match.reason == "exact_url"
    assert match.article_id == aid


def test_canonical_url_duplicate(conn):
    aid = _insert(conn, "Some title", "https://example.com/a")
    dedup = Deduplicator(conn)
    match = dedup.check(title="Different title entirely, no overlap",
                        url="http://www.example.com/a/?utm_source=feed")
    assert match.reason == "canonical_url"
    assert match.article_id == aid


def test_content_hash_duplicate(conn):
    aid = _insert(conn, "OpenAI launches GPT-5", "https://example.com/a")
    dedup = Deduplicator(conn)
    match = dedup.check(title="OpenAI Launches GPT-5!", url="https://other.com/b")
    assert match.reason == "content_hash"
    assert match.article_id == aid


def test_fuzzy_title_duplicate(conn):
    aid = _insert(conn, "OpenAI launches new GPT-5 frontier model",
                  "https://example.com/a")
    dedup = Deduplicator(conn)
    match = dedup.check(title="New GPT-5 frontier model launched by OpenAI",
                        url="https://other.com/b")
    assert match.reason == "fuzzy_title"
    assert match.article_id == aid


def test_new_item_passes(conn):
    _insert(conn, "OpenAI launches GPT-5", "https://example.com/a")
    dedup = Deduplicator(conn)
    assert dedup.check(title="Fed cuts interest rates by 25 basis points",
                       url="https://other.com/fed") is None


def test_intra_run_duplicate_via_register(conn):
    dedup = Deduplicator(conn)
    assert dedup.check(title="Nvidia announces new Blackwell GPU",
                       url="https://a.com/1") is None
    dedup.register("Nvidia announces new Blackwell GPU", article_id=42)
    match = dedup.check(title="New Blackwell GPU announced by Nvidia",
                        url="https://b.com/2")
    assert match.reason == "fuzzy_title"
    assert match.article_id == 42


def test_coverage_tracking(conn):
    aid = _insert(conn, "OpenAI launches GPT-5", "https://example.com/a")
    db.record_duplicate(conn, article_id=aid, source_name="The Verge AI",
                        title="OpenAI Launches GPT-5", url="https://verge.com/x")
    db.record_duplicate(conn, article_id=aid, source_name="TechCrunch AI",
                        title="GPT-5 is here", url="https://tc.com/y")
    # same URL again must not double-count
    db.record_duplicate(conn, article_id=aid, source_name="The Verge AI",
                        title="OpenAI Launches GPT-5", url="https://verge.com/x")
    row = conn.execute("SELECT duplicate_count FROM articles WHERE id = ?",
                       (aid,)).fetchone()
    assert row["duplicate_count"] == 2
    assert sorted(db.coverage_sources(conn, aid)) == ["TechCrunch AI", "The Verge AI"]
