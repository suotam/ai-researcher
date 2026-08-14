"""Shared data structures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RawItem:
    """A collected item before normalization / storage."""

    source_name: str
    category: str
    title: str
    url: str
    published_at: datetime | None = None
    author: str = ""
    summary: str = ""
    raw_text: str = ""
