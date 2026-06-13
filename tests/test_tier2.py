"""Y1D E3 — Tier-2 adaptive search corner-case suite (offline, fakes only).

Covers: UNKNOWN → fit/no-fit resolution + re-selection; the skip gate (Tier 1
already filled the shortlist); the no-op gates (disabled / no provider / no
digest / no UNKNOWN); roster-size auto-gating (brute-force small vs ceiling
large); adaptive early-stop; budget cap + unsearched logging; rate-limit
give-up; robustness (empty results, search raises, evidence-but-still-unknown);
in-place profile_text mutation; warning folding + call accounting.

No network: a fake SearchProvider and a name→score fake LLM are injected.
"""
from __future__ import annotations

from dataclasses import dataclass

from event_intel.events import tier2 as _tier2
from event_intel.events import triage as _triage
from event_intel.providers import llm as _llm
from event_intel.providers import search as _search

_DIGEST = "Acme Vector DB — fast similarity search for AI infra teams."


@dataclass
class _Cand:
    name: str
    source_snippet: str = ""
    profile_text: str | None = None
    url: str | None = None


class _MapLLM:
    """Re-triage fake: maps the company NAME (read from the roster listing) to a
    float fit or the literal "unknown". Unlisted names default to "unknown".
    """

    def __init__(self, mapping: dict[str, object]):
        self.mapping = mapping
        self.calls = 0
        self.model = "map-llm"

    def chat_once(self, *, system, user, max_tokens, temperature):
        import json

        self.calls += 1
        scores: dict[str, object] = {}
        for line in user.splitlines():
            head, _, _ = line.partition(" — ")
            idx_s, _, name = head.partition(". ")
            if idx_s.strip().isdigit() and name:
                scores[idx_s.strip()] = self.mapping.get(name.strip(), "unknown")
        return _llm.LLMResponse(
            text=json.dumps({"scores": scores}),
            usage={"input_tokens": 10, "output_tokens": 5},
            model=self.model,
        )


class _FakeSearch:
    """One web hit per query. ``results`` maps name → snippet text (None = no
    results). ``degrade=True`` makes every call report rate-limited + empty.
    """

    def __init__(self, results: dict[str, str | None] | None = None, *, degrade=False):
        self.results = results or {}
        self.degrade = degrade
        self.queries: list[str] = []
        self.last_call_degraded = False

    def search(self, query, *, kind, count, days=None, lang="en"):
        self.queries.append(query)
        if self.degrade:
            self.last_call_degraded = True
            return []
        self.last_call_degraded = False
        name = query.strip('"')
        snippet = self.results.get(name, "generic company that builds AI products")
        if snippet is None:
            return []
        return [_search.SearchResult(title=name, url="", snippet=snippet)]

    def ping(self):  # pragma: no cover
        return {"status": "ok"}


def _tier1(scores: dict[int, float], unknown: set[int], selected_names) -> _triage.TriageResult:
    return _triage.TriageResult(
        selected=list(selected_names),
        warnings=["triage: selected (stub)"],
        calls=1,
        scores=dict(scores),
        unknown=set(unknown),
    )


# ---------------------------------------------------------------- resolution


def test_unknown_resolves_to_fit_and_enters_selection():
    cands = [_Cand("LowFit Co"), _Cand("Mystery Co"), _Cand("Other Co")]
    # Tier 1: idx0 KNOWN_NOFIT, idx1/idx2 UNKNOWN. cap 2 → UNKNOWN selected.
    tier1 = _tier1({0: 0.1}, {1, 2}, [cands[1], cands[2]])
    llm = _MapLLM({"Mystery Co": 0.9, "Other Co": 0.05})
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, _FakeSearch(), llm,
        max_companies=2, cfg=_tier2.Tier2Config(),
    )
    names = [c.name for c in res.triage.selected]
    assert "Mystery Co" in names                 # resolved fit kept
    assert res.resolved_fit == 1 and res.resolved_nofit == 1
    assert res.triage.scores[1] == 0.9 and 1 not in res.triage.unknown
    assert res.calls == 1                          # one re-triage call


