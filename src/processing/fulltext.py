"""Full-text extraction for selected articles.

RSS summaries are often just teasers. Before synthesis, the top-ranked
articles get their page fetched and the main text extracted with
trafilatura, so Glimmer analyses real content. Any failure falls back to
the stored summary — this step must never break the run.
"""

from __future__ import annotations

import logging

import httpx
import trafilatura

log = logging.getLogger(__name__)

USER_AGENT = "ai-researcher/0.1 (personal local research assistant)"


def fetch_fulltext(url: str, *, timeout: float = 20.0,
                   max_chars: int = 4000) -> str | None:
    """Download a page and extract its main text. Returns None on any failure."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.debug("Fulltext fetch failed for %s: %s", url, exc)
        return None
    try:
        text = trafilatura.extract(resp.text, include_comments=False,
                                   include_tables=False, favor_precision=True)
    except Exception as exc:  # trafilatura can throw on exotic inputs
        log.debug("Fulltext extraction failed for %s: %s", url, exc)
        return None
    if not text or len(text.strip()) < 200:
        return None  # extraction produced nothing useful
    return text.strip()[:max_chars]


def enrich_articles(articles: list[dict], *, timeout: float = 20.0,
                    max_chars: int = 4000) -> int:
    """Upgrade raw_text of each article in place. Returns how many succeeded.

    GitHub releases already carry their full release notes and arXiv
    abstracts are complete, so only web articles are fetched.
    """
    enriched = 0
    for article in articles:
        if article.get("source_type") in ("github", "arxiv"):
            continue
        url = article.get("url") or ""
        if not url.startswith("http"):
            continue
        text = fetch_fulltext(url, timeout=timeout, max_chars=max_chars)
        if text:
            article["raw_text"] = text
            enriched += 1
    log.info("Fulltext fetched for %d/%d selected articles", enriched, len(articles))
    return enriched
