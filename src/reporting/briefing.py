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

# The few human-facing strings the renderer itself emits.
_STRINGS = {
    "en": {
        "topic": "Topic",
        "notes": "Story Notes",
        "notes_intro": "Facts extracted per item by the fast model; the analysis above "
                       "is built on these. Numbers in [[n]] link to the sources.",
        "unhealthy": "Sources with problems",
        "unhealthy_line": "- **{name}** — {n}× in a row without data (last status: "
                          "{status}). Check the URL/selector in config/sources.yaml.",
        "fallback_note": "> Note: LLM synthesis did not run ({reason}). Below is the raw "
                         "selection of the most relevant items.",
        "nothing": "Nothing significant found in the last window.",
        "calendar": "Watchlist (calendar)",
        "coverage": "Coverage",
        "outlets": "outlets",
    },
    "cs": {
        "topic": "Téma",
        "notes": "Poznámky ke zprávám",
        "notes_intro": "Fakta vytažená ke každé položce rychlým modelem; analýza výše "
                       "staví na nich. Čísla v [[n]] odkazují na zdroje.",
        "unhealthy": "Zdroje s problémy",
        "unhealthy_line": "- **{name}** — {n}× po sobě bez dat (poslední stav: {status}). "
                          "Zkontroluj URL/selektor v config/sources.yaml.",
        "fallback_note": "> Poznámka: syntéza přes LLM neproběhla ({reason}). Níže je "
                         "surový výběr nejrelevantnějších položek.",
        "nothing": "Nic významného se v posledním okně nenašlo.",
        "calendar": "Watchlist (kalendář)",
        "coverage": "Pokrytí",
        "outlets": "médií",
    },
}


def _s(language: str) -> dict[str, str]:
    return _STRINGS.get(language, _STRINGS["en"])


def briefing_path(output_dir: str | Path, day: date) -> Path:
    return Path(output_dir) / f"{day.isoformat()}-morning-brief.md"


def chat_pack_path(output_dir: str | Path, day: date) -> Path:
    return Path(output_dir) / f"{day.isoformat()}-chat-pack.md"


def weekly_path(output_dir: str | Path, day: date) -> Path:
    return Path(output_dir) / f"{day.isoformat()}-weekly-digest.md"


def health_section(unhealthy: list[dict], language: str = "en") -> str:
    """Warn about sources that failed repeatedly — a dead feed is otherwise
    invisible for weeks."""
    if not unhealthy:
        return ""
    s = _s(language)
    lines = ["", "---", "", f"## {s['unhealthy']}", ""]
    for src in unhealthy:
        lines.append(s["unhealthy_line"].format(
            name=src["name"], n=src["consecutive_bad"], status=src["last_result"]))
    return "\n".join(lines)


def extract_learning_topic(text: str) -> str | None:
    match = _LEARNING_TOPIC_RE.search(text or "")
    return match.group(1).strip() if match else None


def _ref_num(article: dict[str, Any]) -> str:
    return article["ref_id"].rsplit("_", 1)[-1]


def _sources_section(articles: list[dict[str, Any]]) -> str:
    lines = ["", "---", "", "## Sources", ""]
    for a in articles:
        published = a.get("published_at") or "unknown date"
        lines.append(
            f"- **[{_ref_num(a)}]** [{a.get('title', '')}]({a.get('url', '')}) — "
            f"{a.get('source_name', '')}, {published}"
        )
    return "\n".join(lines)


def _notes_section(articles: list[dict[str, Any]], language: str) -> str:
    """The facts layer: per-item story notes from stage 1 (if any)."""
    with_notes = [a for a in articles if a.get("notes")]
    if not with_notes:
        return ""
    s = _s(language)
    lines = ["", "---", "", f"## {s['notes']}", "", s["notes_intro"], ""]
    for a in with_notes:
        num = _ref_num(a)
        published = a.get("published_at") or "unknown date"
        lines.append(f"### [[{num}]]({a.get('url', '')}) {a.get('title', '')}")
        meta = f"*{a.get('source_name', '')}, {published}"
        if a.get("duplicate_count"):
            meta += f" — {s['coverage']}: {a['duplicate_count'] + 1} {s['outlets']}"
        lines.append(meta + "*")
        lines.append("")
        lines.append(a["notes"].strip())
        lines.append("")
    return "\n".join(lines)


