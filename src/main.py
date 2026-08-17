"""AI Researcher — CLI entry point.

Pipeline:
    SOURCES -> COLLECT -> NORMALIZE -> DEDUPLICATE (+coverage) -> STORE (SQLite)
            -> RANK -> LLM RERANK (fast model) -> SELECT TOP -> FULLTEXT
            -> STORY NOTES (fast model, per article)
            -> ANALYSIS (quality model, one call over the notes)
            -> MORNING BRIEF + CHAT PACK

Run:
    python -m src.main             # full run (needs local llama.cpp server)
    python -m src.main --no-llm    # skip synthesis, produce fallback brief
    python -m src.main --weekly    # weekly digest from the last 7 days
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db
from .collectors import arxiv, github, html, rss
from .config import DB_PATH, PROJECT_ROOT, Config, Source, load_config
from .llm.client import LLMClient, LLMError
from .llm.prompts import build_analysis_prompt, build_weekly_prompt
from .models import RawItem
from .processing.deduplicate import Deduplicator
from .processing.fulltext import enrich_articles
from .processing.normalize import canonicalize_url, clean_text, content_hash
from .processing.notes import extract_notes
from .processing.ranking import score_article, select_top
from .processing.rerank import llm_rerank
from .reporting import briefing

log = logging.getLogger("researcher")

COLLECTORS = {
    "rss": rss.collect,
    "github": github.collect,
    "arxiv": arxiv.collect,
    "html": html.collect,
}

HEALTH_THRESHOLD = 3  # consecutive bad fetches before a source is flagged


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
    parser.add_argument("--weekly", action="store_true",
                        help="generate a weekly digest from the last 7 days "
                             "(no collection, uses stored articles)")
    parser.add_argument("--model", default=None, metavar="NAME",
                        help="synthesis model for this run (a section of "
                             "config/llama-models.ini); overrides llm.model")
    return parser.parse_args(argv)


# ------------------------------------------------------------------ pipeline

def collect_all(conn, sources: list[Source], *, timeout: float, max_items: int,
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
            result = "ok" if fetched else "empty"
        except Exception:  # a single broken source must never kill the run
            log.exception("Collector for '%s' failed unexpectedly", source.name)
            fetched, result = [], "error"
        if source.id is not None:
            bad = db.update_source_health(conn, source.id, result)
            if bad >= HEALTH_THRESHOLD:
                log.warning("Source '%s' has failed %d consecutive runs",
                            source.name, bad)
        log.info("  %-28s %3d items", source.name, len(fetched))
        items.extend(fetched)
    conn.commit()
    return items


def store_new_items(conn, items: list[RawItem], cfg: Config,
                    source_ids: dict[str, int | None], *,
                    dry_run: bool) -> tuple[list[int], set[int], int]:
    """Dedup + insert.

    Returns (new article ids, ids of originals whose coverage grew,
    duplicate count).
    """
    dedup = Deduplicator(
        conn,
        fuzzy_threshold=int(cfg.setting("dedup", "fuzzy_title_threshold", default=88)),
        window_days=int(cfg.setting("dedup", "fuzzy_window_days", default=7)),
    )
    new_ids: list[int] = []
    touched_originals: set[int] = set()
    duplicates = 0
    now_iso = db.utcnow_iso()
    for item in items:
        url = item.url.strip()
        title = clean_text(item.title, 500)
        if not url or not title:
            continue
        match = dedup.check(title=title, url=url)
        if match:
            duplicates += 1
            # Coverage tracking: same event from a *different* source is an
            # importance signal for the original article.
            if match.article_id is not None and not dry_run:
                original = conn.execute(
                    "SELECT s.name AS src FROM articles a "
                    "LEFT JOIN sources s ON s.id = a.source_id WHERE a.id = ?",
                    (match.article_id,)).fetchone()
                if original and original["src"] != item.source_name:
                    db.record_duplicate(
                        conn, article_id=match.article_id,
                        source_name=item.source_name, title=title, url=url)
                    touched_originals.add(match.article_id)
            log.debug("Duplicate (%s): %s", match.reason, title[:80])
            continue
        if dry_run:
            dedup.register(title)
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
        dedup.register(title, article_id)
        new_ids.append(article_id)
    if not dry_run:
        conn.commit()
    return new_ids, touched_originals, duplicates


def rank_articles(conn, cfg: Config, article_ids: list[int],
                  sources_by_id: dict[int | None, Source]) -> None:
    now = datetime.now(timezone.utc)
    feedback_adjust = db.source_feedback_adjustments(conn)
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
            duplicate_count=row["duplicate_count"],
            feedback_adjust=feedback_adjust.get(row["source_id"], 0),
        )
        db.update_score(conn, article_id, score)
    conn.commit()


def make_llm_client(cfg: Config) -> LLMClient:
    """Client for the quality model (llm.model): the analysis stage."""
    return LLMClient(
        base_url=str(cfg.setting("llm", "base_url", default="http://127.0.0.1:8080")),
        model=str(cfg.setting("llm", "model", default="muse-glimmer-30b")),
        timeout_seconds=float(cfg.setting("llm", "timeout_seconds", default=3600)),
        max_retries=int(cfg.setting("llm", "max_retries", default=0)),
        reasoning_effort=cfg.setting("llm", "reasoning_effort", default="low") or None,
    )


def fast_client(cfg: Config, client: LLMClient) -> LLMClient:
    """Client for the cheap stages (rerank, story notes, fallback analysis):
    llm.fast_model, or the quality model when none is configured."""
    return client.with_model(cfg.setting("llm", "fast_model"))


def upcoming_calendar_events(cfg: Config, today: date) -> list[dict]:
    horizon = int(cfg.setting("briefing", "calendar_horizon_days", default=7))
    end = today + timedelta(days=horizon)
    upcoming = []
    for event in cfg.calendar:
        try:
            event_day = date.fromisoformat(event["date"])
        except ValueError:
            continue
        if today <= event_day <= end:
            upcoming.append(event)
    return sorted(upcoming, key=lambda e: e["date"])


CHARS_PER_TOKEN = 3.5  # rough estimate for prompt budgeting

# A reasoning model burns a large part of its completion budget on thinking
# before it writes the brief. Below this floor it tends to produce nothing,
# so shorter notes are always the better trade.
MIN_COMPLETION_TOKENS = 3000


def _language(cfg: Config) -> str:
    return str(cfg.setting("briefing", "language", default="en"))


def _fit_prompt(cfg: Config, client: LLMClient, selected: list[dict], day: date,
                recent_topics: list[str],
                calendar_events: list[dict]) -> tuple[str, str, int]:
    """Build the analysis prompt sized to the server's actual context window.

    Starts with full story notes and trims them until prompt + a completion
    of at least MIN_COMPLETION_TOKENS fits into n_ctx (with ~5% margin).
    Returns (system, user, max_tokens)."""
    max_tokens = int(cfg.setting("llm", "max_tokens", default=5000))
    n_ctx = client.context_size()

    budget = max_tokens
    for max_notes_chars in (1500, 1000, 700, 400):
        system, user = build_analysis_prompt(
            selected, date_str=day.isoformat(),
            recent_learning_topics=recent_topics, language=_language(cfg),
            calendar_events=calendar_events, max_notes_chars=max_notes_chars,
        )
        if n_ctx is None:
            return system, user, max_tokens
        prompt_tokens = int(len(system + user) / CHARS_PER_TOKEN)
        budget = int(n_ctx * 0.95) - prompt_tokens
        if budget >= min(max_tokens, MIN_COMPLETION_TOKENS):
            if budget < max_tokens:
                log.info("Context %d is tight: capping completion to %d tokens "
                         "(notes at %d chars). Restart llama-server with a "
                         "larger -c for fuller briefs.",
                         n_ctx, budget, max_notes_chars)
            return system, user, min(max_tokens, budget)
    log.warning("Prompt barely fits context %s even with short notes — "
                "the model may not finish the brief", n_ctx)
    return system, user, max(budget, 1500)


def synthesize(cfg: Config, client: LLMClient, fast: LLMClient,
               selected: list[dict], day: date,
               recent_topics: list[str], calendar_events: list[dict],
               unhealthy: list[dict]) -> tuple[str, str | None]:
    """Stage 2: the analytical layer over the story notes.

    Tries the quality model first; if it fails (timeout, server error) the
    fast model writes the analysis so a brief always arrives. Only when both
    fail is the fallback (notes + raw selection) written.
    Returns (markdown_body, learning_topic)."""
    temperature = float(cfg.setting("llm", "temperature", default=0.4))
    tried: list[LLMClient] = [client]
    if fast.model != client.model:
        tried.append(fast)
    for stage_client in tried:
        system, user, max_tokens = _fit_prompt(
            cfg, stage_client, selected, day, recent_topics, calendar_events)
        try:
            output = stage_client.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except LLMError as exc:
            log.error("Analysis with %s failed: %s", stage_client.model, exc)
            continue
        topic = briefing.extract_learning_topic(output)
        return briefing.render_brief(output, selected, day, unhealthy,
                                     language=_language(cfg)), topic

    log.error("All analysis models failed — writing fallback brief.")
    return briefing.render_fallback_brief(
        selected, day, "LLM error", calendar_events, unhealthy,
        language=_language(cfg)), None


# ------------------------------------------------------------------ weekly

def run_weekly(cfg: Config, args: argparse.Namespace) -> int:
    """Weekly digest from stored articles — no collection, one LLM call."""
    conn = db.connect(DB_PATH)
    today = date.today()
    period_start = datetime.now(timezone.utc) - timedelta(days=7)
    limit = args.top or int(cfg.setting("briefing", "weekly_top_items", default=15))
    rows = [dict(r) for r in db.top_articles_for_period(
        conn, since_iso=period_start.isoformat(timespec="seconds"), limit=limit)]
    for article in rows:
        article["ref_id"] = f"ARTICLE_{article['id']}"
        article["coverage_sources"] = db.coverage_sources(conn, article["id"])
    log.info("Weekly digest: %d articles from the last 7 days", len(rows))

    out_dir = PROJECT_ROOT / cfg.setting("briefing", "output_dir", default="output")
    path = briefing.weekly_path(out_dir, today)

    language = _language(cfg)
    if args.no_llm or not rows:
        content = briefing.render_fallback_brief(
            rows, today, "--no-llm" if args.no_llm else "no items", language=language)
    else:
        client = make_llm_client(cfg)
        if not client.is_alive():
            log.error("LLM server not reachable — writing fallback weekly digest.")
            content = briefing.render_fallback_brief(rows, today, "server unreachable",
                                                     language=language)
        else:
            system, user = build_weekly_prompt(
                rows, date_str=today.isoformat(), language=language)
            try:
                output = client.chat(
                    [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
                    temperature=float(cfg.setting("llm", "temperature", default=0.4)),
                    max_tokens=int(cfg.setting("llm", "max_tokens", default=5000)),
                )
                content = briefing.render_brief(output, rows, today,
                                                title="Weekly Digest", language=language)
            except LLMError as exc:
                log.error("Weekly synthesis failed: %s", exc)
                content = briefing.render_fallback_brief(rows, today, "LLM error",
                                                         language=language)

    briefing.write_brief(content, path)
    db.insert_briefing(
        conn,
        period_start=period_start.isoformat(timespec="seconds"),
        period_end=db.utcnow_iso(),
        file_path=str(path),
        article_ids=[a["id"] for a in rows],
    )
    conn.commit()
    conn.close()
    log.info("=== Weekly digest finished: %s ===", path)
    print(f"\nWeekly digest: {path}")
    return 0


# ------------------------------------------------------------------ daily

def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config()
    setup_logging(cfg)
    if args.model:
        cfg.settings.setdefault("llm", {})["model"] = args.model

    if args.weekly:
        log.info("=== Weekly digest run started ===")
        return run_weekly(cfg, args)

    log.info("=== Researcher run started (dry_run=%s, no_llm=%s) ===",
             args.dry_run, args.no_llm)

    hours = args.hours or int(cfg.setting("collection", "lookback_hours", default=24))
    categories = args.category or None

    conn = db.connect(DB_PATH)
    db.sync_sources(conn, cfg.sources)
    source_ids = {s.name: s.id for s in cfg.sources}
    sources_by_id = {s.id: s for s in cfg.sources}

    # 1) collect (health-tracked)
    items = collect_all(
        conn, cfg.sources,
        timeout=float(cfg.setting("collection", "http_timeout_seconds", default=20)),
        max_items=int(cfg.setting("collection", "max_items_per_source", default=40)),
        categories=categories,
    )
    log.info("Collected %d raw items", len(items))

    # 2) dedup + store (+ coverage tracking)
    new_ids, touched, duplicates = store_new_items(
        conn, items, cfg, source_ids, dry_run=args.dry_run)
    log.info("New items stored: %d, duplicates: %d (coverage grew for %d articles)",
             len(new_ids), duplicates, len(touched))

    if args.dry_run:
        log.info("Dry run - no DB writes, no briefing. Done.")
        conn.close()
        return 0

    # 3) rank new articles + re-rank originals whose coverage grew
    rank_articles(conn, cfg, list(dict.fromkeys(new_ids + list(touched))),
                  sources_by_id)

    # 4) select candidates
    period_end = datetime.now(timezone.utc)
    period_start = period_end - timedelta(hours=hours)
    candidates = [dict(r) for r in db.candidates_for_briefing(
        conn, since_iso=period_start.isoformat(timespec="seconds"),
        categories=categories,
    )]
    top_n = args.top or int(cfg.setting("ranking", "top_items", default=10))
    preselected = select_top(
        candidates,
        top_items=int(cfg.setting("ranking", "rerank_pool", default=30)),
        min_score=int(cfg.setting("ranking", "min_score", default=25)),
        max_per_category=int(cfg.setting("ranking", "rerank_pool", default=30)),
    )
    log.info("Candidates in window: %d, pre-selected for rerank: %d",
             len(candidates), len(preselected))

    # 5) LLM rerank (optional, falls back to cheap ranking)
    client = make_llm_client(cfg)
    fast = fast_client(cfg, client)
    llm_available = not args.no_llm and client.is_alive()
    if not args.no_llm and not llm_available:
        log.error("LLM server at %s is not reachable — continuing without it. "
                  "Start llama-server and re-run for a full synthesis.",
                  client.base_url)

    selected: list[dict] | None = None
    if llm_available and cfg.setting("ranking", "llm_rerank", default=True) \
            and len(preselected) > top_n:
        selected = llm_rerank(fast, preselected, top_n=top_n)
    if selected is None:
        selected = select_top(
            preselected,
            top_items=top_n,
            min_score=int(cfg.setting("ranking", "min_score", default=25)),
            max_per_category=int(cfg.setting("ranking", "max_per_category", default=8)),
        )
    for article in selected:
        article["ref_id"] = f"ARTICLE_{article['id']}"
        article["coverage_sources"] = db.coverage_sources(conn, article["id"])
    log.info("Selected for brief: %d", len(selected))

    # 6) fulltext enrichment for the final selection
    if selected and cfg.setting("collection", "fetch_fulltext", default=True):
        enrich_articles(
            selected,
            timeout=float(cfg.setting("collection", "http_timeout_seconds", default=20)),
            max_chars=int(cfg.setting("collection", "fulltext_max_chars", default=4000)),
        )

    # 7) story notes (fast model) -> analysis (quality model) -> write brief
    day = date.today()
    out_dir = PROJECT_ROOT / cfg.setting("briefing", "output_dir", default="output")
    path = briefing.briefing_path(out_dir, day)
    calendar_events = upcoming_calendar_events(cfg, day)
    unhealthy = [dict(r) for r in db.unhealthy_sources(conn, threshold=HEALTH_THRESHOLD)]
    language = _language(cfg)

    learning_topic: str | None = None
    if not llm_available:
        reason = "--no-llm" if args.no_llm else "server unreachable"
        content = briefing.render_fallback_brief(selected, day, reason,
                                                 calendar_events, unhealthy,
                                                 language=language)
    elif not selected:
        log.info("Nothing significant found in the window — writing empty brief.")
        content = briefing.render_fallback_brief(selected, day, "no relevant items",
                                                 calendar_events, unhealthy,
                                                 language=language)
    else:
        if cfg.setting("briefing", "story_notes", default=True):
            extract_notes(
                fast, selected,
                max_text_chars=int(cfg.setting("collection", "fulltext_max_chars",
                                               default=4000)),
                max_tokens=int(cfg.setting("briefing", "notes_max_tokens", default=600)),
                language=language,
            )
            # The facts layer is ready minutes before the analysis: publish
            # it now so the file is useful even while the slow model works.
            briefing.write_brief(briefing.render_fallback_brief(
                selected, day, "analysis in progress", calendar_events, unhealthy,
                language=language), path)
        recent_topics = db.recent_learning_topics(
            conn, int(cfg.setting("briefing", "learning_topic_cooldown_days", default=14)))
        content, learning_topic = synthesize(
            cfg, client, fast, selected, day, recent_topics, calendar_events, unhealthy)

    briefing.write_brief(content, path)
    if cfg.setting("briefing", "chat_pack", default=True):
        briefing.write_brief(briefing.render_chat_pack(content, day),
                             briefing.chat_pack_path(out_dir, day))

    # 8) record briefing + mark articles
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
