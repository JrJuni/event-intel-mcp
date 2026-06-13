"""Y1D D2 — LLM roster triage unit tests (events/triage.py).

Corner-case set (self-generated, slice discipline):
- roster ≤ cap → zero LLM calls, identity order, no warnings
- over-cap → top-K by band, ORIGINAL roster order preserved, selection warning
- E2 two-signal: "unknown" parses to None; UNKNOWN companies kept ahead of
  KNOWN-but-low-fit ones (KNOWN_FIT > UNKNOWN > KNOWN_NOFIT); fit_cutoff moves
  the FIT/NOFIT boundary; profile_text used as evidence over the bare snippet
- one batch malformed → that batch's companies become UNKNOWN + warning, other
  batches scored
- every batch fails → exact first-N fallback (all-UNKNOWN ranks in roster order)
  + dedicated warning
- target-fit axis (#17): resolved target_mode injected into the prompt; low-fit
  company (incl. competitor) is cut, no forced pass-through
- global indices across batches (misalignment would silently rank wrong rows)
- parse: clamp / NaN / inf / non-numeric entries / "unknown" / fenced /
  no-wrapper / garbage
- digest: happy path + customer profile (signals/pains/bad-fit), None cards,
  broken cards object (never raises)
- ledger records stage="triage" with real usage; ledger=None safe
"""
from __future__ import annotations

import json
import math

from event_intel.cards.schema import CapabilityCards
from event_intel.events import triage as _triage
from event_intel.events.extraction import ExhibitorCandidate
from event_intel.providers import llm as _llm
from event_intel.runtime.llm_ledger import LlmUsageLedger


def _cand(name: str, snippet: str = "exhibitor snippet") -> ExhibitorCandidate:
    return ExhibitorCandidate(name=name, source_snippet=snippet)


def _cards() -> CapabilityCards:
    return CapabilityCards(
        product_name="Acme Vector DB",
        one_liner="Vector database for AI retrieval workloads.",
        capabilities=[
            {
                "name": "Vector search",
                "keywords": ["vector", "embedding", "ANN"],
                "buyer_pains": ["slow retrieval"],
                "evidence_queries": ["vector db"],
            }
        ],
        ideal_customer={
            "industries": ["AI infrastructure", "SaaS"],
            "company_signals": ["ships an AI product"],
        },
        bad_fit=[{"reason": "we are not a consultancy", "keywords": ["consulting"]}],
    )


class _TriageLLM:
    """Scores companies whose name contains 'fit' high, others low. Replies
    only for the indices listed in the prompt it received (like a real model).
    """

    def __init__(self, reply_overrides: dict[int, str] | None = None):
        self.calls = 0
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.model = "fake-triage-model"
        self._overrides = reply_overrides or {}

    def _score(self, name: str) -> float:
        return 0.9 if "fit" in name.lower() else 0.1

    def chat_once(self, *, system, user, max_tokens, temperature):
        call_idx = self.calls
        self.calls += 1
        self.prompts.append(user)
        self.systems.append(system)
        if call_idx in self._overrides:
            text = self._overrides[call_idx]
        else:
            scores = {}
            for line in user.splitlines():
                head, _, _ = line.partition(" — ")
                idx_s, _, name = head.partition(". ")
                if idx_s.strip().isdigit() and name:
                    scores[idx_s.strip()] = self._score(name)
            text = json.dumps({"scores": scores})
        return _llm.LLMResponse(
            text=text,
            usage={"input_tokens": 200, "output_tokens": 40},
            model=self.model,
        )


class _ExplodingLLM:
    model = "exploding"

    def __init__(self):
        self.calls = 0

    def chat_once(self, **kwargs):
        self.calls += 1
        raise RuntimeError("transport down")


class _MapLLM:
    """Returns a fixed value per company NAME — a float, or the string
    "unknown" for a no-evidence verdict. Unlisted names default to 0.1.
    """

    def __init__(self, mapping: dict[str, object]):
        self.mapping = mapping
        self.calls = 0
        self.model = "map-llm"
        self.prompts: list[str] = []

    def chat_once(self, *, system, user, max_tokens, temperature):
        self.calls += 1
        self.prompts.append(user)
        scores: dict[str, object] = {}
        for line in user.splitlines():
            head, _, _ = line.partition(" — ")
            idx_s, _, name = head.partition(". ")
            if idx_s.strip().isdigit() and name:
                scores[idx_s.strip()] = self.mapping.get(name.strip(), 0.1)
        return _llm.LLMResponse(
            text=json.dumps({"scores": scores}),
            usage={"input_tokens": 10, "output_tokens": 5},
            model=self.model,
        )


