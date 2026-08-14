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

-- Other outlets covering an already-stored article (event). Coverage breadth
-- is an importance signal: 5 outlets writing about the same thing matters.
CREATE TABLE IF NOT EXISTS article_duplicates (
    article_id  INTEGER NOT NULL REFERENCES articles(id),
    source_name TEXT NOT NULL,
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    seen_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dups_article ON article_duplicates(article_id);

-- User feedback on briefed articles (+1 / -1), used to nudge source priorities.
CREATE TABLE IF NOT EXISTS feedback (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL REFERENCES articles(id),
    rating     INTEGER NOT NULL,           -- +1 or -1
    note       TEXT DEFAULT '',
    created_at TEXT NOT NULL
);

-- Per-source fetch health; consecutive_bad >= 3 surfaces in the brief.
CREATE TABLE IF NOT EXISTS source_health (
    source_id       INTEGER PRIMARY KEY REFERENCES sources(id),
    last_run        TEXT NOT NULL,
    last_result     TEXT NOT NULL,          -- ok | empty | error
    consecutive_bad INTEGER NOT NULL DEFAULT 0
);
"""

MIGRATIONS = [
    # (table, column, DDL) — applied only when the column is missing
    ("articles", "duplicate_count",
     "ALTER TABLE articles ADD COLUMN duplicate_count INTEGER NOT NULL DEFAULT 0"),
]


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
    for table, column, ddl in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(ddl)
    conn.commit()
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
                   content_hash: str) -> tuple[str, int] | None:
    """Return (dedup level, matching article id), or None if the article is new."""
    row = conn.execute("SELECT id FROM articles WHERE url = ?", (url,)).fetchone()
    if row:
        return "exact_url", row["id"]
    row = conn.execute(
        "SELECT id FROM articles WHERE canonical_url = ?", (canonical_url,)
    ).fetchone()
    if row:
        return "canonical_url", row["id"]
    row = conn.execute(
        "SELECT id FROM articles WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    if row:
        return "content_hash", row["id"]
    return None


def recent_titles(conn: sqlite3.Connection, days: int) -> list[tuple[int, str]]:
    """(article_id, title) pairs from the recent window, for fuzzy matching."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = conn.execute(
        "SELECT id, title FROM articles WHERE fetched_at >= ?", (cutoff,)
    ).fetchall()
    return [(r["id"], r["title"]) for r in rows]


def record_duplicate(conn: sqlite3.Connection, *, article_id: int,
                     source_name: str, title: str, url: str) -> None:
    """Log another outlet covering an existing article; bump its coverage count.

    Ignores repeats of the same URL (same feed seen again on a later run) so
    coverage counts stay meaningful.
    """
    already = conn.execute(
        "SELECT 1 FROM article_duplicates WHERE article_id = ? AND url = ?",
        (article_id, url),
    ).fetchone()
    if already:
        return
    conn.execute(
        "INSERT INTO article_duplicates (article_id, source_name, title, url, seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (article_id, source_name, title, url, utcnow_iso()),
    )
    conn.execute(
        "UPDATE articles SET duplicate_count = duplicate_count + 1 WHERE id = ?",
        (article_id,),
    )


def coverage_sources(conn: sqlite3.Connection, article_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT source_name FROM article_duplicates WHERE article_id = ?",
        (article_id,),
    ).fetchall()
    return [r["source_name"] for r in rows]


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


# ----------------------------------------------------------------- feedback

def add_feedback(conn: sqlite3.Connection, *, article_id: int, rating: int,
                 note: str = "") -> bool:
    """Record a +1/-1 rating. Returns False if the article does not exist."""
    if not conn.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone():
        return False
    conn.execute(
        "INSERT INTO feedback (article_id, rating, note, created_at) VALUES (?, ?, ?, ?)",
        (article_id, max(-1, min(1, rating)), note, utcnow_iso()),
    )
    return True


def source_feedback_adjustments(conn: sqlite3.Connection, *,
                                max_adjust: int = 10) -> dict[int, int]:
    """Per-source score adjustment learned from feedback.

    avg rating in [-1, 1] scaled by number of ratings (capped at 5) and
    clamped to +-max_adjust. Few ratings -> small nudge; consistent ratings
    -> up to the cap. Keyed by source_id.
    """
    rows = conn.execute(
        """
        SELECT a.source_id AS source_id, AVG(f.rating) AS avg_rating,
               COUNT(*) AS n
        FROM feedback f JOIN articles a ON a.id = f.article_id
        WHERE a.source_id IS NOT NULL
        GROUP BY a.source_id
        """
    ).fetchall()
    adjustments: dict[int, int] = {}
    for r in rows:
        weight = min(r["n"], 5) / 5.0
        adjustments[r["source_id"]] = round(
            max(-max_adjust, min(max_adjust, r["avg_rating"] * max_adjust * weight)))
    return adjustments


def feedback_stats(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.name AS source_name, COUNT(*) AS n, AVG(f.rating) AS avg_rating,
               SUM(CASE WHEN f.rating > 0 THEN 1 ELSE 0 END) AS ups,
               SUM(CASE WHEN f.rating < 0 THEN 1 ELSE 0 END) AS downs
        FROM feedback f
        JOIN articles a ON a.id = f.article_id
        LEFT JOIN sources s ON s.id = a.source_id
        GROUP BY a.source_id ORDER BY n DESC
        """
    ).fetchall()


# ------------------------------------------------------------- source health

def update_source_health(conn: sqlite3.Connection, source_id: int,
                         result: str) -> int:
    """Record a fetch result ('ok' | 'empty' | 'error'). Returns the new
    consecutive_bad counter."""
    row = conn.execute(
        "SELECT consecutive_bad FROM source_health WHERE source_id = ?",
        (source_id,)).fetchone()
    bad = (row["consecutive_bad"] if row else 0)
    bad = 0 if result == "ok" else bad + 1
    conn.execute(
        """
        INSERT INTO source_health (source_id, last_run, last_result, consecutive_bad)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            last_run = excluded.last_run,
            last_result = excluded.last_result,
            consecutive_bad = excluded.consecutive_bad
        """,
        (source_id, utcnow_iso(), result, bad),
    )
    return bad


def unhealthy_sources(conn: sqlite3.Connection, *, threshold: int = 3) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.name AS name, h.last_result AS last_result,
               h.consecutive_bad AS consecutive_bad
        FROM source_health h JOIN sources s ON s.id = h.source_id
        WHERE h.consecutive_bad >= ? AND s.enabled = 1
        ORDER BY h.consecutive_bad DESC
        """,
        (threshold,),
    ).fetchall()


# ------------------------------------------------------------------- weekly

def top_articles_for_period(conn: sqlite3.Connection, *, since_iso: str,
                            limit: int) -> list[sqlite3.Row]:
    """Best-scored articles of the period regardless of briefed status —
    used by the weekly digest."""
    return conn.execute(
        """
        SELECT a.*, s.name AS source_name, s.priority AS source_priority,
               s.type AS source_type
        FROM articles a LEFT JOIN sources s ON s.id = a.source_id
        WHERE a.fetched_at >= ? AND a.status != 'duplicate'
        ORDER BY a.relevance_score DESC, a.duplicate_count DESC
        LIMIT ?
        """,
        (since_iso, limit),
    ).fetchall()


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
