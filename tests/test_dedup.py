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
    _insert(conn, "Some title", "https://example.com/a")
    dedup = Deduplicator(conn)
    assert dedup.check(title="Different title entirely, no overlap",
                       url="https://example.com/a") == "exact_url"


def test_canonical_url_duplicate(conn):
    _insert(conn, "Some title", "https://example.com/a")
    dedup = Deduplicator(conn)
    assert dedup.check(title="Different title entirely, no overlap",
                       url="http://www.example.com/a/?utm_source=feed") == "canonical_url"


def test_content_hash_duplicate(conn):
    _insert(conn, "OpenAI launches GPT-5", "https://example.com/a")
    dedup = Deduplicator(conn)
    assert dedup.check(title="OpenAI Launches GPT-5!",
                       url="https://other.com/b") == "content_hash"


def test_fuzzy_title_duplicate(conn):
    _insert(conn, "OpenAI launches new GPT-5 frontier model",
            "https://example.com/a")
    dedup = Deduplicator(conn)
    assert dedup.check(title="New GPT-5 frontier model launched by OpenAI",
                       url="https://other.com/b") == "fuzzy_title"


def test_new_item_passes(conn):
    _insert(conn, "OpenAI launches GPT-5", "https://example.com/a")
    dedup = Deduplicator(conn)
    assert dedup.check(title="Fed cuts interest rates by 25 basis points",
                       url="https://other.com/fed") is None


def test_intra_run_duplicate_via_register(conn):
    dedup = Deduplicator(conn)
    assert dedup.check(title="Nvidia announces new Blackwell GPU",
                       url="https://a.com/1") is None
    dedup.register("Nvidia announces new Blackwell GPU")
    assert dedup.check(title="New Blackwell GPU announced by Nvidia",
                       url="https://b.com/2") == "fuzzy_title"
