"""Multi-level deduplication with coverage tracking.

Levels (cheapest first):
  1. exact URL          (DB lookup)
  2. canonicalized URL  (DB lookup)
  3. content hash       (normalized title hash, DB lookup)
  4. fuzzy title match  (rapidfuzz against recent titles)

A duplicate is not just discarded: it is recorded against the original
article (article_duplicates + duplicate_count), because five outlets
covering the same event is an importance signal the ranking layer uses.

Embedding-based clustering can later be added as level 5 behind the same
``Deduplicator.check()`` interface.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from rapidfuzz import fuzz

from .. import db
from .normalize import canonicalize_url, content_hash, normalize_title

log = logging.getLogger(__name__)


@dataclass
class DupMatch:
    reason: str            # exact_url | canonical_url | content_hash | fuzzy_title
    article_id: int | None  # original article; None only for intra-run fuzzy hits
                            # against not-yet-committed titles


class Deduplicator:
    def __init__(self, conn: sqlite3.Connection, *, fuzzy_threshold: int = 88,
                 window_days: int = 7):
        self.conn = conn
        self.fuzzy_threshold = fuzzy_threshold
        # (article_id, normalized title) from the recent window; grows with
        # titles accepted during this run so intra-run duplicates are caught.
        self._recent: list[tuple[int | None, str]] = [
            (aid, normalize_title(t)) for aid, t in db.recent_titles(conn, window_days)
        ]

    def check(self, *, title: str, url: str) -> DupMatch | None:
        """Return a DupMatch describing the original, or None if the item is new."""
        canonical = canonicalize_url(url)
        chash = content_hash(title)
        hit = db.article_exists(
            self.conn, url=url, canonical_url=canonical, content_hash=chash
        )
        if hit:
            reason, article_id = hit
            return DupMatch(reason, article_id)

        norm = normalize_title(title)
        if norm:
            for aid, seen in self._recent:
                if not seen:
                    continue
                if fuzz.token_sort_ratio(norm, seen) >= self.fuzzy_threshold:
                    return DupMatch("fuzzy_title", aid)
        return None

    def register(self, title: str, article_id: int | None = None) -> None:
        """Remember an accepted title so later items in the same run dedup
        against it."""
        self._recent.append((article_id, normalize_title(title)))
