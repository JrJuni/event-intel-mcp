"""Tie dimensions + weights → final_score → tier. Optionally enrich with a
1-sentence Sonnet rationale + angle (LLM bounded use, per plan Contract #5).

The score formula:
    final_score_raw = Σ_i weight_i × dim_i
    final_score     = clamp(0, 10, final_score_raw × 10)

Penalty weights in defaults.yaml are negative (e.g. competitor_penalty: -0.10),
so the penalty dimensions (which are 0..1 positive) still subtract from the
sum naturally. We multiply the total by 10 to map onto the [0..10] tier-rule
range from defaults.yaml.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from event_intel.errors import ErrorCode, MCPError, Stage
from event_intel.scoring.dimensions import DimensionScores, compute_dimensions
from event_intel.scoring.rules import (
    TierDecision,
    compute_evidence_floor,
    decide_tier,
)

if TYPE_CHECKING:
    from event_intel.cards.schema import CapabilityCards
    from event_intel.events.enrichment import EnrichedExhibitor
    from event_intel.providers.llm import LLMProvider
    from event_intel.rag.retriever import FitResult


@dataclass
class ScoredExhibitor:
    name: str
    tier: str
    final_score: float
    evidence_floor: int                   # 0 / 1 / 2
    dimensions: DimensionScores
    weights_used: dict[str, float]
    tier_reasons: list[str]
    rationale: str | None = None
    angle: str | None = None
    row: "EnrichedExhibitor" = None       # carry forward for report stage
    fit: "FitResult" = None               # carry forward for explainability


@dataclass
class ScoringSummary:
    rows: list[ScoredExhibitor]
    tier_counts: dict[str, int]
    rationale_calls: int                  # LLM call count actually made


_RATIONALE_PROMPT_EN = (
    "You are a B2B sales analyst. In a SINGLE sentence (max 30 words), explain "
    "why this exhibitor is a {tier}-tier target for the product described in "
    "the capability cards. Then on a new line, give a SINGLE-sentence "
    "'opening angle' a BD rep could use in a cold email.\n\n"
    "Output format (exactly):\n"
    "RATIONALE: <one sentence>\n"
    "ANGLE: <one sentence>\n\n"
    "Do not include anything else."
)


_RATIONALE_PROMPT_KO = (
    "당신은 B2B 영업 분석가입니다. 다음 전시 참가사가 capability cards 의 "
    "제품에 대해 왜 {tier} 등급 타겟인지 한 문장 (최대 30단어) 으로 설명하세요. "
    "그 다음 새 줄에, BD 담당자가 콜드 이메일에 사용할 한 문장짜리 "
    "'오프닝 앵글' 을 제시하세요.\n\n"
    "출력 형식 (정확히):\n"
    "RATIONALE: <한 문장>\n"
    "ANGLE: <한 문장>\n\n"
    "다른 내용은 절대 출력하지 마세요."
)


def _build_rationale_user_message(
    *, scored: ScoredExhibitor, cards: "CapabilityCards | None"
) -> str:
    parts: list[str] = []
    parts.append(f"EXHIBITOR: {scored.row.name}")
    if scored.row.description:
        parts.append(f"DESCRIPTION: {scored.row.description}")
    if scored.row.source_snippet:
        parts.append(f"EVIDENCE: {scored.row.source_snippet}")
    if scored.row.official_url:
        parts.append(f"OFFICIAL_URL: {scored.row.official_url}")
    if scored.row.news_signals:
        titles = "; ".join(n.title for n in scored.row.news_signals[:3] if n.title)
        if titles:
            parts.append(f"RECENT_NEWS: {titles}")
    if cards:
        parts.append(f"PRODUCT: {cards.product_name} — {cards.one_liner}")
        if scored.fit and scored.fit.capability_fit_breakdown:
            top = sorted(
                scored.fit.capability_fit_breakdown.items(),
                key=lambda kv: -kv[1],
            )[:3]
            parts.append(
                "CAPABILITY HITS: "
                + ", ".join(f"{name} ({n})" for name, n in top)
            )
    parts.append(f"FINAL_SCORE: {scored.final_score:.2f}/10")
    parts.append(f"EVIDENCE_FLOOR: {scored.evidence_floor}/2")
    return "\n".join(parts)


def _parse_rationale_response(text: str) -> tuple[str | None, str | None]:
    rationale: str | None = None
    angle: str | None = None
    for line in text.strip().splitlines():
        line = line.strip()
        if line.upper().startswith("RATIONALE:"):
            rationale = line[len("RATIONALE:"):].strip() or None
        elif line.upper().startswith("ANGLE:"):
            angle = line[len("ANGLE:"):].strip() or None
    return rationale, angle


def _compute_one(
    *,
    row: "EnrichedExhibitor",
    fit: "FitResult",
    cards: "CapabilityCards | None",
    weights: dict[str, float],
    tier_rules: dict,
    top_k: int,
    reference_date: datetime | None = None,
    half_life_days: float = 180.0,
    negative_sim_threshold: float = 0.0,
) -> ScoredExhibitor:
    dims = compute_dimensions(
        row, fit, cards=cards, top_k=top_k,
        reference_date=reference_date, half_life_days=half_life_days,
        negative_sim_threshold=negative_sim_threshold,
    )

    raw = (
        dims.capability_fit       * weights.get("capability_fit", 0.0)
        + dims.source_confidence  * weights.get("source_confidence", 0.0)
        + dims.buying_signal      * weights.get("buying_signal", 0.0)
        + dims.website_verification * weights.get("website_verification", 0.0)
        + dims.category_fit       * weights.get("category_fit", 0.0)
        + dims.competitor_penalty * weights.get("competitor_penalty", 0.0)
        + dims.bad_fit_penalty    * weights.get("bad_fit_penalty", 0.0)
    )
    final_score = max(0.0, min(10.0, raw * 10.0))

    floor = compute_evidence_floor(
        has_official_url=bool(row.official_url),
        has_news_signals=bool(row.news_signals),
    )

    decision: TierDecision = decide_tier(
        final_score=final_score, evidence_floor=floor, tier_rules=tier_rules
    )

    return ScoredExhibitor(
        name=row.name,
        tier=decision.tier,
        final_score=final_score,
        evidence_floor=floor,
        dimensions=dims,
        weights_used=dict(weights),
        tier_reasons=list(decision.reasons),
        row=row,
        fit=fit,
    )


def score_exhibitors(
    *,
    enriched: list["EnrichedExhibitor"],
    fit_results: list["FitResult"],
    cards: "CapabilityCards | None",
    config: dict,
    top_k: int,
    llm_provider: "LLMProvider | None" = None,
    rationale_lang: str = "en",
    rationale_for_tiers: tuple[str, ...] = ("S", "A"),
    rationale_max_tokens: int = 256,
    reference_date: datetime | None = None,
) -> ScoringSummary:
    """Score every (enriched, fit_result) pair and decide tier.

    If `llm_provider` is supplied, run a 1-sentence Sonnet rationale call only
    for exhibitors landing in `rationale_for_tiers` (default S/A). This keeps
    LLM use bounded per plan Contract #5.

    `enriched` and `fit_results` MUST be in matching order. We don't reorder
    silently — if a caller has both, they own the alignment.
    """
    if len(enriched) != len(fit_results):
        raise MCPError(
            error_code=ErrorCode.INTERNAL,
            stage=Stage.SCORING,
            message=(
                f"length mismatch: {len(enriched)} enriched rows vs "
                f"{len(fit_results)} fit results"
            ),
        )

    try:
        weights = dict(config["scoring"]["weights"])
        tier_rules = dict(config["scoring"]["tier_rules"])
        half_life_days = float(
            config.get("scoring", {})
            .get("buying_signal", {})
            .get("recency_half_life_days", 180.0)
        )
        negative_sim_threshold = float(
            config.get("scoring", {})
            .get("retrieval", {})
            .get("negative_sim_threshold", 0.0)
        )
    except (KeyError, TypeError) as exc:
        raise MCPError(
            error_code=ErrorCode.CONFIG_ERROR,
            stage=Stage.SCORING,
            message=f"missing scoring config: {exc}",
            hint={"required": ["scoring.weights", "scoring.tier_rules"]},
        ) from exc

    rows: list[ScoredExhibitor] = []
    for row, fit in zip(enriched, fit_results, strict=True):
        scored = _compute_one(
            row=row, fit=fit, cards=cards,
            weights=weights, tier_rules=tier_rules, top_k=top_k,
            reference_date=reference_date, half_life_days=half_life_days,
            negative_sim_threshold=negative_sim_threshold,
        )
        rows.append(scored)

    rationale_calls = 0
    if llm_provider is not None:
        system = (
            _RATIONALE_PROMPT_KO if rationale_lang == "ko" else _RATIONALE_PROMPT_EN
        )
        for scored in rows:
            if scored.tier not in rationale_for_tiers:
                continue
            user = _build_rationale_user_message(scored=scored, cards=cards)
            try:
                resp = llm_provider.chat_once(
                    system=system.format(tier=scored.tier),
                    user=user,
                    max_tokens=rationale_max_tokens,
                    temperature=0.0,
                )
            except Exception:
                # Rationale is decorative — never fail the whole batch on it.
                continue
            rationale, angle = _parse_rationale_response(resp.text)
            scored.rationale = rationale
            scored.angle = angle
            rationale_calls += 1

    counts: dict[str, int] = {"S": 0, "A": 0, "B": 0, "C": 0}
    for scored in rows:
        counts[scored.tier] = counts.get(scored.tier, 0) + 1

    return ScoringSummary(rows=rows, tier_counts=counts, rationale_calls=rationale_calls)
