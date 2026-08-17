"""Prompts for the two-stage synthesis.

Stage 1 (fast model, one call per article): structured *story notes* — the
facts, numbers and quotes of each selected article, extracted from its full
text.

Stage 2 (quality model, one call): the *analytical layer* of the brief,
written from those notes — priorities, connections, what to watch, one deep
dive, one concept to learn. The model does not retell the facts; the notes
are printed below its analysis in the final brief.
"""

from __future__ import annotations

from typing import Any

LANGUAGE_NAMES = {"cs": "Czech", "en": "English"}


def _lang_line(language: str) -> str:
    return f"Write in {LANGUAGE_NAMES.get(language, 'English')}."


SYSTEM_PROMPT = """You are a senior research analyst, not a news summarizer.

Rules:
- Analyze ONLY the provided material. Never invent facts, numbers or events.
- Clearly separate facts (what sources say) from inference (your interpretation).
- If sources conflict, explicitly flag it.
- Prioritize significance over volume. Drop unimportant items.
- Treat causality carefully: do not present correlation as causation. If a
  market move's driver is not supported by the sources, say so.
- For financial markets, NEVER give personal investment advice.
- Back every claim with a source: reference items by ID as [ARTICLE_12].
- Do not invent future events without a source.
- If nothing significant happened in an area, say so. "Nothing important"
  is a valid result — do not pad the report.

Write factually, no clickbait."""

NOTES_SYSTEM = """You are a research assistant preparing story notes for a
senior analyst. You extract, you do not editorialize. Use ONLY the article
text; if a detail is not in the text, leave it out. Keep exact numbers,
names, dates and tickers. Answer in Markdown, no preamble."""


def _article_header(a: dict[str, Any]) -> str:
    coverage = a.get("duplicate_count") or 0
    cov = f", covered by {coverage + 1} outlets" if coverage else ""
    return (f"{a.get('title', '')} — {a.get('source_name', '')}, "
            f"{a.get('published_at') or 'unknown date'} "
            f"(category: {a.get('category', '')}{cov})")


def build_notes_prompt(article: dict[str, Any], *, max_text_chars: int = 4000,
                       language: str = "en") -> tuple[str, str]:
    """Stage 1: notes for ONE article. Returns (system, user)."""
    text = (article.get("raw_text") or article.get("summary") or "")[:max_text_chars]
    user = f"""Article: {_article_header(article)}
URL: {article.get('url', '')}

TEXT:
{text}

Write story notes in exactly this format ({_lang_line(language)}):

**Facts**
- 3-7 bullets: what happened, who, when, how much. Keep numbers and names exact.
**Quotes**
- 0-2 short verbatim quotes with speaker, only if they carry information. Omit the section if none.
**Why it might matter**
One or two sentences. Mark inference as inference.
**Open questions**
- 0-2 bullets: what the article does not answer. Omit if none.

Maximum 180 words. No title, no preamble."""
    return NOTES_SYSTEM, user


def _notes_block(a: dict[str, Any], max_chars: int) -> str:
    body = (a.get("notes") or "").strip()
    if not body:
        # Notes stage unavailable: fall back to raw text (trimmed harder).
        body = "TEXT: " + (a.get("raw_text") or a.get("summary") or "")[:max_chars]
    return f"[{a['ref_id']}] {_article_header(a)}\n{body[:max_chars]}\n"


def _calendar_block(events: list[dict[str, Any]]) -> str:
    if not events:
        return ""
    lines = ["", "=== CALENDAR (manually maintained, verified upcoming events) ===", ""]
    for e in events:
        note = f" — {e['note']}" if e.get("note") else ""
        lines.append(f"- {e['date']}: {e['title']} [{e.get('category', '')}]{note}")
    lines.append("")
    lines.append("Put these into the Watchlist (they are verified, no "
                 "[ARTICLE_x] reference needed).")
    return "\n".join(lines)


def build_analysis_prompt(articles: list[dict[str, Any]], *, date_str: str,
                          recent_learning_topics: list[str],
                          language: str = "en",
                          calendar_events: list[dict[str, Any]] | None = None,
                          max_notes_chars: int = 1500) -> tuple[str, str]:
    """Stage 2: the analytical layer, from story notes. Returns (system, user).
    Each article dict must contain 'ref_id' like 'ARTICLE_12' and ideally
    'notes' (stage 1 output)."""
    blocks = "\n".join(_notes_block(a, max_notes_chars) for a in articles)
    avoid = ", ".join(recent_learning_topics) if recent_learning_topics else "(none yet)"

    user = f"""Date: {date_str}

Below are analyst story notes on today's selected items (AI + financial
markets). The facts are already extracted; your job is the ANALYTICAL LAYER
of the morning brief: judge what matters, connect stories, say what to
watch. Do not retell the facts — the notes are printed under your analysis
in the final report. Every claim must cite its item as [ARTICLE_x].

Write Markdown in exactly this structure (keep the headings):

## Executive Summary
Up to 5 bullets across AI + Markets, most important first, 1-3 sentences
each: what happened and why it matters. Include the [ARTICLE_x] refs.

## What Matters Today
3-5 themes. A theme may combine several items if they are genuinely
connected; say explicitly when a connection is inference. Format:
### <Theme title>
**Why it matters** ...
**What to watch** ...
**Sources** [ARTICLE_x], [ARTICLE_y]
Cover markets as well as AI: context and possible drivers, and state it
when a driver is not supported by the sources.

## Deep Dive
At most ONE item that deserves deeper analysis: second-order effects, who
wins/loses, what would change your view. If nothing qualifies today, write
just "No deep dive today." and nothing else.

## Learn Today
Pick ONE concept related to today's items and explain it in ~5 minutes of
reading. Recently used topics to avoid: {avoid}.
The first line of this section must be exactly: LEARNING_TOPIC: <topic name>

## Watchlist
Things to watch in the coming hours/days — ONLY derived from the notes or
from the calendar below, no invented events.

Total length 450-650 words — be dense, not long. Wide coverage by several outlets ("covered by
N outlets") signals significance. If a category has nothing significant,
say so. {_lang_line(language)}

=== STORY NOTES ===

{blocks}{_calendar_block(calendar_events or [])}"""
    return SYSTEM_PROMPT, user


def build_weekly_prompt(articles: list[dict[str, Any]], *, date_str: str,
                        language: str = "en",
                        max_text_chars: int = 400) -> tuple[str, str]:
    """Weekly digest: trends and through-lines rather than day-by-day news."""
    blocks = []
    for a in articles:
        text = (a.get("raw_text") or a.get("summary") or "")[:max_text_chars]
        blocks.append(f"[{a['ref_id']}] {_article_header(a)}\nURL: {a.get('url', '')}\n"
                      f"Text: {text}\n")
    user = f"""Date: {date_str}

Below are the most significant items of the LAST 7 DAYS (AI + financial
markets). Write a weekly digest in Markdown — not a day-by-day list, but an
analysis of trends and connections across the week:

## The Week in Brief
3-5 sentences: the main story of the week.

## Trends and Connections
2-4 subsections. Each ties several items of the week into one development
(e.g. "hyperscaler capex keeps climbing", "the market reprices rates").
For each: **What happened** ... **Where it is heading** ... **Sources** [ARTICLE_x], [ARTICLE_y]

## Next Week
Only things supported by the sources.

Cite every claim as [ARTICLE_x]. If the week was thin, say so. {_lang_line(language)}

=== ITEMS ===

{chr(10).join(blocks)}"""
    return SYSTEM_PROMPT, user
