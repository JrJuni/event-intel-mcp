"""C1 — entity-relevance gate predicates (pure, unconnected in this slice).

name_is_ambiguous / context_terms / is_relevant_news + the bundled
COMMON_WORDS data file. No call-site behavior changes until C2/B-lane wiring.
"""
from __future__ import annotations

import importlib
import sys

import pytest

from event_intel.events import evidence as E

# Dust's real source_snippet shape from the Pinecone×AIEWF live run.
DUST_SNIPPET = "Enterprise platform for building AI agents grounded in company data"


# ---------- name_is_ambiguous ----------


@pytest.mark.parametrize("name", ["Dust", "Ramp", "Chroma", "Notion", "Anvil"])
def test_single_common_word_names_are_ambiguous(name):
    assert E.name_is_ambiguous(name) is True


@pytest.mark.parametrize(
    "name",
    [
        "LangChain Labs",   # multi-token distinctive
        "LlamaIndex",       # coined compound, not an English word
        "Baseten",          # coined
        "Synaptik",         # coined
        "Data Cloud",       # all-generic → phrase rule territory, not ambiguity
        "",                 # empty
        None,
    ],
)
def test_non_homonym_shapes_are_not_ambiguous(name):
    assert E.name_is_ambiguous(name) is False


def test_generic_suffix_does_not_rescue_a_common_word_name():
    # "Ramp Inc" still reduces to one distinctive common word.
    assert E.name_is_ambiguous("Ramp Inc") is True


def test_wordlist_membership_sanity():
    words = E._common_words()
    assert {"dust", "ramp", "chroma", "snowflake"} <= words
    assert "baseten" not in words
    assert len(words) > 10_000  # the bundled file actually loaded


def test_wordlist_loaded_once_and_lazily():
    # Fresh import must NOT read the file (cold); first call caches a frozenset.
    saved = sys.modules.pop("event_intel.events.evidence", None)
    try:
        mod = importlib.import_module("event_intel.events.evidence")
        assert mod._COMMON_WORDS_CACHE is None  # not loaded at import time
        first = mod._common_words()
        assert mod._common_words() is first     # cached object reused
    finally:
        if saved is not None:
            sys.modules["event_intel.events.evidence"] = saved


# ---------- context_terms ----------


def test_context_terms_drop_stopwords_and_generics():
    terms = E.context_terms(DUST_SNIPPET)
    assert {"enterprise", "building", "agents", "grounded"} <= terms
    assert "for" not in terms        # stopword
    assert "platform" not in terms   # generic company word
    assert "ai" not in terms         # generic (and len<3)
    assert E.context_terms("") == set()
    assert E.context_terms(None) == set()


# ---------- is_relevant_news ----------


def _ctx():
    return E.context_terms(DUST_SNIPPET)


def test_ambiguous_name_with_homonym_article_is_dropped():
    text = "A massive dust storm swept the desert; weather alerts were issued."
    assert E.is_relevant_news(text, name="Dust", ctx_terms=_ctx()) is False


def test_ambiguous_name_with_company_context_passes():
    text = "Dust launches enterprise agents grounded in internal knowledge."
    assert E.is_relevant_news(text, name="Dust", ctx_terms=_ctx()) is True


def test_official_domain_bypasses_everything():
    assert E.is_relevant_news(
        "totally unrelated text", name="Dust", ctx_terms=_ctx(),
        news_domain="dust.tt", official_domain="dust.tt",
    ) is True


def test_name_not_mentioned_fails_regardless():
    assert E.is_relevant_news(
        "An article about something else entirely", name="Dust", ctx_terms=_ctx(),
    ) is False


def test_multi_token_name_keeps_existing_behavior():
    # Non-ambiguous names get NO extra requirement beyond mentions_name.
    text = "LangChain Labs announced something unrelated to agents."
    assert E.is_relevant_news(text, name="LangChain Labs", ctx_terms=set()) is True


def test_empty_context_fails_open_for_ambiguous_name():
    text = "Dust is mentioned here with no other overlap whatsoever."
    assert E.is_relevant_news(text, name="Dust", ctx_terms=set()) is True
    assert E.is_relevant_news(text, name="Dust", ctx_terms=None) is True


