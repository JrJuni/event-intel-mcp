"""Machine-readable tier_list.yaml: dump + load (round-trip).

Schema (informal — pydantic SSOT lives in capability_cards, not here, since
tier_list is a *report* not a long-lived artifact):

    schema_version: 1
    workspace_id: str
    event_name: str
    event_slug: str
    generated_at: str (ISO-8601 UTC)
    tier_counts: {S: int, A: int, B: int, C: int, needs_review: int}
    exhibitors:
      - name: str
        tier: "S" | "A" | "B" | "C"
        final_score: float
        evidence_floor: int
        official_url: str | null
        news_count: int
        source_snippet: str
        rationale: str | null
        angle: str | null
        capability_fit: float
        capability_fit_breakdown: {capability_name: hit_count}
    needs_review:
      - name: str
        source_snippet: str
        enrichment_warnings: [str]

Round-trip via `load_tier_list_yaml(dump_tier_list_yaml(x))` returns the same
dict shape.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from event_intel.events.enrichment import EnrichedExhibitor
    from event_intel.report.tier_list_md import ReportContext
    from event_intel.scoring.compute import ScoringSummary


REPORT_SCHEMA_VERSION = 1


def _exhibitor_to_dict(scored) -> dict:
    row = scored.row
    fit = scored.fit
    return {
        "name": row.name,
        "tier": scored.tier,
        "final_score": round(float(scored.final_score), 4),
        "evidence_floor": int(scored.evidence_floor),
        "official_url": row.official_url,
        "news_count": len(row.news_signals),
        "source_snippet": row.source_snippet,
        "rationale": scored.rationale,
        "angle": scored.angle,
        "capability_fit": round(float(fit.capability_fit), 4) if fit else 0.0,
        "capability_fit_breakdown": dict(fit.capability_fit_breakdown) if fit else {},
    }


def _needs_review_to_dict(row: "EnrichedExhibitor") -> dict:
    return {
        "name": row.name,
        "source_snippet": row.source_snippet,
        "enrichment_warnings": list(row.enrichment_warnings),
    }


def build_tier_list_payload(
    *,
    summary: "ScoringSummary",
    needs_review: list["EnrichedExhibitor"] | None,
    context: "ReportContext",
) -> dict:
    generated_at = context.generated_at or datetime.now(timezone.utc)
    counts = dict(summary.tier_counts)
    counts["needs_review"] = len(needs_review or [])
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "workspace_id": context.workspace_id,
        "event_name": context.event_name,
        "event_slug": context.event_slug,
        "lang": context.lang,
        "generated_at": generated_at.isoformat(),
        "tier_counts": counts,
        "exhibitors": [_exhibitor_to_dict(s) for s in summary.rows],
        "needs_review": [_needs_review_to_dict(r) for r in (needs_review or [])],
    }


def dump_tier_list_yaml(payload: dict) -> str:
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def load_tier_list_yaml(text_or_path: str | Path) -> dict:
    """Load from a yaml string OR from a filesystem path."""
    if isinstance(text_or_path, Path):
        return yaml.safe_load(text_or_path.read_text(encoding="utf-8")) or {}
    p = Path(text_or_path)
    if p.is_file():
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return yaml.safe_load(text_or_path) or {}
