"""Generic HTML listing collector for sites without RSS.

Configured per source in sources.yaml via an ``item_selector`` (CSS selector
matching the article links on the listing page), e.g.:

    - name: Anthropic News
      type: html
      category: ai
      url: https://www.anthropic.com/news
      item_selector: "a[href^='/news/']"

Listing pages rarely expose publish dates in a uniform way, so items carry
``published_at = None`` — the ranking layer treats unknown dates as
neutral-low, and deduplication ensures an item is only ever stored once
(the first run of a new source is the noisy one; after that only genuinely
new posts appear).

Limitation: only server-rendered pages work (no JavaScript execution).
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from ..config import Source
from ..models import RawItem
from ..processing.normalize import clean_text

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ai-researcher/0.1"

MIN_TITLE_CHARS = 20  # anchors with shorter text are nav links, not articles


def parse_listing(html_text: str, source: Source, max_items: int) -> list[RawItem]:
    selector = str(source.extra.get("item_selector") or "")
    if not selector:
        log.warning("HTML source '%s' has no item_selector, skipping", source.name)
        return []
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        anchors = soup.select(selector)
    except Exception as exc:  # bad selector must not kill the run
        log.warning("HTML source '%s' selector failed: %s", source.name, exc)
        return []

    items: list[RawItem] = []
    seen_urls: set[str] = set()
    for anchor in anchors:
        href = anchor.get("href") or ""
        if not href:
            continue
        url = urljoin(source.url, href)
        if url.rstrip("/") == source.url.rstrip("/") or url in seen_urls:
            continue
        title = clean_text(anchor.get_text(" "), 300)
        if len(title) < MIN_TITLE_CHARS:
            continue
        seen_urls.add(url)
        items.append(
            RawItem(
                source_name=source.name,
                category=source.category,
                title=title,
                url=url,
                published_at=None,  # listing pages don't expose dates uniformly
                summary="",
                raw_text="",
            )
        )
        if len(items) >= max_items:
            break
    return items


def collect(source: Source, *, timeout: float = 20.0, max_items: int = 20) -> list[RawItem]:
    """Fetch a listing page and extract article links. Never raises."""
    if not source.url:
        log.warning("HTML source '%s' has no URL, skipping", source.name)
        return []
    try:
        resp = httpx.get(source.url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("HTML source '%s' fetch failed: %s", source.name, exc)
        return []
    items = parse_listing(resp.text, source, max_items)
    if not items:
        log.warning("HTML source '%s' yielded no items — page may be "
                    "JS-rendered or the selector needs updating", source.name)
    return items