# ---------------------------------------------------------------- parse


def test_parse_valid_scores_map():
    out = _triage.parse_triage_response('{"scores": {"0": 0.8, "1": 0.2}}')
    assert out == {0: 0.8, 1: 0.2}


def test_parse_accepts_top_level_map_and_fenced():
    out = _triage.parse_triage_response('```json\n{"3": 1.0, "4": 0.0}\n```')
    assert out == {3: 1.0, 4: 0.0}


def test_parse_clamps_and_drops_bad_entries():
    out = _triage.parse_triage_response(
        '{"scores": {"0": 1.7, "1": -0.3, "2": NaN, "3": Infinity, '
        '"4": "high", "x": 0.5, "-2": 0.5}}'
    )
    # json.loads accepts NaN/Infinity literals — they must be rejected here,
    # along with non-numeric scores, non-int and negative keys.
    assert out == {0: 1.0, 1: 0.0}


def test_parse_unusable_inputs():
    assert _triage.parse_triage_response(None) is None
    assert _triage.parse_triage_response("") is None
    assert _triage.parse_triage_response("no json here") is None
    assert _triage.parse_triage_response("[0.1, 0.2]") is None
    assert _triage.parse_triage_response('{"scores": {"x": "y"}}') is None


def test_parse_unknown_maps_to_none():
    # E2: the literal "unknown" (any case) is an explicit no-evidence verdict,
    # parsed to None and routed to the UNKNOWN band — NOT dropped, NOT a number.
    out = _triage.parse_triage_response(
        '{"scores": {"0": 0.7, "1": "unknown", "2": "UNKNOWN", "3": "y"}}'
    )
    assert out == {0: 0.7, 1: None, 2: None}   # "3":"y" is non-numeric → dropped


def test_parse_all_unknown_is_not_none():
    # A response that is all-unknown is still usable (truthy dict of Nones),
    # not a parse failure — those companies are KNOWN-unknown, not unscored.
    assert _triage.parse_triage_response('{"scores": {"0": "unknown"}}') == {0: None}


# ---------------------------------------------------------------- digest


def test_digest_contains_product_capabilities_industries():
    digest = _triage.build_capability_digest(_cards())
    assert "Acme Vector DB" in digest
    assert "Vector search" in digest
    assert "embedding" in digest
    assert "AI infrastructure" in digest


def test_digest_carries_customer_profile_for_target_fit():
    # #17: target-fit scoring needs the customer profile, not just product
    # domain vocabulary — signals, buyer pains, and bad-fit keywords.
    digest = _triage.build_capability_digest(_cards())
    assert "ships an AI product" in digest      # ideal_customer.company_signals
    assert "slow retrieval" in digest            # capability buyer_pains
    assert "consulting" in digest                # bad_fit keywords


def test_digest_none_and_broken_cards():
    assert _triage.build_capability_digest(None) is None
    assert _triage.build_capability_digest(object()) is None  # never raises


# ---------------------------------------------------------------- triage_roster


def test_under_cap_zero_calls_identity_order():
    llm = _TriageLLM()
    roster = [_cand("A"), _cand("B")]
    res = _triage.triage_roster(
        roster, "digest", llm, max_companies=5,
    )
    assert llm.calls == 0
    assert [c.name for c in res.selected] == ["A", "B"]
    assert res.warnings == []


def test_over_cap_selects_top_k_in_roster_order():
    llm = _TriageLLM()
    ledger = LlmUsageLedger()
    roster = [
        _cand("NoMatch One"), _cand("FitCo Alpha"), _cand("NoMatch Two"),
        _cand("FitCo Beta"), _cand("NoMatch Three"), _cand("FitCo Gamma"),
    ]
    res = _triage.triage_roster(
        roster, _triage.build_capability_digest(_cards()), llm,
        max_companies=3, ledger=ledger,
    )
    # top-3 by score, returned in ORIGINAL roster order
    assert [c.name for c in res.selected] == ["FitCo Alpha", "FitCo Beta", "FitCo Gamma"]
    assert llm.calls == 1
    assert any("selected 3/6" in w for w in res.warnings)
    stage = ledger.summary()["stages"]["triage"]
    assert stage["calls"] == 1
    assert stage["input_tokens"] == 200
    assert stage["models"] == ["fake-triage-model"]
    # prompt carried the digest + the target-fit axis (default mode customer)
    # + the evidence-first instruction (E2: name is not evidence, "unknown")
    assert "Acme Vector DB" in llm.prompts[0]
    assert "TARGET MODE: customer" in llm.prompts[0]
    assert "not\nevidence" in llm.prompts[0] or "not evidence" in llm.prompts[0]
    assert '"unknown"' in llm.prompts[0]