def test_search_evidence_makes_competitor_drop_below_selection():
    cands = [_Cand("KnownFit Co"), _Cand("Lookalike Co")]
    # cap 1: Tier-1 KNOWN_FIT idx0 already wins, but force tier2 by leaving a
    # contention slot — use cap 2 with one fit + one unknown competitor.
    tier1 = _tier1({0: 0.9}, {1}, [cands[0], cands[1]])
    llm = _MapLLM({"Lookalike Co": 0.0})       # search reveals it's a competitor
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, _FakeSearch(), llm,
        max_companies=2, cfg=_tier2.Tier2Config(),
    )
    assert res.resolved_nofit == 1
    assert res.triage.scores[1] == 0.0 and 1 not in res.triage.unknown


def test_profile_text_mutated_in_place_for_searched_companies():
    cands = [_Cand("Fit Co"), _Cand("Seek Co")]
    tier1 = _tier1({0: 0.6}, {1}, [cands[0], cands[1]])
    llm = _MapLLM({"Seek Co": 0.8})
    _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, _FakeSearch({"Seek Co": "builds warehouse robots"}),
        llm, max_companies=2, cfg=_tier2.Tier2Config(),
    )
    assert cands[1].profile_text and "warehouse robots" in cands[1].profile_text


# ---------------------------------------------------------------- gates


def test_skip_when_tier1_filled_shortlist():
    cands = [_Cand("A"), _Cand("B"), _Cand("C")]
    # Two KNOWN_FIT for cap 2 → UNKNOWN idx2 can never be selected.
    tier1 = _tier1({0: 0.9, 1: 0.8}, {2}, [cands[0], cands[1]])
    search = _FakeSearch()
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, search, _MapLLM({}),
        max_companies=2, cfg=_tier2.Tier2Config(),
    )
    assert search.queries == []                    # zero searches
    assert res.searched == 0
    assert any("skipped" in w for w in res.triage.warnings)


def test_noop_when_no_unknown():
    cands = [_Cand("A"), _Cand("B")]
    tier1 = _tier1({0: 0.9, 1: 0.1}, set(), [cands[0]])
    search = _FakeSearch()
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, search, _MapLLM({}),
        max_companies=1, cfg=_tier2.Tier2Config(),
    )
    assert res.triage is tier1 and search.queries == []


def test_noop_when_disabled():
    cands = [_Cand("A"), _Cand("B")]
    tier1 = _tier1({0: 0.1}, {1}, [cands[0]])
    search = _FakeSearch()
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, search, _MapLLM({}),
        max_companies=1, cfg=_tier2.Tier2Config(enabled=False),
    )
    assert res.triage is tier1 and search.queries == []


def test_noop_when_no_provider_or_digest():
    cands = [_Cand("A"), _Cand("B")]
    tier1 = _tier1({0: 0.1}, {1}, [cands[0]])
    assert _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, None, _MapLLM({}),
        max_companies=1, cfg=_tier2.Tier2Config(),
    ).triage is tier1
    assert _tier2.resolve_unknowns(
        cands, tier1, "", _FakeSearch(), _MapLLM({}),
        max_companies=1, cfg=_tier2.Tier2Config(),
    ).triage is tier1


# ---------------------------------------------------------------- gating / adaptive


def test_small_roster_brute_forces_every_unknown():
    cands = [_Cand(f"C{i}") for i in range(5)]
    tier1 = _tier1({0: 0.1}, {1, 2, 3, 4}, [])
    search = _FakeSearch()                          # all default → "unknown" re-triage
    llm = _MapLLM({})                               # everything stays unknown
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, search, llm,
        max_companies=10,                           # cap high → no early-stop
        cfg=_tier2.Tier2Config(small_roster_threshold=300, search_batch=2),
    )
    assert res.searched == 4 and len(search.queries) == 4   # every UNKNOWN searched


def test_large_roster_caps_searches_and_logs_unsearched():
    cands = [_Cand(f"C{i}") for i in range(6)]
    tier1 = _tier1({0: 0.1}, {1, 2, 3, 4, 5}, [])
    search = _FakeSearch()
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, search, _MapLLM({}),
        max_companies=10,
        cfg=_tier2.Tier2Config(
            small_roster_threshold=2,               # total 6 > 2 → ceiling path
            max_searches_per_event=3, search_batch=2,
        ),
    )
    assert res.searched == 3                         # capped
    assert res.unsearched_unknown == 2
    assert any("left unsearched" in w for w in res.triage.warnings)


