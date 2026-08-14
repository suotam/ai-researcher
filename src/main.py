"""AI Researcher — CLI entry point.

Pipeline:
    SOURCES -> COLLECT -> NORMALIZE -> DEDUPLICATE -> STORE (SQLite)
            -> RANK -> SELECT TOP -> GLIMMER SYNTHESIS -> MORNING BRIEF

Run:
    python -m src.main             # full run (needs local llama.cpp server)
    python -m src.main --no-llm    # skip synthesis, produce fallback brief
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db
from .collectors import arxiv, github, rss
from .config import DB_PATH, PROJECT_ROOT, Config, Source, load_config
from .llm.client import LLMClient, LLMError
from .llm.prompts import build_briefing_prompt
from .models import RawItem
from .processing.deduplicate import Deduplicator
from .processing.normalize import canonicalize_url, clean_text, content_hash
from .processing.ranking import score_article, select_top
from .reporting import briefing

log = logging.getLogger("researcher")

COLLECTORS = {
    "rss": rss.collect,
    "github": github.collect,
    "arxiv": arxiv.collect,
}


def setup_logging(cfg: Config) -> None:
    level = getattr(logging, str(cfg.setting("logging", "level", default="INFO")).upper(),
                    logging.INFO)
    log_file = PROJECT_ROOT / cfg.setting("logging", "file", default="logs/researcher.log")
    log_file.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(console)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Local AI Researcher — collects, ranks and synthesizes a morning brief.",
    )
    parser.add_argument("--hours", type=int, default=None,
                        help="lookback window in hours (default: settings.yaml)")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip Glimmer synthesis, write a fallback brief")
    parser.add_argument("--category", action="append", choices=["ai", "markets"],
                        help="restrict to a category (repeatable)")
    parser.add_argument("--dry-run", action="store_true",
                        help="collect and score but write nothing (no DB rows, no brief)")
    parser.add_argument("--top", type=int, default=None,
                        help="override number of items sent to the LLM")
    return parser.parse_args(argv)


def collect_all(sources: list[Source], *, timeout: float, max_items: int,
                categories: list[str] | None) -> list[RawItem]:
    items: list[RawItem] = []
    active = [s for s in sources
              if s.enabled and (not categories or s.category in categories)]
    log.info("Collecting from %d enabled sources", len(active))
    for source in active:
        collector = COLLECTORS.get(source.type)
        if collector is None:
            log.warning("Unknown source type '%s' for '%s', skipping",
                        source.type, source.name)
            continue
        try:
            fetched = collector(source, timeout=timeout, max_items=max_items)
        except Exception:  # a single broken source must never kill the run
            log.exception("Collector for '%s' failed unexpectedly", source.name)
            fetched = []
        log.info("  %-28s %3d items", source.name, len(fetched))
        items.extend(fetched)
    return items


def store_new_items(conn, items: list[RawItem], cfg: Config,
                    source_ids: dict[str, int | None], *, dry_run: bool) -> tuple[list[int], int]:
    """Dedup + insert. Returns (new article ids, duplicate count)."""
    dedup = Deduplicator(
        conn,
        fuzzy_threshold=int(cfg.setting("dedup", "fuzzy_title_threshold", default=88)),
        window_days=int(cfg.setting("dedup", "fuzzy_window_days", default=7)),
    )
    new_ids: list[int] = []
    duplicates = 0
    now_iso = db.utcnow_iso()
    for item in items:
        url = item.url.strip()
        title = clean_text(item.title, 500)
        if not url or not title:
            continue
        reason = dedup.check(title=title, url=url)
        if reason:
            duplicates += 1
            log.debug("Duplicate (%s): %s", reason, title[:80])
            continue
        dedup.register(title)
        if dry_run:
            continue
        article_id = db.insert_article(
            conn,
            source_id=source_ids.get(item.source_name),
            title=title,
            url=url,
            canonical_url=canonicalize_url(url),
            published_at=item.published_at.isoformat(timespec="seconds")
            if item.published_at else None,
            fetched_at=now_iso,
            author=item.author,
            summary=item.summary,
            raw_text=item.raw_text,
            category=item.category,
            content_hash=content_hash(title),
        )
        new_ids.append(article_id)
    if not dry_run:
        conn.commit()
    return new_ids, duplicates


def rank_new_articles(conn, cfg: Config, article_ids: list[int],
                      sources_by_id: dict[int | None, Source]) -> None:
    now = datetime.now(timezone.utc)
    for article_id in article_ids:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row is None:
            continue
        source = sources_by_id.get(row["source_id"])
        score = score_article(
            title=row["title"],
            summary=row["summary"] or "",
            category=row["category"],
            source_priority=source.priority if source else 3,
            source_type=source.type if source else "rss",
            published_at=row["published_at"],
            topics=cfg.topics,
            now=now,
        )
        db.update_score(conn, article_id, score)
    conn.commit()


def synthesize(cfg: Config, selected: list[dict], day: date,
               recent_topics: list[str]) -> tuple[str, str | None]:
    """Run Glimmer synthesis. Returns (markdown_body, learning_topic).

    Falls back to the raw-selection brief if the server is down or errors out.
    """
    client = LLMClient(
        base_url=str(cfg.setting("llm", "base_url", default="http://127.0.0.1:8080")),
        model=str(cfg.setting("llm", "model", default="Muse-Glimmer-30B")),
        timeout_seconds=float(cfg.setting("llm", "timeout_seconds", default=600)),
        max_retries=int(cfg.setting("llm", "max_retries", default=2)),
    )
    if not client.is_alive():
        log.error("LLM server at %s is not reachable — writing fallback brief. "
                  "Start llama-server and re-run for a full synthesis.", client.base_url)
        return briefing.render_fallback_brief(selected, day, "server nedostupný"), None

    system, user = build_briefing_prompt(
        selected,
        date_str=day.isoformat(),
        recent_learning_topics=recent_topics,
        language=str(cfg.setting("briefing", "language", default="cs")),
    )
    try:
        output = client.chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            temperature=float(cfg.setting("llm", "temperature", default=0.4)),
            max_tokens=int(cfg.setting("llm", "max_tokens", default=3000)),
        )
    except LLMError as exc:
        log.error("LLM synthesis failed: %s — writing fallback brief.", exc)
        return briefing.render_fallback_brief(selected, day, "chyba LLM"), None

    topic = briefing.extract_learning_topic(output)
    return briefing.render_brief(output, selected, day), topic


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    setup_logging(cfg)

    log.info("=== Researcher run started (dry_run=%s, no_llm=%s) ===",
             args.dry_run, args.no_llm)

    hours = args.hours or int(cfg.setting("collection", "lookback_hours", default=24))
    categories = args.category or None

    conn = db.connect(DB_PATH)
    db.sync_sources(conn, cfg.sources)
    source_ids = {s.name: s.id for s in cfg.sources}
    sources_by_id = {s.id: s for s in cfg.sources}

    # 1) collect
    items = collect_all(
        cfg.sources,
        timeout=float(cfg.setting("collection", "http_timeout_seconds", default=20)),
        max_items=int(cfg.setting("collection", "max_items_per_source", default=40)),
        categories=categories,
    )
    log.info("Collected %d raw items", len(items))

    # 2) dedup + store
    new_ids, duplicates = store_new_items(conn, items, cfg, source_ids,
                                          dry_run=args.dry_run)
    log.info("New items stored: %d, duplicates skipped: %d", len(new_ids), duplicates)

    if args.dry_run:
        log.info("Dry run - no DB writes, no briefing. Done.")
        conn.close()
        return 0

    # 3) rank
    rank_new_articles(conn, cfg, new_ids, sources_by_id)

    # 4) select
    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(hours=hours)
    candidates = [dict(r) for r in db.candidates_for_briefing(
        conn, since_iso=period_start.isoformat(timespec="seconds"),
        categories=categories,
    )]
    top_n = args.top or int(cfg.setting("ranking", "top_items", default=12))
    selected = select_top(
        candidates,
        top_items=top_n,
        min_score=int(cfg.setting("ranking", "min_score", default=25)),
        max_per_category=int(cfg.setting("ranking", "max_per_category", default=8)),
    )
    for article in selected:
        article["ref_id"] = f"ARTICLE_{article['id']}"
    log.info("Candidates in window: %d, selected for brief: %d",
             len(candidates), len(selected))

    # 5) synthesize + write brief
    day = date.today()
    out_dir = PROJECT_ROOT / cfg.setting("briefing", "output_dir", default="output")
    path = briefing.briefing_path(out_dir, day)

    learning_topic: str | None = None
    if args.no_llm:
        content = briefing.render_fallback_brief(selected, day, "--no-llm")
    elif not selected:
        log.info("Nothing significant found in the window — writing empty brief.")
        content = briefing.render_fallback_brief(selected, day, "žádné relevantní položky")
    else:
        recent_topics = db.recent_learning_topics(
            conn, int(cfg.setting("briefing", "learning_topic_cooldown_days", default=14)))
        content, learning_topic = synthesize(cfg, selected, day, recent_topics)

    briefing.write_brief(content, path)

    # 6) record briefing + mark articles
    selected_ids = [a["id"] for a in selected]
    db.insert_briefing(
        conn,
        period_start=period_start.isoformat(timespec="seconds"),
        period_end=period_end.isoformat(timespec="seconds"),
        file_path=str(path),
        article_ids=selected_ids,
    )
    db.mark_briefed(conn, selected_ids)
    if learning_topic:
        db.record_learning_topic(conn, learning_topic)
        log.info("Learning topic recorded: %s", learning_topic)
    conn.commit()
    conn.close()

    log.info("=== Run finished. Briefing: %s ===", path)
    print(f"\nBriefing: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
