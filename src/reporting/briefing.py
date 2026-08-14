"""Morning brief rendering and post-processing."""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_ARTICLE_REF_RE = re.compile(r"\[ARTICLE_(\d+)\]")
_LEARNING_TOPIC_RE = re.compile(r"^LEARNING_TOPIC:\s*(.+)$", re.MULTILINE)


def briefing_path(output_dir: str | Path, day: date) -> Path:
    return Path(output_dir) / f"{day.isoformat()}-morning-brief.md"


def weekly_path(output_dir: str | Path, day: date) -> Path:
    return Path(output_dir) / f"{day.isoformat()}-weekly-digest.md"


def health_section(unhealthy: list[dict]) -> str:
    """Warn about sources that failed repeatedly — a dead feed is otherwise
    invisible for weeks."""
    if not unhealthy:
        return ""
    lines = ["", "---", "", "## Zdroje s problémy", ""]
    for s in unhealthy:
        lines.append(
            f"- **{s['name']}** — {s['consecutive_bad']}× po sobě bez dat "
            f"(poslední stav: {s['last_result']}). Zkontroluj URL/selektor "
            "v config/sources.yaml."
        )
    return "\n".join(lines)


def extract_learning_topic(text: str) -> str | None:
    match = _LEARNING_TOPIC_RE.search(text or "")
    return match.group(1).strip() if match else None


def _sources_section(articles: list[dict[str, Any]]) -> str:
    lines = ["", "---", "", "## Sources", ""]
    for a in articles:
        num = a["ref_id"].rsplit("_", 1)[-1]
        published = a.get("published_at") or "unknown date"
        lines.append(
            f"- **[{num}]** [{a.get('title', '')}]({a.get('url', '')}) — "
            f"{a.get('source_name', '')}, {published}"
        )
    return "\n".join(lines)


def render_brief(llm_output: str, articles: list[dict[str, Any]], day: date,
                 unhealthy_sources: list[dict] | None = None,
                 title: str = "Morning Brief") -> str:
    """Turn the LLM output into the final Markdown file content.

    Inline [ARTICLE_12] references become linked [[12]](url) markers and a
    Sources section maps every number back to title + URL, so each claim is
    traceable.
    """
    url_by_id = {
        a["ref_id"].rsplit("_", 1)[-1]: a.get("url", "") for a in articles
    }

    def _replace(match: re.Match) -> str:
        num = match.group(1)
        url = url_by_id.get(num)
        return f"[[{num}]]({url})" if url else f"[{num}]"

    body = _ARTICLE_REF_RE.sub(_replace, llm_output or "")
    # Drop the machine-readable learning-topic marker from the human report.
    body = _LEARNING_TOPIC_RE.sub(lambda m: f"**Téma: {m.group(1).strip()}**", body)

    header = f"# {title} — {day.isoformat()}\n\n"
    if body.lstrip().startswith("# "):
        header = ""  # model already produced a top-level heading
    return (header + body.strip() + _sources_section(articles)
            + health_section(unhealthy_sources or []) + "\n")


def render_fallback_brief(articles: list[dict[str, Any]], day: date,
                          reason: str = "LLM disabled",
                          calendar_events: list[dict] | None = None,
                          unhealthy_sources: list[dict] | None = None) -> str:
    """Brief without LLM synthesis (--no-llm or Glimmer unreachable).

    Groups the top-ranked items by category so the run still produces a
    useful, traceable partial briefing.
    """
    lines = [
        f"# Morning Brief — {day.isoformat()}",
        "",
        f"> Poznámka: syntéza přes LLM neproběhla ({reason}). "
        "Níže je surový výběr nejrelevantnějších položek.",
        "",
    ]
    by_category: dict[str, list[dict[str, Any]]] = {}
    for a in articles:
        by_category.setdefault(a.get("category", "other"), []).append(a)

    if not articles:
        lines.append("Nic významného se v posledním okně nenašlo.")

    for category in sorted(by_category):
        lines.append(f"## {category.upper()}")
        lines.append("")
        for a in by_category[category]:
            published = a.get("published_at") or "unknown date"
            lines.append(f"### {a.get('title', '')}")
            lines.append("")
            lines.append(f"- Score: {a.get('relevance_score', 0)}")
            lines.append(f"- Source: {a.get('source_name', '')}, {published}")
            lines.append(f"- URL: {a.get('url', '')}")
            summary = (a.get("summary") or "").strip()
            if summary:
                lines.append(f"- Summary: {summary[:400]}")
            if a.get("duplicate_count"):
                lines.append(f"- Coverage: {a['duplicate_count'] + 1} outlets")
            lines.append("")

    if calendar_events:
        lines.append("## Watchlist (kalendář)")
        lines.append("")
        for e in calendar_events:
            note = f" — {e['note']}" if e.get("note") else ""
            lines.append(f"- {e['date']}: {e['title']}{note}")
        lines.append("")

    return ("\n".join(lines) + _sources_section(articles)
            + health_section(unhealthy_sources or []) + "\n")


def write_brief(content: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("Briefing written to %s", path)
    return path
