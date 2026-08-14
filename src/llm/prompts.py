"""Prompts for the Glimmer 30B synthesis step."""

from __future__ import annotations

from typing import Any

SYSTEM_PROMPT_CS = """Jsi senior research analyst, ne sumarizátor zpráv.

Pravidla:
- Analyzuj POUZE dodané zdroje. Nikdy si nevymýšlej fakta, čísla ani události.
- Jasně rozlišuj fakta (co zdroje říkají) od inference (tvoje interpretace).
- Pokud si zdroje protiřečí, výslovně na to upozorni.
- Prioritizuj význam před množstvím. Méně důležité položky vynech.
- S kauzalitou zacházej opatrně: nevydávej korelaci automaticky za příčinu.
  Pokud driver pohybu trhu není ve zdrojích doložený, napiš to.
- U finančních trhů NIKDY nedávej osobní investiční doporučení.
- Každé tvrzení opři o zdroj: odkazuj na položky jejich ID ve tvaru [ARTICLE_12].
- Nevymýšlej budoucí události bez zdroje.
- Pokud se v některé oblasti nestalo nic významného, napiš to. "Nic důležitého"
  je validní výsledek — negeneruj obsah jen proto, aby report vypadal plný.

Piš česky, věcně, bez clickbaitu."""

SYSTEM_PROMPT_EN = """You are a senior research analyst, not a news summarizer.

Rules:
- Analyze ONLY the provided sources. Never invent facts, numbers or events.
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


def _article_block(ref_id: str, article: dict[str, Any], max_text_chars: int = 600) -> str:
    text = (article.get("raw_text") or article.get("summary") or "")[:max_text_chars]
    return (
        f"[{ref_id}]\n"
        f"Title: {article.get('title', '')}\n"
        f"Source: {article.get('source_name', '')} (category: {article.get('category', '')})\n"
        f"URL: {article.get('url', '')}\n"
        f"Published: {article.get('published_at') or 'unknown'}\n"
        f"Text: {text}\n"
    )


def _coverage_line(article: dict[str, Any]) -> str:
    count = article.get("duplicate_count") or 0
    if not count:
        return ""
    outlets = article.get("coverage_sources") or []
    detail = f" ({', '.join(outlets[:5])})" if outlets else ""
    return f"Coverage: reported by {count + 1} outlets{detail}\n"


def _calendar_block(events: list[dict[str, Any]]) -> str:
    if not events:
        return ""
    lines = ["", "=== KALENDÁŘ (ručně udržovaný, ověřené nadcházející události) ===", ""]
    for e in events:
        note = f" — {e['note']}" if e.get("note") else ""
        lines.append(f"- {e['date']}: {e['title']} [{e.get('category', '')}]{note}")
    lines.append("")
    lines.append("Tyto události zařaď do sekce Watchlist (jsou ověřené, "
                 "smíš je použít bez [ARTICLE_x] reference).")
    return "\n".join(lines)


def build_briefing_prompt(articles: list[dict[str, Any]], *, date_str: str,
                          recent_learning_topics: list[str],
                          language: str = "cs",
                          calendar_events: list[dict[str, Any]] | None = None,
                          max_text_chars: int = 600) -> tuple[str, str]:
    """Return (system_prompt, user_prompt). Each article dict must contain a
    'ref_id' key like 'ARTICLE_12' (its DB id)."""
    system = SYSTEM_PROMPT_CS if language == "cs" else SYSTEM_PROMPT_EN

    blocks = "\n".join(
        _article_block(a["ref_id"], a, max_text_chars) + _coverage_line(a)
        for a in articles
    )
    avoid = ", ".join(recent_learning_topics) if recent_learning_topics else "(zatím žádná)"

    user = f"""Datum: {date_str}

Níže jsou vybrané položky z posledního sběru (AI + finanční trhy). Vytvoř z nich
ranní briefing v Markdownu přesně v této struktuře (nadpisy zachovej):

## Executive Summary
Maximálně 5 nejdůležitějších věcí napříč AI + Markets, každá 1-3 věty.

## AI
Pro každou důležitou událost podsekce:
### <Název události>
**Co se stalo** ...
**Proč je to důležité** ...
**Co sledovat dál** ...
**Zdroje** [ARTICLE_x], [ARTICLE_y]

## Markets
Stejný formát jako AI. Nejen ceny — vysvětluj kontext a možné drivery.
Pokud driver není ve zdrojích spolehlivě doložený, výslovně to uveď.

## Deep Dive
Maximálně JEDNA událost, která si zaslouží hlubší analýzu. Pokud si ji dnes
žádná nezaslouží, napiš jen "Dnes bez deep dive." a nic nevymýšlej.

## Dnes se nauč
Vyber JEDEN koncept související s dnešními událostmi a vysvětli ho
(cca 5 minut čtení). Nedávno použitá témata, kterým se vyhni: {avoid}.
Na první řádek sekce napiš přesně: LEARNING_TOPIC: <název tématu>

## Watchlist
Několik věcí ke sledování v dalších hodinách/dnech — POUZE odvozené
z dodaných zdrojů nebo z kalendáře níže, žádné vymyšlené budoucí eventy.

Důležité: každé tvrzení odkazuj na zdrojové položky pomocí [ARTICLE_x].
Pokud v některé kategorii není nic významného, napiš to.
U položek s "Coverage" údajem: široké pokrytí více médii signalizuje
významnost události.

=== POLOŽKY ===

{blocks}{_calendar_block(calendar_events or [])}"""
    return system, user


def build_weekly_prompt(articles: list[dict[str, Any]], *, date_str: str,
                        language: str = "cs",
                        max_text_chars: int = 400) -> tuple[str, str]:
    """Weekly digest: trends and through-lines rather than day-by-day news."""
    system = SYSTEM_PROMPT_CS if language == "cs" else SYSTEM_PROMPT_EN
    blocks = "\n".join(
        _article_block(a["ref_id"], a, max_text_chars) + _coverage_line(a)
        for a in articles
    )
    user = f"""Datum: {date_str}

Níže jsou nejvýznamnější položky z POSLEDNÍCH 7 DNÍ (AI + finanční trhy).
Vytvoř týdenní přehled v Markdownu — ne výčet zpráv den po dni, ale analýzu
trendů a souvislostí napříč týdnem:

## Týden v kostce
3-5 vět: co byl hlavní příběh týdne.

## Trendy a souvislosti
2-4 podsekce. Každá spojuje více událostí týdne do jednoho vývoje/tématu
(např. "capex hyperscalerů dál roste", "trh přeceňuje sazby"). U každé:
**Co se dělo** ... **Kam to směřuje** ... **Zdroje** [ARTICLE_x], [ARTICLE_y]

## Co příští týden
Jen věci doložené ve zdrojích.

Každé tvrzení odkazuj pomocí [ARTICLE_x]. Pokud byl týden chudý na události,
řekni to.

=== POLOŽKY ===

{blocks}"""
    return system, user
