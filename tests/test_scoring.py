"""S4 — scoring tests covering dimensions / rules / compute.

Evidence floor matrix (Contract #9):
    - (no url, no news) → floor 0 → max tier B
    - (url only OR news only) → floor 1 → max tier A
    - (url + news) → floor 2 → S/A possible
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from event_intel.cards.validator import load_and_validate
from event_intel.errors import ErrorCode, MCPError
from event_intel.events.enrichment import EnrichedExhibitor, NewsSignal
from event_intel.rag.retriever import FitResult
from event_intel.scoring.compute import score_exhibitors
from event_intel.scoring.dimensions import (
    score_buying_signal,
    score_capability_fit,
    score_category_fit,
    score_website_verification,
)
from event_intel.scoring.rules import compute_evidence_floor, decide_tier


def _config():
    return {
        "scoring": {
            "weights": {
                "capability_fit": 0.30,
                "source_confidence": 0.15,
                "buying_signal": 0.15,
                "website_verification": 0.10,
                "category_fit": 0.15,
                "competitor_penalty": -0.10,
                "bad_fit_penalty": -0.10,
            },
            # Test-only thresholds (defaults.yaml uses 7.5/6/4/0). Lowered here
            # so the floor-cap behavior is what differentiates the three rows
            # in the floor-matrix test, not the score.
            "tier_rules": {
                "S": {"min_final_score": 5.5, "evidence_floor_min": 2},
                "A": {"min_final_score": 4.0, "evidence_floor_min": 1},
                "B": {"min_final_score": 2.0, "evidence_floor_min": 0},
                "C": {"min_final_score": 0.0, "evidence_floor_min": 0},
            },
        },
    }


def _row(name, **kw):
    return EnrichedExhibitor(
        name=name,
        source_snippet=kw.get("snippet", "evidence snippet for " + name),
        url=kw.get("url"),
        official_url=kw.get("official_url"),
        description=kw.get("description", "auto-ish ADAS automotive perception"),
        news_signals=kw.get("news_signals", []),
        extraction_confidence=kw.get("extraction_confidence", 1.0),
    )


def _fit(name, **kw):
    return FitResult(
        name=name,
        capability_fit=kw.get("capability_fit", 0.8),
        top_hits=kw.get("top_hits", []),
        capability_fit_breakdown=kw.get("breakdown", {"Cap A": 3, "Cap B": 1}),
        competitor_hits=kw.get("competitor_hits", 0),
        bad_fit_hits=kw.get("bad_fit_hits", 0),
    )


# ---------- dimensions ----------


def test_evidence_floor_matrix():
    assert compute_evidence_floor(has_official_url=False, has_news_signals=False) == 0
    assert compute_evidence_floor(has_official_url=True, has_news_signals=False) == 1
    assert compute_evidence_floor(has_official_url=False, has_news_signals=True) == 1
    assert compute_evidence_floor(has_official_url=True, has_news_signals=True) == 2


def test_website_verification_is_binary():
    assert score_website_verification(_row("X", official_url="https://x")) == 1.0
    assert score_website_verification(_row("X", official_url=None)) == 0.0


def test_buying_signal_news_count_brackets():
    # News must mention the company for the full bracket (name-match relevance).
    assert score_buying_signal(_row("Acme")) == 0.0
    one = _row("Acme", news_signals=[NewsSignal(title="Acme launches", url="u", snippet="s")])
    three = _row(
        "Acme",
        news_signals=[NewsSignal(title=f"Acme update {i}", url="u", snippet="s") for i in range(3)],
    )
    assert score_buying_signal(one) == 0.4
    assert score_buying_signal(three) == 0.6


def test_buying_signal_downweights_generic_news():
    """A pile of articles that never name the company is a weak signal — base halved."""
    matched = _row("Acme", news_signals=[NewsSignal("Acme raises Series B", "u", "s")])
    generic = _row("Acme", news_signals=[NewsSignal("Some unrelated headline", "u", "s")])
    assert score_buying_signal(matched) == 0.4
    assert score_buying_signal(generic) == 0.2


def test_buying_signal_recency_bonus_and_naive_timestamp_safe():
    """Recent name-matched news outranks stale; a naive date-only published_at
    must NOT raise TypeError against the UTC-aware reference_date (round-2 #1)."""
    from datetime import datetime, timezone

    ref = datetime(2026, 6, 1, tzinfo=timezone.utc)
    recent = _row(
        "Acme",
        news_signals=[NewsSignal("Acme news", "u", "s", published_at="2026-05-30")],  # naive date-only
    )
    stale = _row(
        "Acme",
        news_signals=[NewsSignal("Acme news", "u", "s", published_at="2024-01-01")],
    )
    r_recent = score_buying_signal(recent, reference_date=ref)
    r_stale = score_buying_signal(stale, reference_date=ref)
    assert r_recent > r_stale
    assert r_stale == pytest.approx(0.4, abs=0.05)
    # future timestamp contributes no recency bonus, no crash.
    future = _row("Acme", news_signals=[NewsSignal("Acme news", "u", "s", published_at="2099-01-01")])
    assert score_buying_signal(future, reference_date=ref) == 0.4


def test_buying_signal_trigger_keyword_bonus():
    row = _row("X", news_signals=[NewsSignal(
        title="X partners with auto OEM on ADAS Level 3 program",
        url="u", snippet="…",
    )])
    base = score_buying_signal(row)
    boosted = score_buying_signal(row, triggers=["ADAS Level 3"])
    assert boosted > base
    assert boosted <= 1.0


def test_category_fit_returns_zero_without_cards(repo_root):
    assert score_category_fit(_row("X"), cards=None) == 0.0


def test_category_fit_increases_with_industry_overlap(repo_root):
    cards = load_and_validate(repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml")
    matching = _row(
        "X",
        description="automotive tier-1 perception SoC for ADAS Level 3",
        news_signals=[NewsSignal(title="industrial robotics deal", url="", snippet="")],
    )
    nonmatch = _row(
        "X",
        description="completely unrelated catering services for events",
        news_signals=[],
    )
    assert score_category_fit(matching, cards=cards) > score_category_fit(nonmatch, cards=cards)


def test_category_fit_no_substring_or_stopword_false_positive(repo_root):
    """Token-boundary match: geo 'US' must NOT match 'business', and stopwords
    must not match at all. Under the old `needle in haystack` substring logic
    this row scored > 0 from 'us'⊂'business'; now it must be 0."""
    cards = load_and_validate(repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml")
    row = _row(
        "X",
        description="A business platform for retail chains in our local region",
        news_signals=[],
    )
    assert score_category_fit(row, cards=cards) == 0.0


def test_category_fit_matches_short_acronym_token(repo_root):
    """Whitelisted short tokens (geo 'US', tech 'AR'/'VR') still match as whole
    tokens — a blanket len<3 drop would have deleted them."""
    cards = load_and_validate(repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml")
    row = _row("X", description="AR and VR devices built in the US", news_signals=[])
    assert score_category_fit(row, cards=cards) > 0.0


# ---------- rules ----------


def test_decide_tier_floor_caps_tier():
    rules = _config()["scoring"]["tier_rules"]
    # Same high score, different floors → tier moves.
    s_score = 9.0
    assert decide_tier(final_score=s_score, evidence_floor=2, tier_rules=rules).tier == "S"
    assert decide_tier(final_score=s_score, evidence_floor=1, tier_rules=rules).tier == "A"
    assert decide_tier(final_score=s_score, evidence_floor=0, tier_rules=rules).tier == "B"


def test_decide_tier_picks_highest_satisfied():
    rules = _config()["scoring"]["tier_rules"]
    # Test-config thresholds: S=5.5, A=4.0, B=2.0, C=0.0.
    assert decide_tier(final_score=5.0, evidence_floor=2, tier_rules=rules).tier == "A"
    assert decide_tier(final_score=3.0, evidence_floor=2, tier_rules=rules).tier == "B"
    assert decide_tier(final_score=0.5, evidence_floor=2, tier_rules=rules).tier == "C"


# ---------- compute (integration) ----------


def test_score_exhibitors_evidence_floor_caps_full_pipeline():
    """Three rows: same dimensions, different evidence floors."""
    rows = [
        _row("Both", official_url="https://b", news_signals=[NewsSignal("t", "u", "s")]),
        _row("UrlOnly", official_url="https://u"),
        _row("NoneEvidence"),
    ]
    fits = [_fit(r.name, capability_fit=0.95) for r in rows]
    summary = score_exhibitors(
        enriched=rows, fit_results=fits, cards=None, config=_config(), top_k=5,
    )
    by_name = {s.name: s for s in summary.rows}
    # All three have identical scoring inputs except floor → tiers differ.
    assert by_name["Both"].tier == "S"
    assert by_name["UrlOnly"].tier == "A"
    assert by_name["NoneEvidence"].tier == "B"
    assert summary.tier_counts == {"S": 1, "A": 1, "B": 1, "C": 0}


def test_bad_fit_and_competitor_penalty_drops_tier():
    row = _row("Bad", official_url="https://x", news_signals=[NewsSignal("t", "u", "s")])
    # Strong capability_fit but heavy competitor + bad_fit hits.
    fit_clean = _fit("Bad", capability_fit=0.95, competitor_hits=0, bad_fit_hits=0)
    fit_dirty = _fit("Bad", capability_fit=0.95, competitor_hits=5, bad_fit_hits=5)
    clean = score_exhibitors(
        enriched=[row], fit_results=[fit_clean], cards=None, config=_config(), top_k=5,
    ).rows[0]
    dirty = score_exhibitors(
        enriched=[row], fit_results=[fit_dirty], cards=None, config=_config(), top_k=5,
    ).rows[0]
    assert dirty.final_score < clean.final_score
    # Clean lands in S, dirty drops at least one tier.
    tier_order = ["C", "B", "A", "S"]
    assert tier_order.index(dirty.tier) < tier_order.index(clean.tier)


def test_score_exhibitors_runs_rationale_only_for_target_tiers():
    """LLM bounded use — rationale only for S/A, not B/C."""
    rows = [
        _row("HighTier", official_url="https://h", news_signals=[NewsSignal("t", "u", "s")]),
        _row("LowTier"),
    ]
    fits = [_fit("HighTier", capability_fit=0.95), _fit("LowTier", capability_fit=0.20)]

    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def chat_once(self, *, system, user, max_tokens, temperature):
            self.calls += 1
            from dataclasses import dataclass

            @dataclass
            class R:
                text: str
                usage: dict
                model: str = "fake"
                stop_reason: str | None = None

            return R(
                text="RATIONALE: Strong cap fit + verified site.\nANGLE: Lead with NPU compile pain.",
                usage={"input_tokens": 1, "output_tokens": 1},
            )

    llm = FakeLLM()
    summary = score_exhibitors(
        enriched=rows, fit_results=fits, cards=None, config=_config(), top_k=5,
        llm_provider=llm, rationale_lang="en", rationale_for_tiers=("S", "A"),
    )
    assert llm.calls == sum(1 for s in summary.rows if s.tier in ("S", "A"))
    # The high-tier row got rationale + angle populated.
    high = next(s for s in summary.rows if s.name == "HighTier")
    assert high.rationale and "cap fit" in high.rationale.lower()
    assert high.angle and "NPU" in high.angle


def test_score_exhibitors_length_mismatch_raises_internal():
    with pytest.raises(MCPError) as exc_info:
        score_exhibitors(
            enriched=[_row("A")],
            fit_results=[_fit("A"), _fit("B")],
            cards=None, config=_config(), top_k=5,
        )
    assert exc_info.value.error_code == ErrorCode.INTERNAL
