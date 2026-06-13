"""Y1D D2 — LLM roster triage: pick WHICH companies get the enrichment slots.

Why: when the extracted roster exceeds ``enrichment.max_companies`` the
pipeline used to keep "the first N" in page order — on real large rosters
(p7 Siemens: 2,885 exhibitors, cap 30) the companies worth looking at never
reached enrichment at all (extraction_coverage 1–17%). Triage scores the FULL
roster for product-domain relatedness in cheap batched LLM calls and forwards
the top-``max_companies``.

Evidence-first, two-signal output (E2, 2026-06-13). The earlier triage scored
``source_snippet[:100]`` — for a CSV roster just ``"CSV row 12: Acme | url"`` —
so it was name-keyword scoring, and real targets and industrial-sounding
look-alikes were INDISTINGUISHABLE (diagnosed P@10=0). Two changes break that:

1. **Evidence, not name.** When Tier 1 (``profile_fetch``) has populated
   ``candidate.profile_text`` — the body text of the exhibitor's detail page —
   the roster listing shows THAT, so the LLM scores what the company does. The
   bare ``source_snippet`` is the fallback only.
2. **A score is a JUDGMENT, absence is a STATE.** Each exhibitor resolves to a
   numeric fit score (0.0-1.0, KNOWN) OR the string ``"unknown"`` (no usable
   evidence). UNKNOWN is a routing state, not a low score — forcing a middle/low
   number onto no evidence was the single biggest source of wrong shortlists
   (user decision 2026-06-13). UNKNOWN companies are kept ahead of KNOWN-but-
   low-fit ones so Tier 2 (per-company search, later slice) can resolve them.

Selection ordering (user-approved 2026-06-13):
    KNOWN_FIT (fit ≥ cutoff, by fit desc) > UNKNOWN (roster order)
    > KNOWN_NOFIT (fit < cutoff, by fit desc)
Top-``max_companies`` by that key, then returned in ORIGINAL ROSTER ORDER.

Boundary rules:
- roster ≤ cap → returned as-is, ZERO LLM calls.
- no capability digest (cards missing) → first-N fallback + warning.
- a batch fails (transport or unparseable) → every company in it becomes
  UNKNOWN + warning; remaining batches still count. All batches failing
  therefore degrades to the old first-N behaviour (all-UNKNOWN ranks in roster
  order). This stage NEVER fails a build.

Scoring axis (#17, 2026-06-13): the roster is scored for TARGET FIT under the
resolved ``target_mode`` (customer | partner | ecosystem), NOT product-domain
similarity. Competitors/look-alikes are no longer guaranteed a slot: under
customer mode they score low on target fit and MAY BE CUT (user decision:
maximize customer recall; competitor_penalty still applies to whatever reaches
scoring). De-biasing *efficacy* is verified live; offline tests cover the
plumbing (prompt content, digest fields, evidence usage, selection logic).

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
    "You shortlist trade-show exhibitors for B2B business development from the "
    "evidence about what each company does. Reply with ONLY a JSON object, "
    "nothing else."
)

DEFAULT_FIT_CUTOFF = 0.5    # fit ≥ cutoff → KNOWN_FIT band; below → KNOWN_NOFIT
_MAX_PROFILE_CHARS = 600    # Tier-1 evidence shown per company in the listing
_MAX_SNIPPET_CHARS = 100    # bare-snippet fallback when no profile_text
_MAX_TOKENS = 2048          # index→score map for up to ~150 companies fits easily


@dataclass
class TriageResult:
    selected: list[ExhibitorCandidate]          # original roster order
    warnings: list[str] = field(default_factory=list)
    calls: int = 0
    # Two-signal diagnostics. ``scores`` holds ONLY companies the LLM judged
    # (KNOWN, roster index → fit); ``unknown`` holds roster indices with no
    # usable evidence. Every triaged index lands in exactly one of the two.
    scores: dict[int, float] = field(default_factory=dict)
    unknown: set[int] = field(default_factory=set)


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
    """~250-token product digest for the triage prompt — name, one-liner,
    capability names + keywords, ideal-customer industries AND signals, the
    buyer pains we solve, and bad-fit keywords. NOT the full cards.

    The signals / pains / bad-fit lines are the #17 enrichment: target-fit
    scoring needs the *customer profile* (who would buy and why), not just the
    product's own domain vocabulary, otherwise the LLM falls back to domain
    matching and re-introduces the look-alike bias.

    Defensive: any surprise shape → None (caller falls back to first-N).
    """
    if cards is None:
        return None
    try:
        lines = [f"{cards.product_name} — {cards.one_liner}"]
        pains: list[str] = []
        for cap in cards.capabilities:
            kw = ", ".join(list(cap.keywords)[:6])
            lines.append(f"- {cap.name}: {kw}" if kw else f"- {cap.name}")
            pains.extend(list(getattr(cap, "buyer_pains", []) or []))
        industries = ", ".join(list(cards.ideal_customer.industries)[:8])
        if industries:
            lines.append(f"Ideal customer industries: {industries}")
        signals = ", ".join(list(cards.ideal_customer.company_signals)[:8])
        if signals:
            lines.append(f"Ideal customer signals: {signals}")
        if pains:
            lines.append(f"Buyer pains we solve: {', '.join(pains[:6])}")
        bad_kw: list[str] = []
        for bf in getattr(cards, "bad_fit", []) or []:
            bad_kw.extend(list(getattr(bf, "keywords", []) or []))
        if bad_kw:
            lines.append(f"Not a fit: {', '.join(bad_kw[:8])}")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — digest is best-effort, never fail a build
        return None


def parse_triage_response(text: str | None) -> dict[int, float | None] | None:
    """``{"scores": {"<index>": x, ...}}`` → {index: clamped fit OR None}.

    A numeric value parses to a clamped 0.0-1.0 fit score (KNOWN). The literal
    string ``"unknown"`` (case-insensitive) parses to ``None`` — an explicit
    "no usable evidence" verdict the caller routes to the UNKNOWN band. Invalid
    ENTRIES (non-int key, non-finite/non-numeric/non-"unknown" value) are
    dropped; those indices are absent from the map and the caller treats a
    missing index as UNKNOWN too. A whole-response parse failure returns None →
    the caller marks every company in the batch UNKNOWN + warning.

    Tolerates fenced/decorated JSON and a top-level index map (no "scores"
    wrapper).
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
    out: dict[int, float | None] = {}
    for k, v in raw.items():
        try:
            idx = int(str(k).strip())
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        if isinstance(v, str) and v.strip().lower() == "unknown":
            out[idx] = None        # explicit no-evidence verdict
            continue
        try:
            score = float(v)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(score):
            continue
        out[idx] = max(0.0, min(1.0, score))
    return out or None


