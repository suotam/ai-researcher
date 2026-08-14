"""SQLite storage layer.

Plain sqlite3, no ORM. All timestamps are stored as UTC ISO-8601 strings.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    type     TEXT NOT NULL,
    category TEXT NOT NULL,
    url      TEXT NOT NULL,
    enabled  INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 5
);

CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       INTEGER REFERENCES sources(id),
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,
    canonical_url   TEXT NOT NULL,
    published_at    TEXT,
    fetched_at      TEXT NOT NULL,
    author          TEXT DEFAULT '',
    summary         TEXT DEFAULT '',
    raw_text        TEXT DEFAULT '',
    category        TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    relevance_score INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'new'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url ON articles(url);
CREATE INDEX IF NOT EXISTS idx_articles_canonical ON articles(canonical_url);
CREATE INDEX IF NOT EXISTS idx_articles_hash ON articles(content_hash);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status);

CREATE TABLE IF NOT EXISTS briefings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    file_path    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS briefing_items (
    briefing_id INTEGER NOT NULL REFERENCES briefings(id),
    article_id  INTEGER NOT NULL REFERENCES articles(id),
    rank        INTEGER NOT NULL,
    PRIMARY KEY (briefing_id, article_id)
);

CREATE TABLE IF NOT EXISTS learning_topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    topic      TEXT NOT NULL UNIQUE,
    first_seen TEXT NOT NULL,
    last_used  TEXT NOT NULL,
    times_used INTEGER NOT NULL DEFAULT 1
);
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


# ----------------------------------------------------------------- sources

def sync_sources(conn: sqlite3.Connection, sources) -> None:
    """Mirror the YAML source list into the sources table (upsert by name).

    Fills each Source dataclass with its DB id.
    """
    for src in sources:
        conn.execute(
            """
            INSERT INTO sources (name, type, category, url, enabled, priority)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                type = excluded.type,
                category = excluded.category,
                url = excluded.url,
                enabled = excluded.enabled,
                priority = excluded.priority
            """,
            (src.name, src.type, src.category, src.url, int(src.enabled), src.priority),
        )
        row = conn.execute("SELECT id FROM sources WHERE name = ?", (src.name,)).fetchone()
        src.id = row["id"] if row else None
    conn.commit()


# ----------------------------------------------------------------- articles

def article_exists(conn: sqlite3.Connection, *, url: str, canonical_url: str,
                   content_hash: str) -> str | None:
    """Return the dedup level that matched, or None if the article is new."""
    if conn.execute("SELECT 1 FROM articles WHERE url = ?", (url,)).fetchone():
        return "exact_url"
    if conn.execute(
        "SELECT 1 FROM articles WHERE canonical_url = ?", (canonical_url,)
    ).fetchone():
        return "canonical_url"
    if conn.execute(
        "SELECT 1 FROM articles WHERE content_hash = ?", (content_hash,)
    ).fetchone():
        return "content_hash"
    return None


def recent_titles(conn: sqlite3.Connection, days: int) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT title FROM articles WHERE fetched_at >= ?", (cutoff,)
    ).fetchall()
    return [r["title"] for r in rows]


def insert_article(conn: sqlite3.Connection, *, source_id: int | None, title: str,
                   url: str, canonical_url: str, published_at: str | None,
                   fetched_at: str, author: str, summary: str, raw_text: str,
                   category: str, content_hash: str,
                   relevance_score: int = 0, status: str = "new") -> int:
    cur = conn.execute(
        """
        INSERT INTO articles (source_id, title, url, canonical_url, published_at,
                              fetched_at, author, summary, raw_text, category,
                              content_hash, relevance_score, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (source_id, title, url, canonical_url, published_at, fetched_at,
         author, summary, raw_text, category, content_hash, relevance_score, status),
    )
    return int(cur.lastrowid)


def update_score(conn: sqlite3.Connection, article_id: int, score: int) -> None:
    conn.execute(
        "UPDATE articles SET relevance_score = ? WHERE id = ?", (score, article_id)
    )


def candidates_for_briefing(conn: sqlite3.Connection, *, since_iso: str,
                            categories: list[str] | None = None) -> list[sqlite3.Row]:
    """New articles fetched since the cutoff, best score first."""
    sql = (
        "SELECT a.*, s.name AS source_name, s.priority AS source_priority, "
        "s.type AS source_type "
        "FROM articles a LEFT JOIN sources s ON s.id = a.source_id "
        "WHERE a.status = 'new' AND a.fetched_at >= ? "
        # Items with a known publish date must also fall inside the window —
        # otherwise the first run of a new source floods the brief with the
        # feed's whole back-catalog. Unknown dates are kept (recency scoring
        # already treats them as neutral-low).
        "AND (a.published_at IS NULL OR a.published_at >= ?)"
    )
    params: list = [since_iso, since_iso]
    if categories:
        sql += " AND a.category IN (%s)" % ",".join("?" * len(categories))
        params.extend(categories)
    sql += " ORDER BY a.relevance_score DESC, a.published_at DESC"
    return conn.execute(sql, params).fetchall()


def mark_briefed(conn: sqlite3.Connection, article_ids: list[int]) -> None:
    conn.executemany(
        "UPDATE articles SET status = 'briefed' WHERE id = ?",
        [(i,) for i in article_ids],
    )


# ----------------------------------------------------------------- briefings

def insert_briefing(conn: sqlite3.Connection, *, period_start: str, period_end: str,
                    file_path: str, article_ids: list[int]) -> int:
    cur = conn.execute(
        "INSERT INTO briefings (created_at, period_start, period_end, file_path) "
        "VALUES (?, ?, ?, ?)",
        (utcnow_iso(), period_start, period_end, file_path),
    )
    briefing_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO briefing_items (briefing_id, article_id, rank) VALUES (?, ?, ?)",
        [(briefing_id, aid, rank) for rank, aid in enumerate(article_ids, start=1)],
    )
    return briefing_id


# ------------------------------------------------------------ learning topics

def recent_learning_topics(conn: sqlite3.Connection, days: int) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT topic FROM learning_topics WHERE last_used >= ? ORDER BY last_used DESC",
        (cutoff,),
    ).fetchall()
    return [r["topic"] for r in rows]


def record_learning_topic(conn: sqlite3.Connection, topic: str) -> None:
    topic = topic.strip()
    if not topic:
        return
    now = utcnow_iso()
    conn.execute(
        """
        INSERT INTO learning_topics (topic, first_seen, last_used, times_used)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(topic) DO UPDATE SET
            last_used = excluded.last_used,
            times_used = times_used + 1
        """,
        (topic, now, now),
    )
