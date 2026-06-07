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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from event_intel.events.evidence import mentions_name, name_tokens
from event_intel.scoring.cjk import CjkSpec, cjk_bigrams, resolve_segmenter
from event_intel.timeutil import recency_weight

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


def score_capability_fit(fit: FitResult) -> float:
    return max(0.0, min(1.0, float(fit.capability_fit)))


def score_source_confidence(row: EnrichedExhibitor) -> float:
    return max(0.0, min(1.0, float(row.extraction_confidence)))


def score_website_verification(row: EnrichedExhibitor) -> float:
    return 1.0 if row.official_url else 0.0


def score_buying_signal(
    row: EnrichedExhibitor,
    *,
    triggers: list[str] | None = None,
    reference_date: datetime | None = None,
    half_life_days: float = 180.0,
) -> float:
    """News-driven signal: count bracket × company-name relevance + recency bonus.

    - 0 news → 0.0
    - 1-2 news → base 0.4; 3+ news → base 0.6
    - generic news (no company-name match in any title/snippet) halves the base
      — a pile of unrelated articles is a weak buying signal (review round-2 #1).
    - + up to 0.3 recency bonus from the freshest name-matched article
      (exponential half-life; missing/future published_at contributes 0).
    - + 0.4 bonus if any news matches a buying-trigger keyword.

    Clamped to 1.0. published_at normalization is handled in timeutil so a naive
    timestamp never collides with the UTC-aware reference_date.
    """
    news = row.news_signals
    if not news:
        return 0.0
    base = 0.4 if len(news) <= 2 else 0.6

    # Same relevance test as the floor evidence gate (review round-3 #2): the old
    # substring matcher let "data"⊂"database" / "meta"⊂"metadata" count unrelated
    # articles. evidence.mentions_name is token-boundary + generic-token guarded.
    tokens = name_tokens(row.name)
    matched = [
        n for n in news if mentions_name(f"{n.title or ''} {n.snippet or ''}", tokens)
    ]
    if not matched:
        base *= 0.5

    # Recency AND trigger bonuses come ONLY from name-matched news (review r4):
    # the relevance gate must govern the whole signal, not just the base. Earlier,
    # `matched or news` + an all-news trigger scan let recent/trigger-bearing but
    # UNRELATED articles add +0.3 (+0.4) on top of the halved base.
    ref = reference_date or datetime.now(UTC)
    rec = max(
        (
            recency_weight(n.published_at, reference_date=ref, half_life_days=half_life_days)
            for n in matched
        ),
        default=0.0,
    )
    base = min(1.0, base + 0.3 * rec)

    if triggers and matched:
        haystack = " ".join(f"{n.title} {n.snippet}".lower() for n in matched)
        trig_lower = [t.lower() for t in triggers if t]
        if any(t in haystack for t in trig_lower if t):
            base = min(1.0, base + 0.4)
    return min(1.0, base)


# Tokenizer for category_fit only. Splits on whitespace + common punctuation
# (incl. hyphens/parens) so "generative-AI" → {"generative", "ai"}.
_CATEGORY_TOKEN_SPLIT = re.compile(r"[\s,;/()\[\]\-–—.|:&]+")

# CJK ranges (Hiragana/Katakana, CJK Unified incl. ext-A, Hangul syllables, CJK
# compat). ASCII whitespace/punctuation never separates CJK words, so a run like
# "삼성전자" arrives as one token; we emit character bigrams ("삼성","성전","전자")
# so token-boundary overlap works for Korean/Japanese/Chinese without a heavy
# morphological segmenter (review round-2: rule-based, cold-start safe).
_CJK_CHAR = re.compile(r"[぀-ヿ㐀-鿿가-힯豈-﫿]")


def _expand_token(tok: str, *, cjk_segment: Callable[[str], set[str]] = cjk_bigrams) -> set[str]:
    """Split a rough token into ASCII alnum runs (kept whole) + CJK runs (→ char
    bigrams). 'ai반도체' → {'ai', '반도', '도체'}."""
    out: set[str] = set()
    cjk: list[str] = []
    other: list[str] = []
    for ch in tok:
        if _CJK_CHAR.match(ch):
            if other:
                out.add("".join(other))
                other = []
            cjk.append(ch)
        else:
            if cjk:
                out |= cjk_segment("".join(cjk))
                cjk = []
            other.append(ch)
    if other:
        out.add("".join(other))
    if cjk:
        out |= cjk_segment("".join(cjk))
    return {t for t in out if t}

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


