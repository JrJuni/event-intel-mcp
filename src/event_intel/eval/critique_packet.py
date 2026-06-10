"""Critique packet builder + critique schema — BD critique harness S2.

From a collected ``tier_list.yaml`` (engine output), build a packet of the S/A
picks for a host judge to critique through a multi-lens panel. The packet carries
the picks + the product rubric header (product INPUT, not engine output) + the
expected lens keys; the lens rubric + the "blind-first" protocol live in the S3
prompt. A ``packet_sha`` ties any critique back to the exact packet it judged.

The critique schema is dataclass + explicit validation (NOT pydantic) so eval/
stays stdlib-only-at-import (cold-start safe).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from event_intel.errors import ErrorCode, MCPError, Stage

PACKET_SCHEMA_VERSION = 1
SA_TIERS = ("S", "A")
EXPECTED_LENSES = ("customer_fit", "competitor", "buying_signal")
_VERDICTS = ("agree", "disagree", "unsure")


def _packet_sha(packet: dict[str, Any]) -> str:
    blob = json.dumps(
        {k: v for k, v in packet.items() if k != "packet_sha"},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def build_critique_packet(
    *,
    pair: str,
    tier_list: dict[str, Any],
    product_header: str,
    lenses: tuple[str, ...] = EXPECTED_LENSES,
) -> dict[str, Any]:
    """Extract the S/A picks from a tier list into a critique packet.

    Only S/A picks are included — those are the engine's confident recommendations
    and the ones whose BD-defensibility a human most needs spot-checked.
    """
    picks: list[dict[str, Any]] = []
    for ex in tier_list.get("exhibitors", []) or []:
        if ex.get("tier") not in SA_TIERS:
            continue
        picks.append(
            {
                "name": ex.get("name", ""),
                "tier": ex.get("tier"),
                "final_score": ex.get("final_score"),
                "capability_fit": ex.get("capability_fit"),
                "rationale": ex.get("rationale"),
                "evidence": [
                    {"type": e.get("type"), "url": e.get("url")}
                    for e in (ex.get("evidence") or [])
                ],
            }
        )
    packet = {
        "schema_version": PACKET_SCHEMA_VERSION,
        "pair": pair,
        "product_header": product_header,
        "lenses": list(lenses),
        "picks": picks,
    }
    packet["packet_sha"] = _packet_sha(packet)
    return packet


def _err(message: str, **hint: Any) -> MCPError:
    return MCPError(
        error_code=ErrorCode.SCHEMA_ERROR,
        stage=Stage.PREFLIGHT,
        message=message,
        hint=hint or None,
    )


def parse_critique(
    obj: dict[str, Any],
    *,
    expected_packet_sha: str | None = None,
    expected_lenses: tuple[str, ...] = EXPECTED_LENSES,
) -> dict[str, Any]:
    """Validate a host-produced critique against the schema; returns it normalized.

    Schema (per pick): ``{name, independent_first:{would_place_sa:bool, reason},
    lenses:{<lens>:{verdict in agree|disagree|unsure, reason}}, defensible:bool,
    flag:bool}``. The top level carries ``pair``, ``packet_sha``, ``judge_model_id``.
    Raises SCHEMA_ERROR on any violation; when ``expected_packet_sha`` is given it
    must match (ties the critique to the packet it judged).
    """
    if not isinstance(obj, dict):
        raise _err("critique must be a JSON object")
    for key in ("pair", "packet_sha", "judge_model_id", "picks"):
        if key not in obj:
            raise _err(f"critique missing required key: {key}")
    if expected_packet_sha is not None and obj["packet_sha"] != expected_packet_sha:
        raise _err(
            "critique packet_sha does not match the packet it should judge",
            expected=expected_packet_sha, got=obj["packet_sha"],
        )
    if not isinstance(obj["picks"], list):
        raise _err("critique.picks must be a list")
    for i, pick in enumerate(obj["picks"]):
        _validate_pick(pick, i, expected_lenses)
    return obj


def _validate_pick(pick: Any, i: int, expected_lenses: tuple[str, ...]) -> None:
    if not isinstance(pick, dict):
        raise _err(f"picks[{i}] must be an object")
    if not pick.get("name"):
        raise _err(f"picks[{i}] missing name")
    indep = pick.get("independent_first")
    if not isinstance(indep, dict) or not isinstance(indep.get("would_place_sa"), bool):
        raise _err(f"picks[{i}].independent_first.would_place_sa must be bool (judge first, blind)")
    if not isinstance(indep.get("reason"), str) or not indep["reason"].strip():
        raise _err(f"picks[{i}].independent_first.reason is required")
    lenses = pick.get("lenses")
    if not isinstance(lenses, dict):
        raise _err(f"picks[{i}].lenses must be an object")
    missing = [lk for lk in expected_lenses if lk not in lenses]
    if missing:
        raise _err(f"picks[{i}] missing lens verdict(s): {missing}", expected=list(expected_lenses))
    for lk, lv in lenses.items():
        if not isinstance(lv, dict) or lv.get("verdict") not in _VERDICTS:
            raise _err(
                f"picks[{i}].lenses.{lk}.verdict must be one of {_VERDICTS}",
                got=(lv or {}).get("verdict") if isinstance(lv, dict) else lv,
            )
        if not isinstance(lv.get("reason"), str) or not lv["reason"].strip():
            raise _err(f"picks[{i}].lenses.{lk}.reason is required")
    for b in ("defensible", "flag"):
        if not isinstance(pick.get(b), bool):
            raise _err(f"picks[{i}].{b} must be bool")
