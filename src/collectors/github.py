"""GitHub releases collector (public API, no token)."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
from dateutil import parser as dateparser

from ..config import Source
from ..models import RawItem
from ..processing.normalize import clean_text

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
USER_AGENT = "ai-researcher/0.1 (personal local research assistant)"


def _parse_repo(url: str) -> str | None:
    """Accept 'owner/repo' or a full github.com URL."""
    url = (url or "").strip().rstrip("/")
    if not url:
        return None
    if "github.com/" in url:
        url = url.split("github.com/", 1)[1]
    parts = url.split("/")
    if len(parts) >= 2 and parts[0] and parts[1]:
        return f"{parts[0]}/{parts[1]}"
    return None


def parse_releases(payload: list[dict], source: Source, repo: str,
                   max_items: int) -> list[RawItem]:
    items: list[RawItem] = []
    for rel in payload[:max_items]:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag = rel.get("tag_name") or ""
        name = rel.get("name") or tag
        published: datetime | None = None
        if rel.get("published_at"):
            try:
                published = dateparser.parse(rel["published_at"])
            except (ValueError, OverflowError):
                published = None
        body = clean_text(rel.get("body") or "", 2000)
        items.append(
            RawItem(
                source_name=source.name,
                category=source.category,
                title=f"{repo} release: {name}" if name else f"{repo} release {tag}",
                url=rel.get("html_url") or f"https://github.com/{repo}/releases",
                published_at=published,
                author=(rel.get("author") or {}).get("login", ""),
                summary=body[:500],
                raw_text=body,
            )
        )
    return items


def collect(source: Source, *, timeout: float = 20.0, max_items: int = 10) -> list[RawItem]:
    """Fetch recent releases for one repo. Never raises; returns [] on failure."""
    repo = _parse_repo(source.url)
    if not repo:
        log.warning("GitHub source '%s' has invalid repo '%s', skipping",
                    source.name, source.url)
        return []
    try:
        resp = httpx.get(
            f"{API_BASE}/repos/{repo}/releases",
            params={"per_page": max_items},
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
            },
        )
        if resp.status_code == 403:
            log.warning("GitHub rate limit hit for '%s' (unauthenticated: 60 req/h)", repo)
            return []
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("GitHub releases fetch failed for '%s': %s", repo, exc)
        return []
    if not isinstance(payload, list):
        log.warning("Unexpected GitHub API payload for '%s'", repo)
        return []
    return parse_releases(payload, source, repo, max_items)