def _tokens_lower(
    text: str, *, cjk_segment: Callable[[str], set[str]] = cjk_bigrams
) -> set[str]:
    if not text:
        return set()
    out: set[str] = set()
    for raw in _CATEGORY_TOKEN_SPLIT.split(text.lower()):
        if raw:
            out |= _expand_token(raw, cjk_segment=cjk_segment)
    return out


def _category_needles(
    *groups: list[str], cjk_segment: Callable[[str], set[str]] = cjk_bigrams
) -> set[str]:
    """Build the matchable needle set: drop stopwords; drop <3-char tokens unless
    whitelisted (acronyms/geo) — but always keep CJK bigrams (length 2)."""
    needles: set[str] = set()
    for group in groups:
        for tok in _tokens_lower(", ".join(group), cjk_segment=cjk_segment):
            if tok in _CATEGORY_STOPWORDS:
                continue
            if _CJK_CHAR.search(tok):
                needles.add(tok)
            elif len(tok) >= 3 or tok in _SHORT_TOKEN_WHITELIST:
                needles.add(tok)
    return needles


def score_category_fit(
    row: EnrichedExhibitor, *, cards: CapabilityCards | None, cjk: CjkSpec | None = None
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
    needle_groups = [ic.industries, ic.company_signals, ic.geo or []]
    haystack_parts = [row.description or ""]
    for n in row.news_signals:
        haystack_parts.append(n.title or "")
        haystack_parts.append(n.snippet or "")
    haystack = " ".join(haystack_parts)
    # Resolve ONE segmenter for this call (over the combined needle+haystack
    # sample, so `auto` language detection is consistent) and use it for BOTH
    # sides — symmetry is what makes morphological matching eliminate the bigram
    # false-overlaps (Phase 18W P2-4). Default (cjk=None) → char-bigrams.
    sample = haystack + " " + " ".join(t for g in needle_groups for t in g)
    seg = resolve_segmenter(cjk, sample=sample)
    needles = _category_needles(*needle_groups, cjk_segment=seg)
    if not needles:
        return 0.0
    # Token-boundary match via set intersection — NOT substring. The old
    # `needle in haystack` matched "us" inside "business", "ai" inside "chair",
    # and every stopword, inflating category_fit for unrelated companies.
    hay_tokens = _tokens_lower(haystack, cjk_segment=seg)
    hits = len(needles & hay_tokens)
    if hits == 0:
        return 0.0
    return hits / (hits + 1.0)


def score_competitor_penalty(fit: FitResult, *, threshold: float = 0.0) -> float:
    """Penalty driven by the MAX competitor-chunk similarity, gated by threshold.

    Count-based penalties saturate: a competitor-only retrieval pool returns all
    competitors, so a count would flag every company. Max similarity instead
    fires only when a chunk is genuinely close to a competitor (review r2 #1).
    Below `threshold` → 0.0 (coincidental neighbor, no penalty).
    """
    sim = max(0.0, min(1.0, float(getattr(fit, "competitor_similarity", 0.0) or 0.0)))
    return sim if sim >= threshold else 0.0


def score_bad_fit_penalty(fit: FitResult, *, threshold: float = 0.0) -> float:
    """Penalty driven by the MAX bad_fit-chunk similarity, gated by threshold."""
    sim = max(0.0, min(1.0, float(getattr(fit, "bad_fit_similarity", 0.0) or 0.0)))
    return sim if sim >= threshold else 0.0


def compute_dimensions(
    row: EnrichedExhibitor,
    fit: FitResult,
    *,
    cards: CapabilityCards | None,
    top_k: int,
    reference_date: datetime | None = None,
    half_life_days: float = 180.0,
    negative_sim_threshold: float = 0.0,
    cjk: CjkSpec | None = None,
) -> DimensionScores:
    triggers = [t.signal for t in cards.buying_triggers] if cards else []
    return DimensionScores(
        capability_fit=score_capability_fit(fit),
        source_confidence=score_source_confidence(row),
        buying_signal=score_buying_signal(
            row,
            triggers=triggers,
            reference_date=reference_date,
            half_life_days=half_life_days,
        ),
        website_verification=score_website_verification(row),
        category_fit=score_category_fit(row, cards=cards, cjk=cjk),
        competitor_penalty=score_competitor_penalty(fit, threshold=negative_sim_threshold),
        bad_fit_penalty=score_bad_fit_penalty(fit, threshold=negative_sim_threshold),
    )
