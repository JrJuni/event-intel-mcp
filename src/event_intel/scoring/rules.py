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


def compute_evidence_floor(*, has_official_url: bool, has_news_signals: bool) -> int:
    return int(bool(has_official_url)) + int(bool(has_news_signals))


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