def test_batches_use_global_indices():
    llm = _TriageLLM()
    roster = [_cand(f"Company {i}") for i in range(249)] + [_cand("FitCo Tail")]
    res = _triage.triage_roster(
        roster, "digest", llm, max_companies=2, batch_size=100,
    )
    assert llm.calls == 3
    # last batch lists GLOBAL indices (200..249), not 0-based batch indices
    assert "249. FitCo Tail" in llm.prompts[2]
    # the only high scorer (global index 249) must be selected — a local-index
    # mixup would lose it
    assert any(c.name == "FitCo Tail" for c in res.selected)


def test_malformed_batch_goes_unknown_other_batches_scored():
    # batch 0 returns garbage → its 2 companies become UNKNOWN; batch 1 scores
    # normally → FitCo Late (0.9, KNOWN_FIT) wins, NoMatch Late (0.1, NOFIT)
    # loses. With cap 2: FitCo Late (band FIT) + the first UNKNOWN (band UNKNOWN,
    # kept ahead of the low-fit NoMatch). Returned in roster order.
    llm = _TriageLLM(reply_overrides={0: "sorry, cannot help"})
    roster = [_cand("Neutral A"), _cand("Neutral B"),
              _cand("FitCo Late"), _cand("NoMatch Late")]
    res = _triage.triage_roster(
        roster, "digest", llm, max_companies=2, batch_size=2,
    )
    assert [c.name for c in res.selected] == ["Neutral A", "FitCo Late"]
    assert res.unknown == {0, 1}
    assert any("1/2 batches unscored" in w for w in res.warnings)
    assert any("selected 2/4" in w for w in res.warnings)


def test_all_batches_fail_first_n_fallback():
    llm = _ExplodingLLM()
    roster = [_cand(f"C{i}") for i in range(5)]
    res = _triage.triage_roster(
        roster, "digest", llm, max_companies=2, batch_size=2,
    )
    assert llm.calls == 3
    assert [c.name for c in res.selected] == ["C0", "C1"]  # exact old behaviour
    assert any("all 3 batches failed" in w for w in res.warnings)
    assert not any("selected" in w for w in res.warnings)


def test_no_digest_first_n_fallback_zero_calls():
    llm = _TriageLLM()
    roster = [_cand(f"C{i}") for i in range(4)]
    res = _triage.triage_roster(
        roster, None, llm, max_companies=2,
    )
    assert llm.calls == 0
    assert [c.name for c in res.selected] == ["C0", "C1"]
    assert any("no capability digest" in w for w in res.warnings)


def test_low_target_fit_company_is_cut_no_forced_pass():
    # #17 contract change: triage ranks PURELY by the LLM's target-fit score.
    # There is no "competitors must pass" guarantee anymore — a company the
    # model scores low (here a competitor/look-alike that isn't a customer)
    # is cut in favour of higher-fit targets. competitor_penalty still applies
    # later to whatever does reach scoring.
    llm = _TriageLLM()  # scores names containing 'fit' high, else low
    roster = [_cand("Rival Vector DB"), _cand("FitCo Target A"),
              _cand("Lookalike Search"), _cand("FitCo Target B")]
    res = _triage.triage_roster(
        roster, "digest", llm, max_companies=2,
    )
    names = [c.name for c in res.selected]
    assert names == ["FitCo Target A", "FitCo Target B"]
    assert "Rival Vector DB" not in names


def test_prompt_carries_resolved_target_mode():
    # The resolved target_mode is injected into the prompt so the rubric scores
    # for the right kind of target (customer vs partner vs ecosystem).
    llm = _TriageLLM()
    roster = [_cand(f"C{i}") for i in range(4)]
    _triage.triage_roster(
        roster, "digest", llm, max_companies=2, target_mode="partner",
    )
    assert "TARGET MODE: partner" in llm.prompts[0]


def test_neutral_ties_keep_roster_order():
    # Stable selection: all-equal scores → first-N exactly (no reordering).
    class _FlatLLM(_TriageLLM):
        def _score(self, name):
            return 0.5

    llm = _FlatLLM()
    roster = [_cand(f"C{i}") for i in range(6)]
    res = _triage.triage_roster(
        roster, "digest", llm, max_companies=3,
    )
    assert [c.name for c in res.selected] == ["C0", "C1", "C2"]


def test_unparseable_reply_still_records_ledger_usage():
    llm = _TriageLLM(reply_overrides={0: "%%%"})
    ledger = LlmUsageLedger()
    roster = [_cand(f"C{i}") for i in range(3)]
    _triage.triage_roster(
        roster, "digest", llm, max_companies=1, ledger=ledger,
    )
    stage = ledger.summary()["stages"]["triage"]
    assert stage["calls"] == 1
    assert stage["input_tokens"] == 200  # tokens were spent even if unusable


