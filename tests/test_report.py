"""S5 — report rendering tests.

Covers tier_list.md 6-section structure, S/A floor invariant guard, needs_review
isolation, tier_list.yaml round-trip, product_brief.md export, en/ko switch.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from event_intel.cards.validator import load_and_validate
from event_intel.events.enrichment import EnrichedExhibitor, NewsSignal
from event_intel.rag.retriever import FitResult
from event_intel.report.brief_export import render_product_brief_md
from event_intel.report.tier_list_md import ReportContext, render_tier_list_md
from event_intel.report.tier_list_yaml import (
    REPORT_SCHEMA_VERSION,
    build_tier_list_payload,
    dump_tier_list_yaml,
    load_tier_list_yaml,
)
from event_intel.scoring.compute import ScoredExhibitor, ScoringSummary
from event_intel.scoring.dimensions import DimensionScores


def _dims(**kw):
    base = dict(
        capability_fit=0.9, source_confidence=1.0, buying_signal=0.6,
        website_verification=1.0, category_fit=0.5,
        competitor_penalty=0.0, bad_fit_penalty=0.0,
    )
    base.update(kw)
    return DimensionScores(**base)


def _scored(name, tier, *, score, floor, **row_kw):
    news = row_kw.pop("news_signals", [])
    if floor >= 1 and not row_kw.get("official_url") and not news:
        # Tests that pass floor=1 mean "one of url/news is present"; default to url.
        row_kw["official_url"] = "https://example.com/" + name.lower()
    if floor == 2 and not news:
        news = [NewsSignal(title=f"{name} news", url="https://news/x", snippet="news")]
    row = EnrichedExhibitor(
        name=name,
        source_snippet=row_kw.pop("snippet", f"evidence snippet for {name} that is long enough"),
        official_url=row_kw.get("official_url"),
        description=row_kw.get("description"),
        news_signals=news,
    )
    fit = FitResult(
        name=name, capability_fit=0.85, top_hits=[],
        capability_fit_breakdown={"Cap A": 3, "Cap B": 1},
    )
    return ScoredExhibitor(
        name=name, tier=tier, final_score=score, evidence_floor=floor,
        dimensions=_dims(), weights_used={}, tier_reasons=[],
        rationale=row_kw.get("rationale"), angle=row_kw.get("angle"),
        row=row, fit=fit,
    )


def _summary(*scored: ScoredExhibitor) -> ScoringSummary:
    counts = {"S": 0, "A": 0, "B": 0, "C": 0}
    for s in scored:
        counts[s.tier] = counts.get(s.tier, 0) + 1
    return ScoringSummary(rows=list(scored), tier_counts=counts, rationale_calls=0)


def _ctx(**kw):
    base = dict(
        workspace_id="acme", event_name="Sample Expo", event_slug="sample_expo",
        lang="en", generated_at=datetime(2026, 5, 28, 12, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return ReportContext(**base)


# ---------- tier_list_md ----------


def test_tier_list_md_has_all_6_sections():
    summary = _summary(
        _scored("S1", "S", score=8.0, floor=2, rationale="Strong fit", angle="Lead with NPU"),
        _scored("A1", "A", score=6.5, floor=1),
        _scored("B1", "B", score=4.5, floor=0),
        _scored("C1", "C", score=1.0, floor=0),
    )
    needs_review = [EnrichedExhibitor(name="NR1", source_snippet="iffy snippet",
                                       enrichment_warnings=["low confidence"])]
    md = render_tier_list_md(summary=summary, needs_review=needs_review, context=_ctx())

    # 6 sections by ## heading: Tier S, Tier A, Tier B, Tier C, Needs Review
    # (plus a # event header — that's the top H1).
    for header in ("# Sample Expo", "## Tier S", "## Tier A", "## Tier B",
                   "## Tier C", "## Needs Review"):
        assert header in md, f"missing section: {header!r}\n--- md ---\n{md}"
    # Rationale + angle surfaced for S row.
    assert "Strong fit" in md
    assert "Lead with NPU" in md
    # Summary line includes counts.
    assert "S 1 · A 1 · B 1 · C 1 · needs-review 1" in md


def test_tier_list_md_renders_floor_invariant_safe():
    """S/A rows by construction have floor >= 1; renderer asserts to catch
    misconfigured tier_rules that let weak rows leak in."""
    bad = ScoredExhibitor(
        name="Weak", tier="S", final_score=9.0, evidence_floor=0,
        dimensions=_dims(), weights_used={}, tier_reasons=[],
        row=EnrichedExhibitor(name="Weak", source_snippet="snippet only"),
        fit=FitResult(name="Weak", capability_fit=0.9, top_hits=[]),
    )
    summary = _summary(bad)
    with pytest.raises(RuntimeError, match="floor invariant"):
        render_tier_list_md(summary=summary, needs_review=None, context=_ctx())


def test_floor_invariant_enforces_s_needs_floor_2():
    """Review #7: an S row at floor 1 (url only, no activity signal) must be
    caught — the invariant enforces the per-tier minimum, not a blanket >=1.
    The same row at tier A (which only needs floor 1) renders fine."""
    s_at_floor1 = ScoredExhibitor(
        name="UrlOnlyS", tier="S", final_score=9.0, evidence_floor=1,
        dimensions=_dims(), weights_used={}, tier_reasons=[],
        row=EnrichedExhibitor(name="UrlOnlyS", source_snippet="snippet",
                              official_url="https://urlonly.example"),
        fit=FitResult(name="UrlOnlyS", capability_fit=0.9, top_hits=[]),
    )
    with pytest.raises(RuntimeError, match="floor invariant"):
        render_tier_list_md(summary=_summary(s_at_floor1), needs_review=None, context=_ctx())

    a_at_floor1 = ScoredExhibitor(
        name="UrlOnlyA", tier="A", final_score=6.5, evidence_floor=1,
        dimensions=_dims(), weights_used={}, tier_reasons=[],
        row=EnrichedExhibitor(name="UrlOnlyA", source_snippet="snippet",
                              official_url="https://urlonly.example"),
        fit=FitResult(name="UrlOnlyA", capability_fit=0.9, top_hits=[]),
    )
    # No raise — A's minimum floor is 1.
    render_tier_list_md(summary=_summary(a_at_floor1), needs_review=None, context=_ctx())


def test_tier_list_yaml_records_target_mode():
    """Review #7: the resolved target_mode is recorded in the YAML report for
    reproducibility."""
    summary = _summary(_scored("A1", "A", score=6.5, floor=1))
    payload = build_tier_list_payload(
        summary=summary, needs_review=None, context=_ctx(target_mode="partner"),
    )
    reloaded = load_tier_list_yaml(dump_tier_list_yaml(payload))
    assert reloaded["target_mode"] == "partner"


def test_tier_list_md_needs_review_isolated_from_tier_sections():
    """needs_review rows must not appear inside any tier section."""
    summary = _summary(_scored("Real", "B", score=4.5, floor=0))
    nr_name = "MaybeExhibitor"
    needs = [EnrichedExhibitor(name=nr_name, source_snippet="ambiguous snippet that is long enough",
                                enrichment_warnings=[])]
    md = render_tier_list_md(summary=summary, needs_review=needs, context=_ctx())
    s_idx = md.index("## Tier S")
    nr_idx = md.index("## Needs Review")
    # name only appears AFTER the Needs Review section header.
    name_idx = md.index(nr_name)
    assert nr_idx < name_idx
    # And not anywhere in the S/A/B/C sections.
    assert nr_name not in md[s_idx:nr_idx]


def test_tier_list_md_korean_section_headers():
    summary = _summary(_scored("S1", "S", score=8.0, floor=2))
    md = render_tier_list_md(summary=summary, needs_review=None, context=_ctx(lang="ko"))
    assert "Tier S — 최우선" in md
    assert "## 검토 필요" in md
    assert "워크스페이스" in md


# ---------- tier_list_yaml ----------


def test_tier_list_yaml_round_trip():
    summary = _summary(
        _scored("S1", "S", score=8.123, floor=2, rationale="r", angle="a"),
        _scored("B1", "B", score=4.0, floor=0),
    )
    needs = [EnrichedExhibitor(name="NR", source_snippet="snippet",
                                enrichment_warnings=["low conf"])]
    payload = build_tier_list_payload(summary=summary, needs_review=needs, context=_ctx())
    serialized = dump_tier_list_yaml(payload)
    reloaded = load_tier_list_yaml(serialized)

    assert reloaded["schema_version"] == REPORT_SCHEMA_VERSION
    assert reloaded["workspace_id"] == "acme"
    assert reloaded["event_slug"] == "sample_expo"
    assert reloaded["tier_counts"] == {"S": 1, "A": 0, "B": 1, "C": 0, "needs_review": 1}
    assert len(reloaded["exhibitors"]) == 2
    s1 = reloaded["exhibitors"][0]
    assert s1["name"] == "S1"
    assert s1["tier"] == "S"
    assert s1["final_score"] == pytest.approx(8.123, abs=1e-4)
    assert s1["rationale"] == "r"
    assert s1["angle"] == "a"
    assert s1["capability_fit_breakdown"] == {"Cap A": 3, "Cap B": 1}
    assert reloaded["needs_review"][0]["name"] == "NR"


def test_tier_list_yaml_loads_from_path(tmp_path):
    payload = build_tier_list_payload(
        summary=_summary(_scored("X", "A", score=6.5, floor=1)),
        needs_review=None, context=_ctx(),
    )
    p = tmp_path / "tier_list.yaml"
    p.write_text(dump_tier_list_yaml(payload), encoding="utf-8")
    from pathlib import Path
    reloaded = load_tier_list_yaml(Path(p))
    assert reloaded["exhibitors"][0]["name"] == "X"


# ---------- brief_export ----------


def test_product_brief_md_renders_from_cards(repo_root):
    cards = load_and_validate(repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml")
    md = render_product_brief_md(cards, lang="en")
    assert f"# {cards.product_name}" in md
    assert cards.one_liner in md
    for cap in cards.capabilities:
        assert f"### {cap.name}" in md
    # Optional sections only render if present.
    if cards.competitors:
        assert "Competitors" in md
    if cards.bad_fit:
        assert "Bad Fit" in md
    if cards.buying_triggers:
        assert "Buying Triggers" in md


def test_product_brief_md_korean_labels(repo_root):
    cards = load_and_validate(repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml")
    md = render_product_brief_md(cards, lang="ko")
    assert "역량 (Capabilities)" in md
    assert "이상적 고객" in md
