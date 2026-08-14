"""URL and title normalization used for deduplication."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that never change the content of the page.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "source",
    "cmpid", "smid", "ncid", "sr_share", "guccounter",
}

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")


def canonicalize_url(url: str) -> str:
    """Normalize a URL so trivially different variants compare equal.

    Lowercases scheme+host, drops fragments, tracking params, default ports,
    ``www.`` prefix and trailing slashes.
    """
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = (parts.scheme or "https").lower()
    if scheme == "http":
        scheme = "https"  # http/https variants of the same page are the same page
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(query_pairs))
    return urlunsplit((scheme, host, path, query, ""))


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    title = (title or "").lower().strip()
    title = _NON_ALNUM_RE.sub(" ", title)
    return _WHITESPACE_RE.sub(" ", title).strip()


def content_hash(title: str, category: str = "") -> str:
    """Stable hash of the normalized title (+ category to avoid cross-domain
    collisions on generic titles)."""
    normalized = f"{category}:{normalize_title(title)}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def clean_text(text: str, max_chars: int = 2000) -> str:
    """Strip HTML tags and collapse whitespace; cap length."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:max_chars]
