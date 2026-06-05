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


# Tokenizer for category_fit only. Splits on whitespace + common punctuation
# (incl. hyphens/parens) so "generative-AI" → {"generative", "ai"}.
_CATEGORY_TOKEN_SPLIT = re.compile(r"[\s,;/()\[\]\-–—.|:&]+")

# Dropped regardless of length — function words that matched everything under
# the old substring logic.
_CATEGORY_STOPWORDS = {
    "a", "an", "and", "or", "the", "for", "of", "to", "in", "on", "with",
    "by", "at", "as", "is", "are", "be", "this", "that", "from", "into", "per",
}
# Short (<3 char) tokens are dropped UNLESS whitelisted — keeps meaningful tech
# and geo acronyms (review #2 P2-5: a blanket len<3 cut would delete these).
_SHORT_TOKEN_WHITELIST = {
    "ai", "ml", "db", "bi", "ar", "vr", "xr", "5g", "6g", "io", "os", "ui",
    "ux", "us", "eu", "kr", "jp", "cn", "uk", "de", "fr", "sg",
}


def _tokens_lower(text: str) -> set[str]:
    if not text:
        return set()
    return {t for t in _CATEGORY_TOKEN_SPLIT.split(text.lower()) if t}


def _category_needles(*groups: list[str]) -> set[str]:
    """Build the matchable needle set: drop stopwords; drop <3-char tokens
    unless whitelisted (acronyms/geo)."""
    needles: set[str] = set()
    for group in groups:
        for tok in _tokens_lower(", ".join(group)):
            if tok in _CATEGORY_STOPWORDS:
                continue
            if len(tok) >= 3 or tok in _SHORT_TOKEN_WHITELIST:
                needles.add(tok)
    return needles


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
    needles = _category_needles(ic.industries, ic.company_signals, ic.geo or [])
    if not needles:
        return 0.0
    haystack_parts = [row.description or ""]
    for n in row.news_signals:
        haystack_parts.append(n.title or "")
        haystack_parts.append(n.snippet or "")
    # Token-boundary match via set intersection — NOT substring. The old
    # `needle in haystack` matched "us" inside "business", "ai" inside "chair",
    # and every stopword, inflating category_fit for unrelated companies.
    hay_tokens = _tokens_lower(" ".join(haystack_parts))
    hits = len(needles & hay_tokens)
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