def test_ledger_none_and_min_cap_guard():
    llm = _TriageLLM()
    roster = [_cand("FitCo"), _cand("NoMatch")]
    res = _triage.triage_roster(
        roster, "digest", llm, max_companies=0,  # clamped to 1
    )
    assert len(res.selected) == 1
    assert res.selected[0].name == "FitCo"


def test_scores_diagnostic_map_covers_full_roster():
    llm = _TriageLLM()
    roster = [_cand(f"C{i}") for i in range(5)]
    res = _triage.triage_roster(
        roster, "digest", llm, max_companies=2, batch_size=3,
    )
    assert set(res.scores) == set(range(5))
    assert all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in res.scores.values())


def test_prompt_lang_ko_and_unknown_fallback():
    ko = _triage.load_triage_prompt("ko")
    assert "타깃 모드" in ko          # localized rubric present
    assert "unknown" in ko           # the no-evidence verdict token is kept verbatim
    assert _triage.load_triage_prompt("fr") == _triage.load_triage_prompt("en")


# ---------------------------------------------------------------- E2 two-signal


def test_three_band_selection_fit_then_unknown_then_nofit():
    # KNOWN_FIT (≥cutoff, fit desc) > UNKNOWN (roster order) > KNOWN_NOFIT.
    # An UNKNOWN company outranks a KNOWN-but-low-fit one — no middle/low score
    # is ever forced onto absent evidence.
    roster = [_cand("LowFit Co"), _cand("Unknown Co"),
              _cand("HighFit Co"), _cand("MidFit Co")]
    llm = _MapLLM({
        "LowFit Co": 0.2, "Unknown Co": "unknown",
        "HighFit Co": 0.9, "MidFit Co": 0.6,
    })
    res = _triage.triage_roster(roster, "digest", llm, max_companies=3)
    # top-3 indices {1,2,3} → roster order; LowFit (KNOWN_NOFIT) is cut.
    assert [c.name for c in res.selected] == ["Unknown Co", "HighFit Co", "MidFit Co"]
    assert res.unknown == {1}
    assert res.scores == {0: 0.2, 2: 0.9, 3: 0.6}
    assert any("had no usable evidence" in w for w in res.warnings)


def test_fit_cutoff_controls_band_vs_unknown():
    # The same 0.45 fit lands in different bands relative to an UNKNOWN company
    # depending on the cutoff — proving fit_cutoff drives the FIT/NOFIT split.
    def winner(cutoff: float) -> str:
        roster = [_cand("Borderline"), _cand("Unknown Co")]
        llm = _MapLLM({"Borderline": 0.45, "Unknown Co": "unknown"})
        return _triage.triage_roster(
            roster, "digest", llm, max_companies=1, fit_cutoff=cutoff,
        ).selected[0].name

    # 0.45 < 0.5 → Borderline is KNOWN_NOFIT, ranks BELOW unknown → unknown wins
    assert winner(0.5) == "Unknown Co"
    # 0.45 ≥ 0.4 → Borderline is KNOWN_FIT, ranks ABOVE unknown → Borderline wins
    assert winner(0.4) == "Borderline"


def test_profile_text_used_as_evidence_over_snippet():
    # E2 Tier-1 wiring: when profile_text is populated, the roster listing the
    # LLM sees carries it (what the company does), NOT the bare CSV snippet.
    c = ExhibitorCandidate(name="Acme", source_snippet="CSV row 7: Acme | https://x")
    c.profile_text = "Acme builds autonomous warehouse robots for logistics."
    roster = [c, _cand("Filler One"), _cand("Filler Two")]
    llm = _TriageLLM()
    _triage.triage_roster(roster, "digest", llm, max_companies=2)
    prompt = llm.prompts[0]
    assert "autonomous warehouse robots" in prompt
    assert "CSV row 7" not in prompt          # profile replaces the bare snippet


def test_all_unknown_keeps_roster_order_like_first_n():
    # Every company UNKNOWN (no evidence anywhere) → pure roster order, exactly
    # the first-N guarantee, but WITHOUT inventing scores.
    roster = [_cand(f"C{i}") for i in range(5)]
    llm = _MapLLM({f"C{i}": "unknown" for i in range(5)})
    res = _triage.triage_roster(roster, "digest", llm, max_companies=2)
    assert [c.name for c in res.selected] == ["C0", "C1"]
    assert res.scores == {}
    assert res.unknown == set(range(5))
