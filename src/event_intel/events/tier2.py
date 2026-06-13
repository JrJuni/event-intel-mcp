"""Y1D E3 — Tier 2 adaptive evidence search for UNKNOWN exhibitors.

After Tier-1 triage (``triage.py``) some exhibitors resolve to UNKNOWN — no
usable evidence on the show's detail page, only a bare name. Tier 1 keeps those
ahead of KNOWN-but-low-fit companies, but they cannot be CONFIRMED as targets
without knowing what they do. Tier 2 closes that gap: for the UNKNOWN companies
still IN CONTENTION for an enrichment slot it runs ONE web search per company
(the configured search provider — ddgs keyless by default), turns the top
result snippets into ``profile_text``, and re-triages just those companies so
they convert UNKNOWN → KNOWN_FIT / KNOWN_NOFIT.

Adaptive + cost-gated (user decision 2026-06-13):

- **Skip when Tier 1 already filled the shortlist.** UNKNOWN companies never
  outrank KNOWN_FIT ones, so when Tier-1 KNOWN_FIT ≥ cap none of the UNKNOWN can
  be selected — Tier 2 does nothing (a show with rich detail pages costs $0
  extra). This is the "no need to brute-force if the page told us enough" gate.
- **Roster-size auto-gating** (user-chosen). A small roster (≤
  ``small_roster_threshold``) is brute-forced — every UNKNOWN gets searched, the
  "minor show, be prepared to search every company" case. A large roster is
  bounded by ``max_searches_per_event`` so a 2,885-row CSV can't fire thousands
  of ddgs queries; whatever is left unsearched is logged (no silent caps).
- **Adaptive early-stop.** UNKNOWN are searched in ROSTER ORDER (the order they
  would be selected), re-triaged in chunks, and the loop STOPS as soon as enough
  KNOWN_FIT exist to fill the cap — so a roster that resolves quickly spends only
  what it needs, while a stubbornly-thin one keeps going to the budget.
- **Rate-limit give-up.** ddgs degrades to empty under rate-limiting; after
  ``_DEGRADED_GIVEUP`` consecutive degraded searches Tier 2 aborts rather than
  burning the rest of the budget on a throttled provider.

All thresholds are config-driven and PROVISIONAL (June 2026 is ddgs-only with
burned quotas — validated offline against fakes; live tuning is a later phase).

NEVER raises — a search/transport/LLM failure leaves a company UNKNOWN and the
Tier-1 selection stands. stdlib-only at module load (cold-import rule); the
search provider and LLM arrive injected.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from event_intel.events import triage as _triage

if TYPE_CHECKING:
    from event_intel.events.extraction import ExhibitorCandidate
    from event_intel.events.triage import TriageResult

# PROVISIONAL defaults (config-overridable; derive from live data in a later
# phase per the empirical-policy contract).
DEFAULT_ENABLED = True
DEFAULT_SMALL_ROSTER_THRESHOLD = 300   # ≤ this → search every UNKNOWN (brute force)
DEFAULT_MAX_SEARCHES = 50              # > threshold → hard per-event search ceiling
DEFAULT_SEARCH_BATCH = 10             # UNKNOWN searched + re-triaged per adaptive step
DEFAULT_SEARCH_COUNT = 3              # web results per company folded into evidence

_MAX_TIER2_PROFILE_CHARS = 600        # mirror triage._MAX_PROFILE_CHARS
_DEGRADED_GIVEUP = 3                  # consecutive degraded searches → abort Tier 2


@dataclass
class Tier2Config:
    enabled: bool = DEFAULT_ENABLED
    small_roster_threshold: int = DEFAULT_SMALL_ROSTER_THRESHOLD
    max_searches_per_event: int = DEFAULT_MAX_SEARCHES
    search_batch: int = DEFAULT_SEARCH_BATCH
    search_count: int = DEFAULT_SEARCH_COUNT

    @classmethod
    def from_dict(cls, d: dict | None) -> Tier2Config:
        d = d or {}
        return cls(
            enabled=bool(d.get("enabled", DEFAULT_ENABLED)),
            small_roster_threshold=int(
                d.get("small_roster_threshold", DEFAULT_SMALL_ROSTER_THRESHOLD)
            ),
            max_searches_per_event=int(
                d.get("max_searches_per_event", DEFAULT_MAX_SEARCHES)
            ),
            search_batch=int(d.get("search_batch", DEFAULT_SEARCH_BATCH)),
            search_count=int(d.get("search_count", DEFAULT_SEARCH_COUNT)),
        )


@dataclass
class Tier2Result:
    """The re-selected triage outcome plus Tier-2 cost/effect counters."""

    triage: TriageResult                 # updated selection + folded warnings
    searched: int = 0                    # companies web-searched
    resolved_fit: int = 0               # UNKNOWN → KNOWN_FIT
    resolved_nofit: int = 0             # UNKNOWN → KNOWN_NOFIT
    still_unknown: int = 0              # searched but evidence stayed thin
    unsearched_unknown: int = 0         # budget/early-stop/give-up left these UNKNOWN
    calls: int = 0                      # extra re-triage LLM calls
    degraded_giveup: bool = False       # provider rate-limited → aborted early


def _search_profile(
    candidate: ExhibitorCandidate,
    search_provider: object,
    cfg: Tier2Config,
    lang: str,
) -> tuple[str | None, bool]:
    """One web search for the company → a ``profile_text``-shaped evidence
    string from the top result titles+snippets. Returns ``(text, degraded)``:
    ``text`` is None on no name / no results / error; ``degraded`` is True when
    the provider reported the call rate-limited (ddgs degrade convention).
    """
    name = (getattr(candidate, "name", "") or "").strip()
    if not name:
        return None, False
    # PROVISIONAL query: the quoted company name. Generic qualifiers risk
    # cross-language noise; the detail-page evidence already disambiguated the
    # KNOWN ones, so Tier 2 only needs "what is this company".
    query = f'"{name}"'
    try:
        results = search_provider.search(
            query, kind="web", count=int(cfg.search_count), lang=lang
        )
    except Exception:  # noqa: BLE001 — degrade to no-evidence, never fail a build
        return None, False
    degraded = bool(getattr(search_provider, "last_call_degraded", False))
    if not results:
        return None, degraded
    parts: list[str] = []
    for r in results:
        title = (getattr(r, "title", "") or "").strip()
        snippet = (getattr(r, "snippet", "") or "").strip()
        seg = f"{title}. {snippet}".strip(". ").strip()
        if seg:
            parts.append(seg)
    text = " ".join(parts).strip().replace("\n", " ")
    if not text:
        return None, degraded
    return text[:_MAX_TIER2_PROFILE_CHARS], degraded


def resolve_unknowns(
    candidates: list[ExhibitorCandidate],
    tier1: TriageResult,
    capability_digest: str | None,
    search_provider: object | None,
    triage_llm: object,
    *,
    max_companies: int,
    cfg: Tier2Config,
    fit_cutoff: float = _triage.DEFAULT_FIT_CUTOFF,
    lang: str = "en",
    target_mode: str = "customer",
    ledger: object | None = None,
) -> Tier2Result:
    """Resolve Tier-1 UNKNOWN exhibitors via per-company web search + re-triage,
    then re-select. Returns a :class:`Tier2Result` whose ``triage`` carries the
    updated selection and the Tier-1 warnings + Tier-2 summary lines folded in.
    NEVER raises; a no-op returns ``tier1`` unchanged.
    """
    total = len(candidates)
    cap = max(1, int(max_companies))
    cutoff = float(fit_cutoff)
    known: dict[int, float] = dict(tier1.scores)
    unknown: set[int] = set(tier1.unknown)

    # Gates: feature off / no provider / no digest / nothing to resolve.
    if (
        not cfg.enabled
        or search_provider is None
        or not capability_digest
        or not str(capability_digest).strip()
        or not unknown
    ):
        return Tier2Result(triage=tier1)

    known_fit = sum(1 for v in known.values() if v >= cutoff)
    if known_fit >= cap:
        # Tier 1 already found enough fit companies to fill the shortlist —
        # UNKNOWN can't be selected, so don't spend a single search.
        merged = list(tier1.warnings)
        merged.append(
            f"tier2: skipped — Tier 1 found {known_fit} fit companies for "
            f"{cap} slots (UNKNOWN can't be selected)"
        )
        return Tier2Result(triage=_replace_warnings(tier1, merged))

    # Roster-size auto-gating: brute-force a small roster, ceiling a big one.
    if total <= int(cfg.small_roster_threshold):
        budget = len(unknown)
    else:
        budget = max(0, int(cfg.max_searches_per_event))
    budget = min(budget, len(unknown))

    pending = sorted(unknown)            # UNKNOWN in roster (selection) order
    step = max(1, int(cfg.search_batch))
    searched = 0
    extra_calls = 0
    searched_idx: set[int] = set()
    consecutive_degraded = 0
    degraded_giveup = False

    while pending and searched < budget and known_fit < cap:
        take = min(step, budget - searched, len(pending))
        chunk = pending[:take]
        pending = pending[take:]
        with_evidence: list[int] = []
        for i in chunk:
            searched += 1
            searched_idx.add(i)
            text, degraded = _search_profile(candidates[i], search_provider, cfg, lang)
            if degraded:
                consecutive_degraded += 1
            else:
                consecutive_degraded = 0
            if text:
                candidates[i].profile_text = text
                with_evidence.append(i)
            if consecutive_degraded >= _DEGRADED_GIVEUP:
                degraded_giveup = True
                break
        # Re-triage the companies that gained evidence this step (one batch).
        if with_evidence:
            k2, _u2, calls2, _fb, _nb = _triage.score_indexed(
                [(i, candidates[i]) for i in with_evidence],
                str(capability_digest),
                triage_llm,
                batch_size=len(with_evidence),
                lang=lang,
                target_mode=target_mode,
                ledger=ledger,
            )
            extra_calls += calls2
            for i, fit in k2.items():
                known[i] = fit
                unknown.discard(i)
            known_fit = sum(1 for v in known.values() if v >= cutoff)
        if degraded_giveup:
            break

    # Re-select with the updated maps.
    selected_idx = _triage.select_by_band(total, known, unknown, cap, cutoff)
    selected = [candidates[i] for i in selected_idx]

    resolved_fit = sum(1 for i in searched_idx if known.get(i, -1.0) >= cutoff)
    resolved_nofit = sum(
        1 for i in searched_idx if i in known and known[i] < cutoff
    )
    still_unknown = sum(1 for i in searched_idx if i in unknown)
    unsearched_unknown = len(pending)

    warnings = list(tier1.warnings)
    warnings.append(
        f"tier2: searched {searched} UNKNOWN company(ies) → {resolved_fit} fit, "
        f"{resolved_nofit} no-fit, {still_unknown} still unknown ({extra_calls} "
        f"re-triage call(s))"
    )
    if degraded_giveup:
        warnings.append(
            f"tier2: search provider rate-limited ({_DEGRADED_GIVEUP} consecutive "
            f"degraded) — aborted; {unsearched_unknown} UNKNOWN left unresolved"
        )
    elif unsearched_unknown:
        warnings.append(
            f"tier2: {unsearched_unknown} UNKNOWN left unsearched "
            f"(budget {budget}/early-stop) — kept ahead of low-fit in selection"
        )

    updated = _triage.TriageResult(
        selected=selected,
        warnings=warnings,
        calls=int(tier1.calls) + extra_calls,
        scores=known,
        unknown=unknown,
    )
    return Tier2Result(
        triage=updated,
        searched=searched,
        resolved_fit=resolved_fit,
        resolved_nofit=resolved_nofit,
        still_unknown=still_unknown,
        unsearched_unknown=unsearched_unknown,
        calls=extra_calls,
        degraded_giveup=degraded_giveup,
    )


def _replace_warnings(tier1: TriageResult, warnings: list[str]) -> TriageResult:
    """A copy of ``tier1`` with ``warnings`` swapped (selection untouched)."""
    return _triage.TriageResult(
        selected=tier1.selected,
        warnings=warnings,
        calls=tier1.calls,
        scores=tier1.scores,
        unknown=tier1.unknown,
    )