def _link_refs(text: str, articles: list[dict[str, Any]]) -> str:
    url_by_id = {_ref_num(a): a.get("url", "") for a in articles}

    def _replace(match: re.Match) -> str:
        num = match.group(1)
        url = url_by_id.get(num)
        return f"[[{num}]]({url})" if url else f"[{num}]"

    return _ARTICLE_REF_RE.sub(_replace, text or "")


def render_brief(llm_output: str, articles: list[dict[str, Any]], day: date,
                 unhealthy_sources: list[dict] | None = None,
                 title: str = "Morning Brief", language: str = "en") -> str:
    """Turn the LLM output into the final Markdown file content.

    Inline [ARTICLE_12] references become linked [[12]](url) markers, the
    stage-1 story notes are appended as the facts layer, and a Sources
    section maps every number back to title + URL, so each claim is
    traceable.
    """
    body = _link_refs(llm_output, articles)
    # Drop the machine-readable learning-topic marker from the human report.
    topic_label = _s(language)["topic"]
    body = _LEARNING_TOPIC_RE.sub(lambda m: f"**{topic_label}: {m.group(1).strip()}**", body)

    header = f"# {title} — {day.isoformat()}\n\n"
    if body.lstrip().startswith("# "):
        header = ""  # model already produced a top-level heading
    return (header + body.strip() + _notes_section(articles, language)
            + _sources_section(articles)
            + health_section(unhealthy_sources or [], language) + "\n")


def render_fallback_brief(articles: list[dict[str, Any]], day: date,
                          reason: str = "LLM disabled",
                          calendar_events: list[dict] | None = None,
                          unhealthy_sources: list[dict] | None = None,
                          language: str = "en") -> str:
    """Brief without the analysis stage (--no-llm or LLM unreachable).

    Groups the top-ranked items by category so the run still produces a
    useful, traceable partial briefing. Story notes are included when the
    fast model produced them.
    """
    s = _s(language)
    lines = [
        f"# Morning Brief — {day.isoformat()}",
        "",
        s["fallback_note"].format(reason=reason),
        "",
    ]
    by_category: dict[str, list[dict[str, Any]]] = {}
    for a in articles:
        by_category.setdefault(a.get("category", "other"), []).append(a)

    if not articles:
        lines.append(s["nothing"])

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
                lines.append(f"- {s['coverage']}: {a['duplicate_count'] + 1} {s['outlets']}")
            lines.append("")

    if calendar_events:
        lines.append(f"## {s['calendar']}")
        lines.append("")
        for e in calendar_events:
            note = f" — {e['note']}" if e.get("note") else ""
            lines.append(f"- {e['date']}: {e['title']}{note}")
        lines.append("")

    return ("\n".join(lines) + _notes_section(articles, language)
            + _sources_section(articles)
            + health_section(unhealthy_sources or [], language) + "\n")


CHAT_PACK_PREAMBLE = """# Chat pack — {date}

Paste this whole file into ChatGPT / Claude / any assistant and talk about
the news. Everything the assistant needs is below: the analytical brief,
the per-item story notes (facts, numbers, quotes) and the source links.

---

You are my research partner for today's AI and financial-markets news. Below is
my morning brief for {date}: an analytical layer written by a local model, then
"Story Notes" with the extracted facts per item, then the source list. Ground
your answers in the notes; when something is not covered by them, say so
explicitly rather than guessing, and mark your own inference as inference.
Never give me personal investment advice. Start by asking which item I want to
dig into, or suggest the two most consequential ones and why.

---

"""


def render_chat_pack(brief_content: str, day: date) -> str:
    """The brief wrapped in a ready-to-paste conversation preamble."""
    return CHAT_PACK_PREAMBLE.format(date=day.isoformat()) + brief_content


def write_brief(content: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    log.info("Briefing written to %s", path)
    return path
