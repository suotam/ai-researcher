"""RSS/Atom feed collector."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import feedparser
import httpx

from ..config import Source
from ..models import RawItem
from ..processing.normalize import clean_text

log = logging.getLogger(__name__)

USER_AGENT = "ai-researcher/0.1 (personal local research assistant)"


def _entry_datetime(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None) or entry.get(attr)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def parse_feed_text(text: str, source: Source, max_items: int) -> list[RawItem]:
    """Parse feed XML into RawItems. Separated from fetching for testability."""
    parsed = feedparser.parse(text)
    if parsed.bozo and not parsed.entries:
        log.warning("Feed '%s' is malformed and yielded no entries (%s)",
                    source.name, getattr(parsed, "bozo_exception", "unknown error"))
        return []
    items: list[RawItem] = []
    for entry in parsed.entries[:max_items]:
        title = clean_text(entry.get("title", ""), 500)
        url = (entry.get("link") or "").strip()
        if not title or not url:
            continue
        summary = clean_text(entry.get("summary", "") or entry.get("description", ""), 2000)
        items.append(
            RawItem(
                source_name=source.name,
                category=source.category,
                title=title,
                url=url,
                published_at=_entry_datetime(entry),
                author=clean_text(entry.get("author", ""), 200),
                summary=summary,
                raw_text=summary,
            )
        )
    return items


def collect(source: Source, *, timeout: float = 20.0, max_items: int = 40) -> list[RawItem]:
    """Fetch and parse one RSS/Atom feed. Never raises; returns [] on failure."""
    if not source.url:
        log.warning("Source '%s' has no URL, skipping", source.name)
        return []
    try:
        resp = httpx.get(
            source.url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Feed '%s' fetch failed: %s", source.name, exc)
        return []
    items = parse_feed_text(resp.text, source, max_items)
    log.debug("Feed '%s': %d items", source.name, len(items))
    return items