def test_own_name_token_in_context_cannot_satisfy_the_gate():
    """Live AIEWF bug (2026-06-11): snippets shaped 'company: Dust |
    description: ...' put 'dust' into ctx_terms — every dust-storm article
    then trivially co-mentioned a 'context' term. The name token must not
    count as disambiguating context."""
    scaffolded = "company: Dust | description: " + DUST_SNIPPET
    ctx = E.context_terms(scaffolded)
    assert "dust" in ctx  # extraction keeps it…
    storm = "Massive dust storm swallows Rajasthan in minutes as skies darken"
    assert E.is_relevant_news(storm, name="Dust", ctx_terms=ctx) is False  # …but it can't satisfy the gate
    real = "Dust launches enterprise agents grounded in internal knowledge."
    assert E.is_relevant_news(real, name="Dust", ctx_terms=ctx) is True


def test_name_only_context_falls_back_to_fail_open():
    """If ctx reduces to NOTHING but the name token, treat it as no context
    (fail-open) rather than dropping every article."""
    text = "Dust is mentioned in an article with zero snippet overlap."
    assert E.is_relevant_news(text, name="Dust", ctx_terms={"dust"}) is True


def test_scaffold_words_are_not_context():
    ctx = E.context_terms("company: Ramp | description: exhibitor booth overview")
    assert not ({"description", "exhibitor", "booth", "overview"} & ctx)


def test_full_multi_token_name_is_phrase_strength():
    """'Together AI' is ambiguous-shaped (distinctive token 'together'), but an
    article containing the FULL name needs no extra context; an article with
    only the bare common word still does."""
    assert E.name_is_ambiguous("Together AI") is True
    full = "Together AI announces a new inference platform this week."
    assert E.is_relevant_news(full, name="Together AI", ctx_terms={"inference"}) is True
    assert E.is_relevant_news(full, name="Together AI", ctx_terms=set()) is True
    bare = "We walked together through the market; nothing else relevant."
    assert E.is_relevant_news(
        bare, name="Together AI", ctx_terms={"inference", "platform"}
    ) is False


def test_missing_wordlist_fails_open(monkeypatch):
    monkeypatch.setattr(E, "_COMMON_WORDS_CACHE", frozenset())
    # With an empty list nothing is ambiguous → gate never strengthens.
    assert E.name_is_ambiguous("Dust") is False
    text = "A dust storm article."
    assert E.is_relevant_news(text, name="Dust", ctx_terms=_ctx()) is True


# ====================================================================== #
# C2 — call-site wiring: enrichment floor gate + buying_signal
# ====================================================================== #


def _enrich_cfg(gate: bool | None):
    cfg = {
        "enrichment": {
            "max_companies": 30, "count_web": 5, "count_news": 5,
            "news_days_back": 180, "cache_enabled": True,
            "official_url_levenshtein_threshold": 0.4,
        },
    }
    if gate is not None:
        cfg["enrichment"]["news_entity_gate"] = {"enabled": gate}
    return cfg


class _NewsOnlySearch:
    def __init__(self, news):
        self._news = news
        self.last_call_degraded = False

    def search(self, query, *, kind, count, days=None, lang="en"):
        return list(self._news) if kind == "news" else []

    def ping(self):  # pragma: no cover
        return {"status": "ok"}


def _dust_news():
    from dataclasses import dataclass

    @dataclass
    class _SR:
        title: str
        url: str
        snippet: str
        source: str | None = None
        published_at: object = None

    return [
        _SR(title="Massive dust storm sweeps the region",
            url="https://weather.example.com/storm", snippet="weather alerts issued"),
        _SR(title="Dust launches enterprise agents",
            url="https://techpress.example.com/dust",
            snippet="agents grounded in company knowledge"),
    ]


def _run_dust_enrichment(tmp_path, gate):
    from event_intel.events.enrichment import enrich_exhibitors
    from event_intel.events.extraction import ExhibitorCandidate

    return enrich_exhibitors(
        candidates=[ExhibitorCandidate(name="Dust", source_snippet=DUST_SNIPPET)],
        workspace_id=f"c2{'on' if gate else 'off'}", lang="en",
        config=_enrich_cfg(gate),
        search_provider=_NewsOnlySearch(_dust_news()),
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )


def test_enrichment_gate_on_drops_homonym_from_floor_evidence(tmp_path):
    result = _run_dust_enrichment(tmp_path, gate=True)
    row = result.rows[0]
    ev_urls = {e.url for e in row.evidence}
    assert "https://techpress.example.com/dust" in ev_urls
    assert "https://weather.example.com/storm" not in ev_urls
    assert len(row.news_signals) == 2  # both still listed (visibility)


