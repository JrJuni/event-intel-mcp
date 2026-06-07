"""Tier-decision rules — pure functions over (final_score, evidence_floor, config).

Per plan v0.5 §Contract #9 evidence floor 3-state lifecycle:
    scoring stage uses {has_snippet=True (always), has_official_url, has_news_signals}.

    floor = has_official_url(int) + has_news_signals(int) ∈ {0, 1, 2}.

    - floor == 2 → S/A possible (depending on final_score)
    - floor == 1 → A maximum (S blocked)
    - floor == 0 → B maximum (snippet only)

Tiers + min_final_score are loaded from `scoring.tier_rules` in defaults.yaml.
This module never reads from disk; the dict is injected.
"""
from __future__ import annotations

from dataclasses import dataclass

TIER_ORDER = ("S", "A", "B", "C")


@dataclass(frozen=True)
class TierDecision:
    tier: str
    reasons: list[str]


def floor_from_components(has_identity: bool, has_activity: bool) -> int:
    """Identity-vs-activity floor (Phase 18V item 1). Floor 2 requires BOTH an
    identity signal (existence proof) AND an activity/independent signal, so
    same-site evidence alone (official_url + product_page) caps at floor 1.
    """
    if has_identity and has_activity:
        return 2
    if has_identity or has_activity:
        return 1
    return 0


def compute_evidence_floor(row: object) -> int:
    """Evidence floor (0/1/2) for an enriched row. Reads the typed evidence list
    when present, else the legacy official_url + news_signals fields — so the old
    `url + news → 2` behavior is preserved as a strict subset.
    """
    from event_intel.events.evidence import floor_components

    has_identity, has_activity = floor_components(row)
    return floor_from_components(has_identity, has_activity)


def decide_tier(
    *, final_score: float, evidence_floor: int, tier_rules: dict
) -> TierDecision:
    """Find the highest tier whose min_final_score AND evidence_floor_min are satisfied.

    `tier_rules` shape (from defaults.yaml `scoring.tier_rules`):
        {tier: {"min_final_score": float, "evidence_floor_min": int}}
    """
    reasons: list[str] = []
    for tier in TIER_ORDER:
        rule = tier_rules.get(tier)
        if not rule:
            continue
        min_score = float(rule.get("min_final_score", 0.0))
        floor_min = int(rule.get("evidence_floor_min", 0))
        if final_score >= min_score and evidence_floor >= floor_min:
            return TierDecision(tier=tier, reasons=[
                f"final_score={final_score:.2f} >= {min_score:.2f}",
                f"evidence_floor={evidence_floor} >= {floor_min}",
            ])
        # Record why this tier was skipped — useful for explainability later.
        if final_score < min_score:
            reasons.append(
                f"{tier}: score {final_score:.2f} < min {min_score:.2f}"
            )
        else:
            reasons.append(
                f"{tier}: evidence_floor {evidence_floor} < min {floor_min}"
            )
    # No rule matched (would only happen if tier_rules is empty / malformed).
    return TierDecision(tier="C", reasons=reasons or ["no rule matched, default to C"])
