"""7 scoring dimensions. Each function returns a float 0..1 (penalties
return negative-ready raw signals; final sign is applied by `compute.py`
using yaml weights — penalty weights are negative).

Per plan v0.5 §S4 dimensions list:
    - capability_fit          : avg top-k cosine, supplied by rag/retriever
    - source_confidence       : extraction_confidence (already 0..1)
    - buying_signal           : news-driven signal (recency × keyword hit)
    - website_verification    : did we resolve an official_url
    - category_fit            : ideal-customer industries/geo overlap
    - competitor_penalty      : retrieval competitor_hits / top_k
    - bad_fit_penalty         : retrieval bad_fit_hits / top_k

All dimensions are deterministic. The Sonnet rationale call is separate and
runs only after the tier is decided.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from event_intel.cards.schema import CapabilityCards
    from event_intel.events.enrichment import EnrichedExhibitor
    from event_intel.rag.retriever import FitResult


@dataclass
class DimensionScores:
    capability_fit: float
    source_confidence: float
    buying_signal: float
    website_verification: float
    category_fit: float
    competitor_penalty: float
    bad_fit_penalty: float


def score_capability_fit(fit: "FitResult") -> float:
    return max(0.0, min(1.0, float(fit.capability_fit)))


def score_source_confidence(row: "EnrichedExhibitor") -> float:
    return max(0.0, min(1.0, float(row.extraction_confidence)))


def score_website_verification(row: "EnrichedExhibitor") -> float:
    return 1.0 if row.official_url else 0.0


def score_buying_signal(
    row: "EnrichedExhibitor", *, triggers: list[str] | None = None
) -> float:
    """Coarse signal driven by news count + trigger-keyword hit rate.

    - 0 news → 0.0
    - 1-2 news → 0.4
    - 3+ news → 0.6
    - + 0.4 bonus if any news title/snippet matches a buying-trigger keyword.

    Clamped to 1.0.
    """
    news = row.news_signals
    if not news:
        return 0.0
    base = 0.4 if len(news) <= 2 else 0.6
    if triggers:
        haystack = " ".join(
            f"{n.title} {n.snippet}".lower() for n in news
        )
        trig_lower = [t.lower() for t in triggers if t]
        hit = any(t in haystack for t in trig_lower if t)
        if hit:
            base = min(1.0, base + 0.4)
    return base


def _tokens_lower(text: str) -> set[str]:
    if not text:
        return set()
    return {t for t in re.split(r"[\s,;/]+", text.lower()) if t}


def score_category_fit(
    row: "EnrichedExhibitor", *, cards: "CapabilityCards | None"
) -> float:
    """Industries/geo overlap between exhibitor evidence and ideal_customer.

    Coarse but deterministic — uses description + news titles as the haystack
    and ideal_customer.industries + .geo as the needles. Returns:
      - 0.0 if no cards available
      - hits / (hits + 1) ⇒ asymptotes to 1.0 (single hit ≈ 0.5)
    """
    if cards is None:
        return 0.0
    ic = cards.ideal_customer
    needles_industry = _tokens_lower(", ".join(ic.industries))
    needles_geo = _tokens_lower(", ".join(ic.geo)) if ic.geo else set()
    needles_signals = _tokens_lower(", ".join(ic.company_signals))
    haystack_parts = [row.description or ""]
    for n in row.news_signals:
        haystack_parts.append(n.title or "")
        haystack_parts.append(n.snippet or "")
    haystack = " ".join(haystack_parts).lower()

    def _count(needles: set[str]) -> int:
        return sum(1 for n in needles if n and n in haystack)

    hits = _count(needles_industry) + _count(needles_geo) + _count(needles_signals)
    if hits == 0:
        return 0.0
    return hits / (hits + 1.0)


def score_competitor_penalty(fit: "FitResult", *, top_k: int) -> float:
    """Fraction of top-k retrieval hits that landed on competitor chunks."""
    if top_k <= 0:
        return 0.0
    return max(0.0, min(1.0, fit.competitor_hits / top_k))


def score_bad_fit_penalty(fit: "FitResult", *, top_k: int) -> float:
    """Fraction of top-k retrieval hits that landed on bad_fit chunks."""
    if top_k <= 0:
        return 0.0
    return max(0.0, min(1.0, fit.bad_fit_hits / top_k))


def compute_dimensions(
    row: "EnrichedExhibitor",
    fit: "FitResult",
    *,
    cards: "CapabilityCards | None",
    top_k: int,
) -> DimensionScores:
    triggers = [t.signal for t in cards.buying_triggers] if cards else []
    return DimensionScores(
        capability_fit=score_capability_fit(fit),
        source_confidence=score_source_confidence(row),
        buying_signal=score_buying_signal(row, triggers=triggers),
        website_verification=score_website_verification(row),
        category_fit=score_category_fit(row, cards=cards),
        competitor_penalty=score_competitor_penalty(fit, top_k=top_k),
        bad_fit_penalty=score_bad_fit_penalty(fit, top_k=top_k),
    )
