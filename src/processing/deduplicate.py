"""Multi-level deduplication.

Levels (cheapest first):
  1. exact URL          (DB unique index)
  2. canonicalized URL  (DB lookup)
  3. content hash       (normalized title hash, DB lookup)
  4. fuzzy title match  (rapidfuzz against recent titles)

Embedding-based clustering can later be added as level 5 behind the same
``Deduplicator.check()`` interface.
"""

from __future__ import annotations

import logging
import sqlite3

from rapidfuzz import fuzz

from .. import db
from .normalize import canonicalize_url, content_hash, normalize_title

log = logging.getLogger(__name__)


class Deduplicator:
    def __init__(self, conn: sqlite3.Connection, *, fuzzy_threshold: int = 88,
                 window_days: int = 7):
        self.conn = conn
        self.fuzzy_threshold = fuzzy_threshold
        # Normalized titles from the recent window; also grows with titles
        # accepted during this run so intra-run duplicates are caught too.
        self._recent_titles: list[str] = [
            normalize_title(t) for t in db.recent_titles(conn, window_days)
        ]

    def check(self, *, title: str, url: str) -> str | None:
        """Return dedup reason ('exact_url' | 'canonical_url' | 'content_hash'
        | 'fuzzy_title') or None if the item is new."""
        canonical = canonicalize_url(url)
        chash = content_hash(title)
        reason = db.article_exists(
            self.conn, url=url, canonical_url=canonical, content_hash=chash
        )
        if reason:
            return reason

        norm = normalize_title(title)
        if norm:
            for seen in self._recent_titles:
                if not seen:
                    continue
                if fuzz.token_sort_ratio(norm, seen) >= self.fuzzy_threshold:
                    return "fuzzy_title"
        return None

    def register(self, title: str) -> None:
        """Remember an accepted title so later items in the same run dedup
        against it."""
        self._recent_titles.append(normalize_title(title))
