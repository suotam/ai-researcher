"""Stage 1 of the synthesis: story notes per article via the fast model.

One small LLM call per selected article turns its full text into compact,
structured notes (facts, quotes, why it might matter). The quality model
then analyses those notes instead of raw article text — a much shorter
prompt, denser input, and the slow model spends its tokens on judgment,
not on retelling.

Any failure leaves the article without notes; the analysis prompt falls
back to (trimmed) raw text for that item, so this stage can never break
the run.
"""

from __future__ import annotations

import logging
from typing import Any

from ..llm.client import LLMClient, LLMError
from ..llm.prompts import build_notes_prompt

log = logging.getLogger(__name__)


def extract_notes(client: LLMClient, articles: list[dict[str, Any]], *,
                  max_text_chars: int = 4000, max_tokens: int = 600,
                  language: str = "en") -> int:
    """Fill article['notes'] in place. Returns how many articles got notes."""
    done = 0
    for article in articles:
        system, user = build_notes_prompt(
            article, max_text_chars=max_text_chars, language=language)
        try:
            notes = client.chat(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                temperature=0.2,
                max_tokens=max_tokens,
            )
        except LLMError as exc:
            log.warning("Notes failed for %s (%s) — using raw text instead",
                        article.get("ref_id"), exc)
            continue
        article["notes"] = notes.strip()
        done += 1
    log.info("Story notes written for %d/%d articles", done, len(articles))
    return done
