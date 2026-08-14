"""Cheap, LLM-free relevance scoring (0-100).

Components:
  * source priority   (0-10 -> up to 25 points)
  * recency           (up to 20 points)
  * topic keywords    (high/medium/entities from topics.yaml, up to 40 points)
  * title indicators  (announcement-style boosts, listicle/promo penalties)
  * source type       (arXiv papers get a flat penalty: low default priority)
  * category importance multiplier

Only the top N articles ever reach the LLM.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from dateutil import parser as dateparser

POSITIVE_TITLE_WORDS = (
    "announc", "launch", "release", "unveil", "introduc", "breakthrough",
    "record", "acqui", "raises", "cuts", "hikes", "surges", "plunges",
    "beats", "misses", "warns",
)
NEGATIVE_TITLE_WORDS = (
    "webinar", "sponsored", "how to", "top 10", "top 5", "best of",
    "roundup", "giveaway", "deal", "coupon", "quiz",
)

ARXIV_PENALTY = 25


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = dateparser.parse(str(value))
        except (ValueError, OverflowError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def recency_points(published_at: Any, now: datetime | None = None) -> int:
    dt = _parse_dt(published_at)
    if dt is None:
        return 5  # unknown date: neutral-low
    now = now or datetime.now(timezone.utc)
    age_hours = (now - dt).total_seconds() / 3600.0
    if age_hours < 0:
        age_hours = 0.0
    if age_hours <= 6:
        return 20
    if age_hours <= 24:
        return 15
    if age_hours <= 48:
        return 8
    if age_hours <= 72:
        return 4
    return 0


def keyword_points(text: str, topic_cfg: dict[str, Any]) -> int:
    """Score keyword/entity matches in the given text. Capped at 40."""
    text_l = (text or "").lower()
    points = 0
    keywords = topic_cfg.get("keywords") or {}
    for kw in keywords.get("high") or []:
        if str(kw).lower() in text_l:
            points += 12
    for kw in keywords.get("medium") or []:
        if str(kw).lower() in text_l:
            points += 5
    for entity in topic_cfg.get("entities") or []:
        if str(entity).lower() in text_l:
            points += 6
    for kw in topic_cfg.get("negative") or []:
        if str(kw).lower() in text_l:
            points -= 15
    return min(points, 40)


def title_indicator_points(title: str) -> int:
    title_l = (title or "").lower()
    points = 0
    for word in POSITIVE_TITLE_WORDS:
        if word in title_l:
            points += 5
            break  # one boost is enough
    for word in NEGATIVE_TITLE_WORDS:
        if word in title_l:
            points -= 15
    return points


def coverage_points(duplicate_count: int) -> int:
    """Breadth of coverage: each extra outlet writing about the same event
    adds points, capped so one viral story cannot dominate on this alone."""
    return min(max(duplicate_count, 0) * 4, 12)


def score_article(*, title: str, summary: str, category: str,
                  source_priority: int, source_type: str,
                  published_at: Any, topics: dict[str, Any],
                  now: datetime | None = None,
                  duplicate_count: int = 0,
                  feedback_adjust: int = 0) -> int:
    topic_cfg = topics.get(category) or {}
    score = 0.0
    score += min(max(source_priority, 0), 10) * 2.5
    score += recency_points(published_at, now)
    score += keyword_points(f"{title} {summary}", topic_cfg)
    score += title_indicator_points(title)
    score += coverage_points(duplicate_count)
    score += feedback_adjust
    if source_type == "arxiv":
        score -= ARXIV_PENALTY
    importance = topic_cfg.get("importance", 1.0)
    try:
        score *= float(importance)
    except (TypeError, ValueError):
        pass
    return max(0, min(100, round(score)))


def select_top(rows: list[dict[str, Any]], *, top_items: int, min_score: int,
               max_per_category: int) -> list[dict[str, Any]]:
    """Pick the top N rows overall while capping any single category.

    ``rows`` must already be sorted by score descending.
    """
    selected: list[dict[str, Any]] = []
    per_category: dict[str, int] = {}
    for row in rows:
        if len(selected) >= top_items:
            break
        if row["relevance_score"] < min_score:
            continue
        cat = row["category"]
        if per_category.get(cat, 0) >= max_per_category:
            continue
        selected.append(row)
        per_category[cat] = per_category.get(cat, 0) + 1
    return selected