def test_adaptive_early_stop_once_cap_filled():
    cands = [_Cand(f"C{i}") for i in range(6)]
    tier1 = _tier1({}, {0, 1, 2, 3, 4, 5}, [])      # all UNKNOWN
    # Every searched company resolves to FIT; cap 2 → should stop after 2.
    llm = _MapLLM({f"C{i}": 0.9 for i in range(6)})
    search = _FakeSearch()
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, search, llm,
        max_companies=2,
        cfg=_tier2.Tier2Config(small_roster_threshold=300, search_batch=1),
    )
    assert res.searched == 2                         # early-stop, not all 6
    assert res.resolved_fit == 2
    assert res.unsearched_unknown == 4


# ---------------------------------------------------------------- robustness


def test_search_empty_keeps_company_unknown():
    cands = [_Cand("Fit Co"), _Cand("Ghost Co")]
    tier1 = _tier1({0: 0.6}, {1}, [cands[0], cands[1]])
    search = _FakeSearch({"Ghost Co": None})        # no results
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, search, _MapLLM({}),
        max_companies=2, cfg=_tier2.Tier2Config(),
    )
    assert 1 in res.triage.unknown                   # stayed UNKNOWN
    assert cands[1].profile_text is None
    assert res.still_unknown == 1


def test_search_raises_is_swallowed():
    class _Boom(_FakeSearch):
        def search(self, *a, **k):
            self.queries.append(a)
            raise RuntimeError("transport down")

    cands = [_Cand("Fit Co"), _Cand("Boom Co")]
    tier1 = _tier1({0: 0.6}, {1}, [cands[0], cands[1]])
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, _Boom(), _MapLLM({}),
        max_companies=2, cfg=_tier2.Tier2Config(),
    )
    assert 1 in res.triage.unknown                   # no crash, stayed UNKNOWN


def test_evidence_found_but_still_unknown_on_retriage():
    cands = [_Cand("Fit Co"), _Cand("Vague Co")]
    tier1 = _tier1({0: 0.6}, {1}, [cands[0], cands[1]])
    search = _FakeSearch({"Vague Co": "a holding company"})
    llm = _MapLLM({"Vague Co": "unknown"})           # re-triage can't decide
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, search, llm,
        max_companies=2, cfg=_tier2.Tier2Config(),
    )
    assert 1 in res.triage.unknown and res.still_unknown == 1
    assert cands[1].profile_text is not None          # evidence WAS attached
    assert res.calls == 1                              # re-triage still happened


def test_rate_limit_giveup_aborts():
    cands = [_Cand(f"C{i}") for i in range(8)]
    tier1 = _tier1({}, set(range(8)), [])
    search = _FakeSearch(degrade=True)               # every call rate-limited
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, search, _MapLLM({}),
        max_companies=4,
        cfg=_tier2.Tier2Config(small_roster_threshold=300, search_batch=5),
    )
    assert res.degraded_giveup is True
    assert res.searched == _tier2._DEGRADED_GIVEUP   # aborted at the threshold
    assert any("rate-limited" in w for w in res.triage.warnings)


# ---------------------------------------------------------------- bookkeeping


def test_warnings_folded_and_calls_accumulated():
    cands = [_Cand("Fit Co"), _Cand("Seek Co")]
    tier1 = _tier1({0: 0.6}, {1}, [cands[0], cands[1]])
    llm = _MapLLM({"Seek Co": 0.8})
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, _FakeSearch(), llm,
        max_companies=2, cfg=_tier2.Tier2Config(),
    )
    assert any("triage: selected (stub)" in w for w in res.triage.warnings)  # Tier-1 kept
    assert any(w.startswith("tier2: searched") for w in res.triage.warnings)
    assert res.triage.calls == tier1.calls + res.calls


def test_selected_returned_in_roster_order():
    cands = [_Cand("Zeta"), _Cand("Alpha"), _Cand("Beta")]
    tier1 = _tier1({0: 0.1}, {1, 2}, [cands[1], cands[2]])
    llm = _MapLLM({"Alpha": 0.9, "Beta": 0.9})
    res = _tier2.resolve_unknowns(
        cands, tier1, _DIGEST, _FakeSearch(), llm,
        max_companies=2, cfg=_tier2.Tier2Config(),
    )
    sel = [c.name for c in res.triage.selected]
    assert sel == ["Alpha", "Beta"]                  # roster order (idx1, idx2)