def _roster_listing(candidates: list[ExhibitorCandidate], offset: int) -> str:
    """``index. name — evidence`` per line. Evidence is Tier-1 ``profile_text``
    (what the company does) when present, else the bare ``source_snippet``.
    """
    lines = []
    for i, c in enumerate(candidates):
        profile = (getattr(c, "profile_text", None) or "").strip().replace("\n", " ")
        if profile:
            evidence = profile[:_MAX_PROFILE_CHARS]
        else:
            evidence = (c.source_snippet or "").strip().replace("\n", " ")[:_MAX_SNIPPET_CHARS]
        lines.append(
            f"{offset + i}. {c.name} — {evidence}" if evidence else f"{offset + i}. {c.name}"
        )
    return "\n".join(lines)


def triage_roster(
    candidates: list[ExhibitorCandidate],
    capability_digest: str | None,
    llm_provider: object,
    *,
    max_companies: int,
    batch_size: int = 120,
    lang: str = "en",
    target_mode: str = "customer",
    fit_cutoff: float = DEFAULT_FIT_CUTOFF,
    ledger: object | None = None,
) -> TriageResult:
    """Score the full roster for TARGET FIT under ``target_mode`` from the
    evidence and keep the top ``max_companies`` by the band ordering KNOWN_FIT >
    UNKNOWN > KNOWN_NOFIT, ORIGINAL ROSTER ORDER preserved in the output. See
    the module docstring for boundary rules and the two-signal model. NEVER
    raises.
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
    cutoff = float(fit_cutoff)
    template = load_triage_prompt(lang)
    known: dict[int, float] = {}
    unknown: set[int] = set()
    calls = 0
    failed_batches = 0
    n_batches = (total + bs - 1) // bs
    for b_start in range(0, total, bs):
        batch = candidates[b_start : b_start + bs]
        prompt = (
            template
            .replace("{digest}", str(capability_digest))
            .replace("{mode}", str(target_mode or "customer"))
            .replace("{roster}", _roster_listing(batch, b_start))
        )
        parsed: dict[int, float | None] | None = None
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
            # Failed batch → all UNKNOWN. Missing index or explicit "unknown"
            # → UNKNOWN. A finite number → KNOWN.
            val = parsed.get(i) if parsed is not None else None
            if val is None:
                unknown.add(i)
            else:
                known[i] = val

    # Three-band ranking. band: 2 = KNOWN_FIT, 1 = UNKNOWN, 0 = KNOWN_NOFIT.
    # Within a band, higher fit first then roster order; UNKNOWN has no fit, so
    # it falls back to pure roster order. A whole-roster failure leaves every
    # index UNKNOWN → ranks in roster order → exactly the old first-N.
    def _rank_key(i: int) -> tuple[int, float, int]:
        if i in unknown:
            return (-1, 0.0, i)                      # band 1
        fit = known[i]
        band = 2 if fit >= cutoff else 0
        return (-band, -fit, i)

    ranked = sorted(range(total), key=_rank_key)
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
            f"triage: selected {cap}/{total} exhibitors by LLM target fit "
            f"(mode={target_mode}, {calls} calls)"
        )
        if failed_batches:
            warnings.append(
                f"triage: {failed_batches}/{n_batches} batches unscored (LLM "
                f"failure) — those companies marked UNKNOWN"
            )
        if unknown:
            warnings.append(
                f"triage: {len(unknown)}/{total} exhibitors had no usable "
                f"evidence → UNKNOWN (kept ahead of low-fit; Tier 2 resolves them)"
            )
    return TriageResult(
        selected=selected, warnings=warnings, calls=calls,
        scores=known, unknown=unknown,
    )
