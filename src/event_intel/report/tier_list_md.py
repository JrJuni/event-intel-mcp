"""Render a tier_list.md report from a ScoringSummary + a list of needs_review rows.

6 sections (per plan v0.5 §S5):
    1. Header (event metadata + run timestamp + counts)
    2. Tier S
    3. Tier A
    4. Tier B
    5. Tier C
    6. Needs Review (separated; not part of the scored set)

Floor invariant: every S/A row has has_official_url + has_news_signals >= 1.
This is guaranteed by `scoring/rules.decide_tier`, but the renderer adds a
defensive assertion so a misconfigured tier_rules can't silently leak weak
rows into the high-tier sections.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from event_intel.events.enrichment import EnrichedExhibitor
    from event_intel.scoring.compute import ScoredExhibitor, ScoringSummary


@dataclass
class ReportContext:
    workspace_id: str
    event_name: str
    event_slug: str
    lang: str = "en"
    generated_at: datetime | None = None
    target_mode: str = "customer"   # resolved mode, recorded for reproducibility (review #7)
    tier_rules: dict | None = None  # effective scoring.tier_rules — drives the floor invariant (review r2 #3)


_TIER_HEADINGS_EN = {
    "S": "## Tier S — top targets (full evidence)",
    "A": "## Tier A — strong fit (partial evidence)",
    "B": "## Tier B — worth tracking (snippet-only OK)",
    "C": "## Tier C — long tail",
}
_TIER_HEADINGS_KO = {
    "S": "## Tier S — 최우선 (full evidence)",
    "A": "## Tier A — 강한 적합 (부분 evidence)",
    "B": "## Tier B — 추적 가치 (snippet only)",
    "C": "## Tier C — 후순위",
}


def _h(lang: str, tier: str) -> str:
    return (_TIER_HEADINGS_KO if lang == "ko" else _TIER_HEADINGS_EN)[tier]


def _evidence_chips(row: EnrichedExhibitor) -> str:
    chips: list[str] = []
    evidence = getattr(row, "evidence", None)
    if evidence:
        # One chip per distinct evidence type, with a count (typed evidence, 18V).
        counts: dict[str, int] = {}
        for e in evidence:
            counts[e.type] = counts.get(e.type, 0) + 1
        for etype in ("official_url", "product_page", "docs", "partner_page", "press_release", "news"):
            if etype in counts:
                n = counts[etype]
                chips.append(f"`{etype}×{n}`" if n > 1 else f"`{etype}`")
    else:
        if row.official_url:
            chips.append("`url`")
        if row.news_signals:
            chips.append(f"`news×{len(row.news_signals)}`")
    if not chips:
        chips.append("`snippet-only`")
    return " ".join(chips)


def _render_row(scored: ScoredExhibitor, *, lang: str) -> str:
    row = scored.row
    lines: list[str] = []
    lines.append(f"### {row.name} — **{scored.tier}** · score {scored.final_score:.2f}/10")
    lines.append(_evidence_chips(row))
    if row.official_url:
        lines.append(f"- official: <{row.official_url}>")
    if row.description:
        lines.append(f"- description: {row.description}")
    snippet = row.source_snippet.strip()
    if snippet:
        # Single-line — collapse internal whitespace so the MD stays scannable.
        snippet_clean = " ".join(snippet.split())
        lines.append(f"- evidence snippet: “{snippet_clean}”")
    if row.news_signals:
        lines.append("- recent news:")
        for n in row.news_signals[:3]:
            title = n.title.strip() or "(untitled)"
            lines.append(f"  - [{title}]({n.url})")
    if scored.rationale:
        prefix = "근거" if lang == "ko" else "rationale"
        lines.append(f"- **{prefix}**: {scored.rationale}")
    if scored.angle:
        prefix = "오프닝 앵글" if lang == "ko" else "opening angle"
        lines.append(f"- **{prefix}**: {scored.angle}")
    if scored.fit and scored.fit.capability_fit_breakdown:
        top = sorted(scored.fit.capability_fit_breakdown.items(), key=lambda kv: -kv[1])[:3]
        breakdown = ", ".join(f"{name} ({n})" for name, n in top)
        prefix = "capability hits" if lang != "ko" else "역량 매칭"
        lines.append(f"- {prefix}: {breakdown}")
    return "\n".join(lines)


# Fallback floor minimums (shipped defaults) when no tier_rules are supplied —
# used only by callers that don't pass the effective config.
_DEFAULT_TIER_FLOOR_MIN = {"S": 2, "A": 1}


def _floor_minimums(tier_rules: dict | None) -> dict[str, int]:
    """Per-tier evidence_floor_min from the EFFECTIVE tier_rules (review r2 #3) —
    so a user who legitimately lowers a tier's floor in config doesn't trip a
    report-time crash that the scorer already accepted. Falls back to defaults.
    """
    if not tier_rules:
        return dict(_DEFAULT_TIER_FLOOR_MIN)
    out: dict[str, int] = {}
    for tier, rule in tier_rules.items():
        try:
            out[tier] = int((rule or {}).get("evidence_floor_min", 0))
        except (TypeError, ValueError):
            out[tier] = 0
    return out


def _assert_floor_invariant(summary: ScoringSummary, tier_rules: dict | None = None) -> None:
    # Use the single floor authority (rules.compute_evidence_floor) so this can
    # never diverge from the scoring-stage formula again (18V item 1). The per-tier
    # minimum comes from the EFFECTIVE tier_rules (review r2 #3), not a hardcoded
    # map — config-changed floors must not crash the report.
    from event_intel.scoring.rules import compute_evidence_floor

    minimums = _floor_minimums(tier_rules)
    for scored in summary.rows:
        need = minimums.get(scored.tier)
        if not need:  # tier absent from rules or min 0 → nothing to assert
            continue
        floor = compute_evidence_floor(scored.row)
        if floor < need:
            raise RuntimeError(
                f"floor invariant broken: {scored.row.name} is tier {scored.tier} "
                f"but evidence_floor={floor} (needs >= {need}). Check scoring.tier_rules."
            )


def render_tier_list_md(
    *,
    summary: ScoringSummary,
    needs_review: list[EnrichedExhibitor] | None = None,
    context: ReportContext,
) -> str:
    """Render the 6-section Markdown report.

    `needs_review` is the bucket of low-confidence rows from extraction +
    enrichment that scoring skipped. Rendered in its own section so the human
    can decide whether to promote / drop.
    """
    _assert_floor_invariant(summary, context.tier_rules)
    generated_at = context.generated_at or datetime.now(UTC)
    lang = context.lang

    counts = summary.tier_counts
    review_n = len(needs_review or [])
    out: list[str] = []
    out.append(f"# {context.event_name}")
    if lang == "ko":
        out.append(f"_워크스페이스 `{context.workspace_id}` · 이벤트 슬러그 `{context.event_slug}` · {generated_at.strftime('%Y-%m-%d %H:%M UTC')}_")
        out.append("")
        out.append(
            "**요약**: "
            f"S {counts.get('S', 0)} · A {counts.get('A', 0)} · "
            f"B {counts.get('B', 0)} · C {counts.get('C', 0)} · "
            f"검토 필요 {review_n}"
        )
    else:
        out.append(f"_workspace `{context.workspace_id}` · event slug `{context.event_slug}` · {generated_at.strftime('%Y-%m-%d %H:%M UTC')}_")
        out.append("")
        out.append(
            "**Summary**: "
            f"S {counts.get('S', 0)} · A {counts.get('A', 0)} · "
            f"B {counts.get('B', 0)} · C {counts.get('C', 0)} · "
            f"needs-review {review_n}"
        )
    out.append("")

    rows_by_tier: dict[str, list[ScoredExhibitor]] = {"S": [], "A": [], "B": [], "C": []}
    for scored in summary.rows:
        rows_by_tier.setdefault(scored.tier, []).append(scored)
    # Within tier, sort by descending final_score so the strongest is at top.
    for tier in rows_by_tier:
        rows_by_tier[tier].sort(key=lambda s: -s.final_score)

    for tier in ("S", "A", "B", "C"):
        out.append(_h(lang, tier))
        rows = rows_by_tier[tier]
        if not rows:
            empty = "_(없음)_" if lang == "ko" else "_(none)_"
            out.append(empty)
        else:
            for scored in rows:
                out.append("")
                out.append(_render_row(scored, lang=lang))
        out.append("")

    review_heading = "## 검토 필요 (Needs Review)" if lang == "ko" else "## Needs Review"
    out.append(review_heading)
    if not needs_review:
        out.append("_(없음)_" if lang == "ko" else "_(none)_")
    else:
        for row in needs_review:
            chips = _evidence_chips(row)
            out.append("")
            out.append(f"### {row.name}")
            out.append(chips)
            if row.source_snippet:
                snippet_clean = " ".join(row.source_snippet.split())
                out.append(f"- evidence snippet: “{snippet_clean}”")
            if row.enrichment_warnings:
                for w in row.enrichment_warnings:
                    out.append(f"- ⚠ {w}")
    out.append("")
    return "\n".join(out)
