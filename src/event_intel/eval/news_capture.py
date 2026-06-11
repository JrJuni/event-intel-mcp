"""G4 — news-capture report (ZNC success criterion ⑤, ADVISORY).

Per company: did the build capture enough BODY-CRAWLED news? The user-defined
contract: companies with est. revenue >= $10M need >= 10 captured articles,
smaller ones >= 3. "Captured" counts post-dedup articles with a fetched body
(`news_relatedness` entries in the tier_list payload, schema v5+ — each entry
exists only for a gated, deduped, body-fetched article). `news_count` (listed,
snippet-level) is reported alongside for context.

ADVISORY, not a D6 gate: the gate contract for this cycle was frozen
(thresholds_znc.json) BEFORE this metric existed — adding a gate post-freeze
would break the freeze discipline. A future freeze may promote it.

Inputs are explicit artifacts (tier_list payload + committed revenue_tiers
file), so the report is reproducible from committed/immutable data. Companies
without a revenue-tier judgment are reported as `unknown_tier` and excluded
from the met-rate denominators (insufficient data ≠ failure, CS6 discipline).
stdlib-only.
"""
from __future__ import annotations

from typing import Any

NEWS_CAPTURE_SCHEMA = "news-capture/v1"

BIG_THRESHOLD = 10   # >= $10M revenue
SMALL_THRESHOLD = 3  # below


def news_capture_report(
    tier_list_payload: dict[str, Any],
    revenue_tiers: dict[str, bool],
    *,
    big_threshold: int = BIG_THRESHOLD,
    small_threshold: int = SMALL_THRESHOLD,
) -> dict[str, Any]:
    """Fold a tier-list payload + revenue-tier judgments into the criterion-⑤
    report. ``revenue_tiers`` maps company name → True (>= $10M) / False.
    """
    rows: list[dict[str, Any]] = []
    met_big = total_big = met_small = total_small = unknown = 0
    for ex in tier_list_payload.get("exhibitors", []) or []:
        name = ex.get("name", "")
        bodied = len(ex.get("news_relatedness", []) or [])
        listed = int(ex.get("news_count", 0) or 0)
        tier = revenue_tiers.get(name)
        if tier is None:
            unknown += 1
            rows.append({
                "name": name, "revenue_tier": None, "bodied_news": bodied,
                "listed_news": listed, "threshold": None, "met": None,
            })
            continue
        threshold = big_threshold if tier else small_threshold
        met = bodied >= threshold
        if tier:
            total_big += 1
            met_big += int(met)
        else:
            total_small += 1
            met_small += int(met)
        rows.append({
            "name": name, "revenue_tier": ">=10M" if tier else "<10M",
            "bodied_news": bodied, "listed_news": listed,
            "threshold": threshold, "met": met,
        })

    def _rate(met: int, total: int) -> float | None:
        return round(met / total, 3) if total else None

    return {
        "schema": NEWS_CAPTURE_SCHEMA,
        "grade": "advisory",  # NOT a frozen D6 gate this cycle
        "thresholds": {">=10M": big_threshold, "<10M": small_threshold},
        "companies": rows,
        "summary": {
            "total": len(rows),
            "unknown_tier": unknown,
            "big": {"total": total_big, "met": met_big,
                    "met_rate": _rate(met_big, total_big)},
            "small": {"total": total_small, "met": met_small,
                      "met_rate": _rate(met_small, total_small)},
            "overall_met_rate": _rate(met_big + met_small, total_big + total_small),
        },
    }
