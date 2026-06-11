"""Y1D D2 — LLM roster triage: pick WHICH companies get the enrichment slots.

Why: when the extracted roster exceeds ``enrichment.max_companies`` the
pipeline used to keep "the first N" in page order — on real large rosters
(p7 Siemens: 2,885 exhibitors, cap 30) the companies worth looking at never
reached enrichment at all (extraction_coverage 1–17%). Triage scores the FULL
roster for product-domain relatedness in cheap batched LLM calls (names +
short snippets only) and forwards the top-``max_companies``.

Boundary rules:
- roster ≤ cap → returned as-is, ZERO LLM calls.
- no capability digest (cards missing) → first-N fallback + warning.
- a batch fails (transport or unparseable) → that batch scores neutral 0.5
  + warning; remaining batches still count. All batches failing therefore
  degrades to the old first-N behaviour. This stage NEVER fails a build.
- competitors / bad-fit companies are SAME-domain by definition and must PASS
  triage (the prompt says so explicitly) — penalties are scoring's job.

stdlib-only at module load (cold-import rule); the LLM arrives as an injected
provider.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from event_intel.cards.schema import CapabilityCards
    from event_intel.events.extraction import ExhibitorCandidate

TRIAGE_STAGE = "triage"

TRIAGE_SYSTEM = (
    "You shortlist trade-show exhibitors for B2B business development against "
    "a product digest. Reply with ONLY a JSON object, nothing else."
)

NEUTRAL_SCORE = 0.5         # unscored companies tie here; stable sort keeps roster order
_MAX_SNIPPET_CHARS = 100    # per-company evidence in the roster listing
_MAX_TOKENS = 2048          # index→score map for up to ~150 companies fits easily


@dataclass
class TriageResult:
    selected: list[ExhibitorCandidate]          # original roster order
    warnings: list[str] = field(default_factory=list)
    calls: int = 0
    scores: dict[int, float] = field(default_factory=dict)  # roster index → score


@lru_cache(maxsize=8)
def load_triage_prompt(lang: str) -> str:
    """prompts/{lang}/triage.txt with an en fallback. Placeholders go through
    str.replace (brace-safe — the template contains a JSON example).
    """
    here = Path(__file__).resolve().parents[1]  # src/event_intel
    path = here / "prompts" / lang / "triage.txt"
    if not path.is_file():
        path = here / "prompts" / "en" / "triage.txt"
    return path.read_text(encoding="utf-8")


def build_capability_digest(cards: CapabilityCards | None) -> str | None:
    """~200-token product digest for the triage prompt — name, one-liner,
    capability names + a few keywords, ideal industries. NOT the full cards.
    Defensive: any surprise shape → None (caller falls back to first-N).
    """
    if cards is None:
        return None
    try:
        lines = [f"{cards.product_name} — {cards.one_liner}"]
        for cap in cards.capabilities:
            kw = ", ".join(list(cap.keywords)[:6])
            lines.append(f"- {cap.name}: {kw}" if kw else f"- {cap.name}")
        industries = ", ".join(list(cards.ideal_customer.industries)[:8])
        if industries:
            lines.append(f"Ideal customer industries: {industries}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — digest is best-effort, never fail a build
        return None


def parse_triage_response(text: str | None) -> dict[int, float] | None:
    """``{"scores": {"<index>": x, ...}}`` → {index: clamped score}.

    Tolerates fenced/decorated JSON and a top-level index map (no "scores"
    wrapper). Invalid ENTRIES (non-int key, non-finite/non-numeric score) are
    dropped — the caller fills them with NEUTRAL_SCORE. None = nothing usable
    → the caller marks the whole batch neutral + warning.
    """
    if not text:
        return None
    s = text.strip()
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    raw = obj.get("scores") if isinstance(obj.get("scores"), dict) else obj
    out: dict[int, float] = {}
    for k, v in raw.items():
        try:
            idx = int(str(k).strip())
            score = float(v)
        except (TypeError, ValueError):
            continue
        if idx < 0 or not math.isfinite(score):
            continue
        out[idx] = max(0.0, min(1.0, score))
    return out or None


def _roster_listing(candidates: list[ExhibitorCandidate], offset: int) -> str:
    lines = []
    for i, c in enumerate(candidates):
        snippet = (c.source_snippet or "").strip().replace("\n", " ")
        snippet = snippet[:_MAX_SNIPPET_CHARS]
        lines.append(f"{offset + i}. {c.name} — {snippet}" if snippet else f"{offset + i}. {c.name}")
    return "\n".join(lines)


def triage_roster(
    candidates: list[ExhibitorCandidate],
    capability_digest: str | None,
    llm_provider: object,
    *,
    max_companies: int,
    batch_size: int = 120,
    lang: str = "en",
    ledger: object | None = None,
) -> TriageResult:
    """Score the full roster for product-domain relevance and keep the top
    ``max_companies``, ORIGINAL ROSTER ORDER preserved. See module docstring
    for the boundary rules. NEVER raises.
    """
    total = len(candidates)
    cap = max(1, int(max_companies))
    if total <= cap:
        return TriageResult(selected=list(candidates))
    if not capability_digest or not str(capability_digest).strip():
        return TriageResult(
            selected=list(candidates[:cap]),
            warnings=[
                f"triage: no capability digest (cards unavailable) — kept the "
                f"first {cap}/{total} in roster order"
            ],
        )

    bs = max(1, int(batch_size))
    template = load_triage_prompt(lang)
    scores: dict[int, float] = {}
    calls = 0
    failed_batches = 0
    n_batches = (total + bs - 1) // bs
    for b_start in range(0, total, bs):
        batch = candidates[b_start : b_start + bs]
        prompt = (
            template
            .replace("{digest}", str(capability_digest))
            .replace("{roster}", _roster_listing(batch, b_start))
        )
        parsed: dict[int, float] | None = None
        try:
            resp = llm_provider.chat_once(
                system=TRIAGE_SYSTEM, user=prompt,
                max_tokens=_MAX_TOKENS, temperature=0.0,
            )
            calls += 1
            if ledger is not None:
                ledger.record(
                    TRIAGE_STAGE,
                    getattr(llm_provider, "model", ""),
                    getattr(resp, "usage", None),
                )
            parsed = parse_triage_response(getattr(resp, "text", None))
        except Exception:  # noqa: BLE001 — batch-level fallback, never fail the build
            parsed = None
        if parsed is None:
            failed_batches += 1
        for i in range(b_start, b_start + len(batch)):
            scores[i] = (parsed or {}).get(i, NEUTRAL_SCORE)

    # Stable sort: ties (incl. whole failed batches at NEUTRAL_SCORE) keep
    # roster order, so a total failure degrades exactly to the old first-N.
    ranked = sorted(range(total), key=lambda i: -scores[i])
    selected_idx = sorted(ranked[:cap])
    selected = [candidates[i] for i in selected_idx]

    warnings: list[str] = []
    if failed_batches >= n_batches:
        warnings.append(
            f"triage: all {n_batches} batches failed — fell back to the first "
            f"{cap}/{total} in roster order"
        )
    else:
        # No-silent-caps: triage trimming is always announced.
        warnings.append(
            f"triage: selected {cap}/{total} exhibitors by LLM product-domain "
            f"relevance ({calls} calls)"
        )
        if failed_batches:
            warnings.append(
                f"triage: {failed_batches}/{n_batches} batches unscored (LLM "
                f"failure) — those companies scored neutral {NEUTRAL_SCORE}"
            )
    return TriageResult(
        selected=selected, warnings=warnings, calls=calls, scores=scores
    )
