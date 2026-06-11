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


def test_missing_wordlist_fails_open(monkeypatch):
    monkeypatch.setattr(E, "_COMMON_WORDS_CACHE", frozenset())
    # With an empty list nothing is ambiguous → gate never strengthens.
    assert E.name_is_ambiguous("Dust") is False
    text = "A dust storm article."
    assert E.is_relevant_news(text, name="Dust", ctx_terms=_ctx()) is True
