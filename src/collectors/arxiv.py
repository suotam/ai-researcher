"""arXiv collector.

The arXiv API returns Atom, so this is a thin wrapper around the RSS
collector's parser. Papers deliberately keep a low source priority — the
ranking layer additionally penalises the 'arxiv' source type so only truly
significant papers can reach the brief.
"""

from __future__ import annotations

import logging

import httpx

from ..config import Source
from ..models import RawItem
from .rss import USER_AGENT, parse_feed_text

log = logging.getLogger(__name__)


def collect(source: Source, *, timeout: float = 20.0, max_items: int = 15) -> list[RawItem]:
    if not source.url:
        log.warning("arXiv source '%s' has no query URL, skipping", source.name)
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
        log.warning("arXiv fetch failed for '%s': %s", source.name, exc)
        return []
    return parse_feed_text(resp.text, source, max_items)
