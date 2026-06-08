"""Blind labeling artifacts — Y1 benchmark CS3 / CS3b.

Enforces the blind measurement state machine (design v4 §2):
  (4) company packet  — names only, shuffled, engine score/tier/rank hidden.
  (5) sealed labels   — user labels, a SEPARATE artifact from the packet (R2-1).
  (7) evidence packet — built ONLY after company labels are sealed, so the
                        top-10 membership it exposes can't bias company labels
                        (R2-2). Carries enough per-item context to judge, but
                        hides tier/score/rank (SK-2).
  (8) sealed verdicts — user evidence verdicts, sealed.

Cohorts (R3-3): full-label pairs label the whole roster; ordinary pairs label
the engine top-10 mixed with fixed decoys so the labeler can't tell which were
top-10. Pure stdlib (random / hashlib / json) — import-cold.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field
from typing import Any

from event_intel.eval.roster import RosterEntry, match_norm

FULL = "full"
TOP10_DECOY = "top10_decoy"
_EVIDENCE_VERDICTS = ("correct", "wrong-company", "wrong-type", "stale")


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


@dataclass
class CompanyPacket:
    pair: str
    cohort: str
    seed: int
    entries: list[dict[str, Any]]  # [{"index": i, "name": str}] — NO engine output

    def names(self) -> list[str]:
        return [e["name"] for e in self.entries]


@dataclass
class SealedLabels:
    pair: str
    labels: dict[str, str]  # name -> target|competitor|bad_fit|neutral
    sha: str
    packet_sha: str         # ties labels to the packet they were made against


@dataclass
class EvidenceItem:
    company: str
    credited_type: str
    snippet: str
    url: str | None
    published_at: str | None


@dataclass
class EvidencePacket:
    pair: str
    items: list[EvidenceItem] = field(default_factory=list)


@dataclass
class SealedVerdicts:
    pair: str
    verdicts: list[str]
    sha: str


def select_cohort(
    *,
    cohort: str,
    roster: list[RosterEntry],
    run_top10_names: list[str] | None = None,
    decoy_count: int = 10,
    seed: int = 0,
) -> list[str]:
    """Names to label, deduped + deterministically shuffled (seed)."""
    if cohort == FULL:
        names = [e.canonical_name for e in roster]
    elif cohort == TOP10_DECOY:
        top = list(run_top10_names or [])
        top_norm = {match_norm(n) for n in top}
        pool = [e.canonical_name for e in roster if match_norm(e.canonical_name) not in top_norm]
        rng = random.Random(seed)
        decoys = rng.sample(pool, min(decoy_count, len(pool)))
        names = top + decoys
    else:
        raise ValueError(f"unknown cohort {cohort!r}")
    # dedupe by norm, preserve first occurrence, then shuffle.
    seen: set[str] = set()
    uniq = [n for n in names if not (match_norm(n) in seen or seen.add(match_norm(n)))]
    random.Random(seed).shuffle(uniq)
    return uniq


def build_company_packet(
    *,
    pair: str,
    cohort: str,
    roster: list[RosterEntry],
    run_top10_names: list[str] | None = None,
    decoy_count: int = 10,
    seed: int = 0,
) -> CompanyPacket:
    """Build the blind company packet — names only, engine output never included."""
    names = select_cohort(
        cohort=cohort, roster=roster, run_top10_names=run_top10_names,
        decoy_count=decoy_count, seed=seed,
    )
    entries = [{"index": i, "name": n} for i, n in enumerate(names)]
    return CompanyPacket(pair=pair, cohort=cohort, seed=seed, entries=entries)


def seal_company_labels(
    packet: CompanyPacket, labels: dict[str, str]
) -> SealedLabels:
    """Freeze user labels into a SEPARATE artifact (R2-1), tied to the packet."""
    clean = {name: labels[name] for name in packet.names() if name in labels}
    return SealedLabels(
        pair=packet.pair,
        labels=clean,
        sha=_sha(clean),
        packet_sha=_sha([e["name"] for e in packet.entries]),
    )


def build_evidence_packet(
    *,
    pair: str,
    top10_evidence: list[dict[str, Any]],
    sealed_company_labels: SealedLabels | None,
) -> EvidencePacket:
    """Build the evidence packet — ONLY after company labels are sealed (R2-2).

    `top10_evidence`: per-item dicts with company / credited_type / snippet /
    url / published_at. tier/score/rank are deliberately not carried (SK-2).
    """
    if sealed_company_labels is None:
        raise ValueError(
            "evidence packet must be built AFTER company labels are sealed "
            "(top-10 membership would otherwise bias company labels — R2-2)"
        )
    items = [
        EvidenceItem(
            company=str(e["company"]),
            credited_type=str(e["credited_type"]),
            snippet=str(e.get("snippet", "")),
            url=e.get("url"),
            published_at=e.get("published_at"),
        )
        for e in top10_evidence
    ]
    return EvidencePacket(pair=pair, items=items)


def seal_evidence_verdicts(
    packet: EvidencePacket, verdicts: list[str]
) -> SealedVerdicts:
    bad = [v for v in verdicts if v not in _EVIDENCE_VERDICTS]
    if bad:
        raise ValueError(f"invalid evidence verdicts {bad}; allowed {_EVIDENCE_VERDICTS}")
    return SealedVerdicts(pair=packet.pair, verdicts=list(verdicts), sha=_sha(verdicts))