def test_enrichment_gate_off_keeps_pre_c2_behavior(tmp_path):
    result = _run_dust_enrichment(tmp_path, gate=None)  # absent key = off
    ev_urls = {e.url for e in result.rows[0].evidence}
    assert "https://weather.example.com/storm" in ev_urls  # old behavior


def test_enrichment_gate_multi_token_name_unchanged(tmp_path):
    from dataclasses import dataclass

    from event_intel.events.enrichment import enrich_exhibitors
    from event_intel.events.extraction import ExhibitorCandidate

    @dataclass
    class _SR:
        title: str
        url: str
        snippet: str
        source: str | None = None
        published_at: object = None

    news = [_SR(title="Synaptik Robotics wins industrial tender",
                url="https://news.example.com/s1", snippet="robotic arms")]
    result = enrich_exhibitors(
        candidates=[ExhibitorCandidate(
            name="Synaptik Robotics", source_snippet="industrial robot arm control",
        )],
        workspace_id="c2multi", lang="en", config=_enrich_cfg(True),
        search_provider=_NewsOnlySearch(news),
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    assert "https://news.example.com/s1" in {e.url for e in result.rows[0].evidence}


# ---------- buying_signal ----------


def _dust_row(news):
    from event_intel.events.enrichment import EnrichedExhibitor, NewsSignal

    return EnrichedExhibitor(
        name="Dust", source_snippet=DUST_SNIPPET,
        news_signals=[
            NewsSignal(title=t, url=u, snippet=s) for (t, u, s) in news
        ],
    )


def test_buying_signal_entity_gate_halves_homonym_only_news():
    from event_intel.scoring.dimensions import score_buying_signal

    homonym = [
        ("Dust storm warning issued", "https://w/1", "weather"),
        ("Dust levels rise in mines", "https://w/2", "safety"),
        ("Cleaning dust from your PC", "https://w/3", "howto"),
    ]
    row = _dust_row(homonym)
    gated = score_buying_signal(row, entity_gate=True)
    ungated = score_buying_signal(row, entity_gate=False)
    assert ungated == pytest.approx(0.6)   # 3 news, token "dust" matches
    assert gated == pytest.approx(0.3)     # gate: none relevant → base halved


def test_buying_signal_entity_gate_keeps_contextual_news():
    from event_intel.scoring.dimensions import score_buying_signal

    row = _dust_row([
        ("Dust ships enterprise agents grounded in company data", "https://t/1", "ai"),
        ("Dust storm hits", "https://w/1", "weather"),
        ("Dust storm again", "https://w/2", "weather"),
    ])
    assert score_buying_signal(row, entity_gate=True) == pytest.approx(0.6)


def test_score_exhibitors_threads_entity_gate_from_config():
    from event_intel.rag.retriever import FitResult
    from event_intel.scoring.compute import score_exhibitors

    row = _dust_row([
        ("Dust storm warning issued", "https://w/1", "weather"),
        ("Dust levels rise in mines", "https://w/2", "safety"),
        ("Cleaning dust from your PC", "https://w/3", "howto"),
    ])
    fit = FitResult(name="Dust", capability_fit=0.8, top_hits=[],
                    capability_fit_breakdown={})
    scoring_cfg = {
        "scoring": {
            "weights": {"buying_signal": 1.0},
            "tier_rules": {
                "S": {"min_final_score": 9.9, "evidence_floor_min": 2},
                "A": {"min_final_score": 9.9, "evidence_floor_min": 1},
                "B": {"min_final_score": 0.0, "evidence_floor_min": 0},
                "C": {"min_final_score": 0.0, "evidence_floor_min": 0},
            },
        },
    }
    off = score_exhibitors(
        enriched=[row], fit_results=[fit], cards=None,
        config=dict(scoring_cfg), top_k=5,
    )
    on_cfg = {**scoring_cfg, "enrichment": {"news_entity_gate": {"enabled": True}}}
    on = score_exhibitors(
        enriched=[row], fit_results=[fit], cards=None, config=on_cfg, top_k=5,
    )
    assert on.rows[0].dimensions.buying_signal < off.rows[0].dimensions.buying_signal


def test_config_fingerprint_includes_entity_gate():
    from event_intel.events.enrichment import _config_fingerprint

    base = {"max_companies": 30}
    with_gate = {"max_companies": 30, "news_entity_gate": {"enabled": True}}
    assert _config_fingerprint(base) != _config_fingerprint(with_gate)
