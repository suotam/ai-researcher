"""Second-layer relevance ranking via one cheap LLM call.

Keyword scoring is fast but blind — it cannot tell that an article with
great keywords is old news or fluff. This layer shows the LLM a compact
list (id + title + one line) of the top candidates and asks for the most
significant subset, ordered. One call, small prompt, small output.

Any failure (server down, malformed output) falls back to the cheap
ranking order — this layer can only ever *improve* selection, never
break the run.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..llm.client import LLMClient, LLMError

log = logging.getLogger(__name__)

RERANK_SYSTEM = """You are a strict research editor for a daily AI + financial
markets briefing. You receive candidate items and pick only the truly
significant ones. Prefer: real events over commentary, primary sources over
aggregators, market-moving news, frontier-model and AI-infrastructure news.
Reject: listicles, promos, minor product updates, old news, hype.
Respond with JSON only."""


def _build_prompt(candidates: list[dict[str, Any]], top_n: int) -> str:
    lines = []
    for c in candidates:
        summary = (c.get("summary") or "")[:150]
        coverage = c.get("duplicate_count") or 0
        cov = f" | covered by {coverage + 1} outlets" if coverage else ""
        lines.append(
            f"id={c['id']} [{c.get('category')}] score={c.get('relevance_score')}{cov}\n"
            f"  {c.get('title')}\n  {summary}"
        )
    items = "\n".join(lines)
    return (
        f"Pick the up to {top_n} most significant items for today's briefing, "
        "most important first. It is fine to pick fewer if little happened.\n"
        'Answer with JSON only, no prose: {"selected": [id, id, ...]}\n\n'
        f"CANDIDATES:\n{items}"
    )


def _parse_ids(output: str) -> list[int] | None:
    match = re.search(r"\{[^{}]*\}", output or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        ids = data.get("selected")
        if isinstance(ids, list) and all(isinstance(i, int) for i in ids):
            return ids
    except (ValueError, AttributeError):
        pass
    return None


def llm_rerank(client: LLMClient, candidates: list[dict[str, Any]], *,
               top_n: int, max_tokens: int = 3500) -> list[dict[str, Any]] | None:
    """Return candidates reordered/filtered by the LLM, or None on failure."""
    if not candidates:
        return []
    prompt = _build_prompt(candidates, top_n)
    try:
        output = client.chat(
            [{"role": "system", "content": RERANK_SYSTEM},
             {"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=max_tokens,  # reasoning models think before answering
        )
    except LLMError as exc:
        log.warning("LLM rerank failed (%s) — falling back to cheap ranking", exc)
        return None
    ids = _parse_ids(output)
    if ids is None:
        log.warning("LLM rerank returned unparseable output — falling back "
                    "to cheap ranking")
        return None
    by_id = {c["id"]: c for c in candidates}
    reranked = [by_id[i] for i in ids if i in by_id][:top_n]
    if not reranked:
        log.warning("LLM rerank selected nothing valid — falling back")
        return None
    log.info("LLM rerank picked %d of %d candidates", len(reranked), len(candidates))
    return reranked
