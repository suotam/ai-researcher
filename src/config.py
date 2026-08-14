"""Configuration loading for the AI Researcher.

All configuration lives in YAML files under ``config/``. This module loads
them, applies defaults and exposes simple dict/dataclass access. No YAML
value is required for the app to start — missing keys fall back to sane
defaults so a partially edited config never crashes the run.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "researcher.db"

DEFAULT_SETTINGS: dict[str, Any] = {
    "llm": {
        "base_url": "http://127.0.0.1:8080",
        "model": "Muse-Glimmer-30B",
        "timeout_seconds": 1800,
        "max_retries": 1,
        "temperature": 0.4,
        "max_tokens": 5000,
    },
    "collection": {
        "http_timeout_seconds": 20,
        "max_items_per_source": 40,
        "lookback_hours": 24,
    },
    "dedup": {
        "fuzzy_title_threshold": 88,
        "fuzzy_window_days": 7,
    },
    "ranking": {
        "top_items": 10,
        "min_score": 25,
        "max_per_category": 8,
    },
    "briefing": {
        "output_dir": "output",
        "language": "cs",
        "learning_topic_cooldown_days": 14,
    },
    "logging": {
        "level": "INFO",
        "file": "logs/researcher.log",
    },
}


@dataclass
class Source:
    name: str
    type: str          # rss | github | arxiv
    category: str      # ai | markets
    url: str
    enabled: bool = True
    priority: int = 5
    id: int | None = None  # DB id, filled in after sync


@dataclass
class Config:
    settings: dict[str, Any]
    sources: list[Source]
    topics: dict[str, Any]
    root: Path = field(default_factory=lambda: PROJECT_ROOT)

    def setting(self, *keys: str, default: Any = None) -> Any:
        node: Any = self.settings
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def _merge_defaults(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_defaults(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_sources(path: Path | None = None) -> list[Source]:
    raw = _load_yaml(path or CONFIG_DIR / "sources.yaml")
    sources: list[Source] = []
    for entry in raw.get("sources", []):
        if not isinstance(entry, dict):
            continue
        try:
            sources.append(
                Source(
                    name=str(entry["name"]),
                    type=str(entry.get("type", "rss")).lower(),
                    category=str(entry.get("category", "ai")).lower(),
                    url=str(entry.get("url") or ""),
                    enabled=bool(entry.get("enabled", True)),
                    priority=int(entry.get("priority", 5)),
                )
            )
        except (KeyError, TypeError, ValueError):
            # A malformed source entry must not kill the run.
            continue
    return sources


def load_config(config_dir: Path | None = None) -> Config:
    cdir = config_dir or CONFIG_DIR
    settings = _merge_defaults(DEFAULT_SETTINGS, _load_yaml(cdir / "settings.yaml"))
    sources = load_sources(cdir / "sources.yaml")
    topics = _load_yaml(cdir / "topics.yaml")
    return Config(settings=settings, sources=sources, topics=topics)
