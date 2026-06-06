"""S4 — fit retrieval tests (event → product, unidirectional).

Uses the same FakeEmbedding / FakeVectorStore shape as test_cards_ingest.py
but with deterministic distances so we can assert the average and breakdown
exactly.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from event_intel.events.enrichment import EnrichedExhibitor, NewsSignal
from event_intel.rag.retriever import (
    FitResult,
    _product_collection_name,
    _similarity_from_distance,
    retrieve_fit_event_to_product,
)


class FakeEmbed:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        # 4-dim, content-derived so identical text → identical vec.
        return [[float(len(t)), 0.1, 0.2, 0.3] for t in texts]


@dataclass
class FakeHit:
    pass  # only used to keep type imports light


class FakeVS:
    """Returns hits supplied by the test via `hits_per_query`. Records every
    query call so tests can assert ONLY event→product happened (no reverse)."""

    def __init__(self):
        self.calls: list[dict] = []
        self.hits_per_query: list[list[dict]] | None = None

    def query(self, *, collection, query_embeddings, top_k=5, where=None):
        self.calls.append({
            "collection": collection,
            "n_queries": len(query_embeddings),
            "top_k": top_k,
            "where": where,
        })
        if self.hits_per_query is None:
            return [[] for _ in query_embeddings]
        # Repeat / pad as needed to match query count.
        out = []
        for i in range(len(query_embeddings)):
            out.append(self.hits_per_query[i % len(self.hits_per_query)])
        return out

    # Unused for retriever tests
    def upsert(self, **kw):  # pragma: no cover
        raise NotImplementedError

    def collection_info(self, collection):  # pragma: no cover
        return {"exists": True, "count": 1}

    def ensure_writable(self):  # pragma: no cover
        return {"status": "writable", "path": "/fake"}


def _row(name, **kw):
    return EnrichedExhibitor(
        name=name,
        source_snippet=kw.get("snippet", "some evidence snippet for " + name),
        url=kw.get("url"),
        official_url=kw.get("official_url"),
        description=kw.get("description"),
        news_signals=kw.get("news_signals", []),
        extraction_confidence=kw.get("extraction_confidence", 1.0),
    )


def test_collection_name_matches_preflight_convention():
    from event_intel.runtime.preflight import _product_collection_name as p

    assert _product_collection_name("default") == p("default")
    assert _product_collection_name("acme") == p("acme")


def test_similarity_from_distance_clamps():
    assert _similarity_from_distance(0.0) == 1.0
    assert _similarity_from_distance(2.0) == 0.0
    assert _similarity_from_distance(1.0) == 0.5
    assert _similarity_from_distance(None) == 0.0


def test_retrieve_averages_topk_similarity_and_breakdown():
    rows = [_row("Mobius")]
    vs = FakeVS()
    vs.hits_per_query = [[
        {"id": "cap:0:Quantization-aware compile", "document": "...", "distance": 0.2,
         "metadata": {"kind": "capability", "capability_name": "Quantization-aware compile"}},
        {"id": "cap:1:Cross-vendor NPU backend", "document": "...", "distance": 0.4,
         "metadata": {"kind": "capability", "capability_name": "Cross-vendor NPU backend"}},
        {"id": "product:summary", "document": "...", "distance": 0.6,
         "metadata": {"kind": "product_summary"}},
        {"id": "ideal_customer:industries", "document": "...", "distance": 0.8,
         "metadata": {"kind": "ideal_customer", "facet": "industries"}},
        {"id": "cap:0:Quantization-aware compile", "document": "...", "distance": 1.0,
         "metadata": {"kind": "capability", "capability_name": "Quantization-aware compile"}},
    ]]
    results = retrieve_fit_event_to_product(
        exhibitors=rows, workspace_id="acme",
        embedding_provider=FakeEmbed(), vectorstore_provider=vs, top_k=5,
    )
    assert len(results) == 1
    r = results[0]
    # capability_fit averages ONLY the 3 capability-kind hits (dist 0.2/0.4/1.0
    # → sims 0.9/0.8/0.5). product_summary + ideal_customer are excluded.
    assert r.capability_fit == pytest.approx((0.9 + 0.8 + 0.5) / 3, abs=1e-6)
    assert r.capability_fit_breakdown["Quantization-aware compile"] == 2
    assert r.capability_fit_breakdown["Cross-vendor NPU backend"] == 1


def test_retrieve_counts_competitor_and_bad_fit_hits():
    rows = [_row("Mobius")]
    vs = FakeVS()
    vs.hits_per_query = [[
        {"id": "competitor:0:Edge Impulse Compiler", "document": "...", "distance": 0.5,
         "metadata": {"kind": "competitor", "competitor_name": "Edge Impulse Compiler"}},
        {"id": "bad_fit:0", "document": "...", "distance": 0.5,
         "metadata": {"kind": "bad_fit"}},
        {"id": "cap:0:X", "document": "...", "distance": 0.5,
         "metadata": {"kind": "capability", "capability_name": "X"}},
    ]]
    fit = retrieve_fit_event_to_product(
        exhibitors=rows, workspace_id="acme",
        embedding_provider=FakeEmbed(), vectorstore_provider=vs, top_k=3,
    )[0]
    assert fit.competitor_hits == 1
    assert fit.bad_fit_hits == 1
    assert fit.capability_fit_breakdown == {"X": 1}
    # capability_fit comes from the single capability hit only (dist 0.5 → 0.75),
    # NOT diluted/inflated by the competitor + bad_fit chunks.
    assert fit.capability_fit == pytest.approx(0.75, abs=1e-6)
    # 4b: penalty drivers are the max similarity per kind (dist 0.5 → 0.75).
    assert fit.competitor_similarity == pytest.approx(0.75, abs=1e-6)
    assert fit.bad_fit_similarity == pytest.approx(0.75, abs=1e-6)


def test_retrieve_all_competitor_hits_yield_zero_capability_fit():
    """A row whose top-k is entirely competitor chunks (semantically a
    competitor) gets capability_fit 0.0 — the contamination fix."""
    rows = [_row("Snowflake-like")]
    vs = FakeVS()
    vs.hits_per_query = [[
        {"id": "competitor:0", "document": "...", "distance": 0.1,
         "metadata": {"kind": "competitor", "competitor_name": "Snowflake"}},
        {"id": "competitor:1", "document": "...", "distance": 0.2,
         "metadata": {"kind": "competitor", "competitor_name": "ClickHouse"}},
        {"id": "bad_fit:0", "document": "...", "distance": 0.3,
         "metadata": {"kind": "bad_fit"}},
    ]]
    fit = retrieve_fit_event_to_product(
        exhibitors=rows, workspace_id="acme",
        embedding_provider=FakeEmbed(), vectorstore_provider=vs, top_k=3,
    )[0]
    assert fit.capability_fit == 0.0
    assert fit.competitor_hits == 2
    assert fit.bad_fit_hits == 1
    assert fit.capability_fit_breakdown == {}


def test_retrieve_only_queries_product_collection_not_event_collection():
    """v0 is unidirectional. Confirm we never query an event_* collection."""
    rows = [_row("Mobius"), _row("Neuro")]
    vs = FakeVS()
    vs.hits_per_query = [[]]
    retrieve_fit_event_to_product(
        exhibitors=rows, workspace_id="acme",
        embedding_provider=FakeEmbed(), vectorstore_provider=vs, top_k=5,
    )
    collections_queried = {c["collection"] for c in vs.calls}
    assert collections_queried == {"product_acme"}, vs.calls
    # Two batched query calls (capability pool + negative pool), not one per
    # exhibitor. Both carry a kind `where` filter.
    assert len(vs.calls) == 2
    assert all(c["n_queries"] == 2 for c in vs.calls)
    wheres = [c["where"] for c in vs.calls]
    assert {"kind": "capability"} in wheres
    assert {"kind": {"$in": ["competitor", "bad_fit"]}} in wheres


def test_retrieve_empty_input_returns_empty_list():
    assert retrieve_fit_event_to_product(
        exhibitors=[], workspace_id="acme",
        embedding_provider=FakeEmbed(), vectorstore_provider=FakeVS(), top_k=5,
    ) == []
