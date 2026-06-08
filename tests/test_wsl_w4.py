"""W4 — ingest sync hook (opt-in) + rationale provenance (schema bump) +
run_summary source_index_fingerprint.

The headline guarantee: attaching source provenance NEVER changes any scoring
field — it only adds a separate report key.
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from event_intel.events.enrichment import EnrichedExhibitor, NewsSignal
from event_intel.rag.retriever import FitResult
from event_intel.report.tier_list_md import ReportContext, render_tier_list_md
from event_intel.report.tier_list_yaml import (
    REPORT_SCHEMA_VERSION,
    build_tier_list_payload,
)
from event_intel.scoring.compute import ScoredExhibitor, ScoringSummary
from event_intel.scoring.dimensions import DimensionScores
from event_intel.sources import retrieval as R


def _scored(name, tier, *, score, floor):
    news = [NewsSignal(title=f"{name} news", url="https://n/x", snippet="n")] if floor == 2 else []
    row = EnrichedExhibitor(
        name=name,
        source_snippet=f"snippet for {name}",
        official_url="https://example.com/" + name.lower() if floor >= 1 else None,
        description=f"{name} does things",
        news_signals=news,
    )
    fit = FitResult(name=name, capability_fit=0.85, top_hits=[], capability_fit_breakdown={"Cap A": 3})
    return ScoredExhibitor(
        name=name, tier=tier, final_score=score, evidence_floor=floor,
        dimensions=DimensionScores(
            capability_fit=0.9, source_confidence=1.0, buying_signal=0.6,
            website_verification=1.0, category_fit=0.5,
            competitor_penalty=0.0, bad_fit_penalty=0.0,
        ),
        weights_used={}, tier_reasons=[], rationale="why", angle="angle", row=row, fit=fit,
    )


def _summary(*scored):
    counts = {"S": 0, "A": 0, "B": 0, "C": 0}
    for s in scored:
        counts[s.tier] += 1
    return ScoringSummary(rows=list(scored), tier_counts=counts, rationale_calls=0)


def _ctx():
    return ReportContext(
        workspace_id="acme", event_name="Expo", event_slug="expo", lang="en",
        generated_at=datetime(2026, 6, 8, tzinfo=UTC),
    )


_SCORE_KEYS = (
    "tier", "final_score", "evidence_floor", "capability_fit",
    "capability_fit_breakdown", "rationale", "angle", "evidence",
    "official_url", "news_count", "source_snippet",
)


# --------------------------------------------------------------------------- #
# the headline guarantee: provenance never changes a score
# --------------------------------------------------------------------------- #
def test_provenance_does_not_change_any_scoring_field():
    summary = _summary(_scored("Acme", "S", score=8.0, floor=2), _scored("Beta", "A", score=6.0, floor=1))
    prov = {"Acme": [{"source_path": "p/brief.pdf", "locator": "p/brief.pdf p2", "snippet": "grounding"}]}

    without = build_tier_list_payload(summary=summary, needs_review=[], context=_ctx())
    with_prov = build_tier_list_payload(
        summary=summary, needs_review=[], context=_ctx(), source_provenance=prov
    )

    for a, b in zip(without["exhibitors"], with_prov["exhibitors"], strict=True):
        for k in _SCORE_KEYS:
            assert a[k] == b[k], f"scoring field {k} changed when provenance attached"
    # only source_provenance differs
    assert with_prov["exhibitors"][0]["source_provenance"] == prov["Acme"]
    assert without["exhibitors"][0]["source_provenance"] == []


def test_schema_version_bumped_to_4_and_field_present():
    summary = _summary(_scored("Acme", "S", score=8.0, floor=2))
    payload = build_tier_list_payload(summary=summary, needs_review=[], context=_ctx())
    assert payload["schema_version"] == REPORT_SCHEMA_VERSION == 4
    assert payload["exhibitors"][0]["source_provenance"] == []


def test_md_renders_provenance_block_only_for_matched_rows():
    summary = _summary(_scored("Acme", "S", score=8.0, floor=2), _scored("Beta", "A", score=6.0, floor=1))
    prov = {"Acme": [{"source_path": "p/brief.pdf", "locator": "p/brief.pdf p2", "snippet": "the grounding text"}]}
    md = render_tier_list_md(summary=summary, needs_review=[], context=_ctx(), source_provenance=prov)
    assert "source grounding" in md
    assert "p/brief.pdf p2" in md
    assert "the grounding text" in md
    # Beta has no provenance → exactly one grounding block in the doc
    assert md.count("source grounding") == 1


# --------------------------------------------------------------------------- #
# gather_exhibitor_provenance
# --------------------------------------------------------------------------- #
class _Emb:
    def embed(self, texts):
        return [[float(i)] for i, _ in enumerate(texts)]


class _VS:
    def __init__(self, per_query):
        self.per_query = per_query

    def query(self, *, collection, query_embeddings, top_k, where=None):
        return [hits[:top_k] for hits in self.per_query]


def _h(cid, doc, path, **md):
    return {"id": cid, "document": doc, "metadata": {"source_path": path, **md}}


def test_gather_provenance_maps_names_to_top_chunks():
    vs = _VS([
        [_h("c1", "alpha body", "a.pdf", page=1), _h("c2", "alpha body 2", "a.pdf", page=2)],
        [_h("c3", "beta body", "b.md")],
    ])
    out = R.gather_exhibitor_provenance(
        items=[("Acme", "acme query"), ("Beta", "beta query")],
        workspace_id="default", embedding_provider=_Emb(), vectorstore_provider=vs, top_k=2,
    )
    assert out["Acme"][0]["locator"] == "a.pdf p1"
    assert out["Acme"][0]["snippet"] == "alpha body"
    assert len(out["Acme"]) == 2
    assert out["Beta"][0]["source_path"] == "b.md"


def test_gather_provenance_empty_items_returns_empty():
    assert R.gather_exhibitor_provenance(
        items=[], workspace_id="default", embedding_provider=_Emb(), vectorstore_provider=_VS([])
    ) == {}


def test_gather_provenance_snippet_truncation():
    vs = _VS([[_h("c1", "x" * 500, "a.md")]])
    out = R.gather_exhibitor_provenance(
        items=[("Acme", "q")], workspace_id="default",
        embedding_provider=_Emb(), vectorstore_provider=vs, snippet_chars=50,
    )
    assert len(out["Acme"][0]["snippet"]) == 50


# --------------------------------------------------------------------------- #
# run_summary field
# --------------------------------------------------------------------------- #
def test_run_summary_carries_source_index_fingerprint():
    from event_intel.events import run_summary as rs

    summary = rs.RunSummary(
        run_id="r", run_fingerprint="f", git_commit_sha="g", config_fp="c",
        cards_fingerprint=None, source_sha256=None, provider="anthropic", model_ids={},
        reference_timestamp="t", target_mode="customer", max_companies=None,
        max_chunks_per_event=None, refresh=False, cache_hits=0, cache_misses=0,
        skipped_from_resume=0, search_calls=0, extracted=0, enriched=0, scored=0,
        extraction_coverage=None, source_index_fingerprint="abc123",
    )
    assert summary.to_dict()["source_index_fingerprint"] == "abc123"


def test_run_summary_source_index_fingerprint_defaults_none():
    from event_intel.events import run_summary as rs

    summary = rs.RunSummary(
        run_id="r", run_fingerprint="f", git_commit_sha="g", config_fp="c",
        cards_fingerprint=None, source_sha256=None, provider="anthropic", model_ids={},
        reference_timestamp="t", target_mode="customer", max_companies=None,
        max_chunks_per_event=None, refresh=False, cache_hits=0, cache_misses=0,
        skipped_from_resume=0, search_calls=0, extracted=0, enriched=0, scored=0,
        extraction_coverage=None,
    )
    assert summary.to_dict()["source_index_fingerprint"] is None


# --------------------------------------------------------------------------- #
# ingest sync hook (opt-in)
# --------------------------------------------------------------------------- #
@pytest.fixture
def ingest_wired(monkeypatch, tmp_path, repo_root):
    """Validate against the real sample cards; bypass preflight; isolate paths."""
    src = repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml"
    cards_path = tmp_path / "cards.yaml"
    cards_path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr("event_intel.runtime.preflight.run_preflight", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr("event_intel.runtime.preflight.load_config", lambda *a, **kw: {})

    class _Emb2:
        model_id = "fake"

        def embed(self, texts):
            return [[0.1] * 3 for _ in texts]

    class _VS2:
        def __init__(self, **_):
            self.store = {}

        def upsert(self, *, collection, ids, embeddings, metadatas, documents):
            self.store.setdefault(collection, {}).update(dict.fromkeys(ids))

        def existing_ids(self, c):
            return set(self.store.get(c, {}))

        def delete_ids(self, c, ids):
            for i in ids:
                self.store.get(c, {}).pop(i, None)

        def set_collection_metadata(self, c, m):
            pass

        def get_collection_metadata(self, c):
            return {}

    monkeypatch.setattr("event_intel.providers.embedding.BgeM3Provider", _Emb2)
    monkeypatch.setattr("event_intel.providers.vectorstore.ChromaProvider", _VS2)
    monkeypatch.setenv("EVENT_INTEL_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("EVENT_INTEL_DATA_DIR", str(tmp_path / "data"))
    return tmp_path, str(cards_path)


def _ingest():
    return importlib.import_module("event_intel.tools.ingest_capability_cards").ingest_product_context


def test_ingest_default_does_not_sync_sources(ingest_wired):
    _, cards_path = ingest_wired
    res = _ingest()(workspace_id="default", cards_path=cards_path)
    assert res["ok"] is True
    assert "source_sync" not in res  # unchanged default behavior


def test_ingest_sync_sources_happy_path(ingest_wired):
    tmp, cards_path = ingest_wired
    prod = tmp / "ws" / "default" / "sources" / "product"
    prod.mkdir(parents=True)
    (prod / "brief.md").write_text("product source body", encoding="utf-8")

    res = _ingest()(workspace_id="default", cards_path=cards_path, sync_sources=True)
    assert res["ok"] is True
    assert res["card_ingested"] is True
    assert res["source_sync"]["total_files"] == 1
    assert res["collection"] == "product_default"


def test_ingest_sync_partial_aborts_card_ingest(ingest_wired, monkeypatch):
    tmp, cards_path = ingest_wired

    def _partial_sync(**kw):
        return {"ok": True, "partial": True, "failed_files": ["bad.pdf"], "warnings": ["x"]}

    monkeypatch.setattr("event_intel.sources.indexer.sync_sources", _partial_sync)

    # ingest_cards must NOT be called when the source sync is partial
    called = {"n": 0}
    import event_intel.cards.ingest as _ci

    orig = _ci.ingest_cards
    monkeypatch.setattr("event_intel.cards.ingest.ingest_cards", lambda **kw: called.__setitem__("n", called["n"] + 1) or orig(**kw))

    res = _ingest()(workspace_id="default", cards_path=cards_path, sync_sources=True)
    assert res["ok"] is True
    assert res["card_ingested"] is False
    assert "partial" in res["reason"]
    assert called["n"] == 0


def test_ingest_force_source_sync_passes_force(ingest_wired, monkeypatch):
    _, cards_path = ingest_wired
    seen = {}

    def _spy_sync(**kw):
        seen.update(kw)
        return {"ok": True, "partial": False, "total_files": 0, "warnings": []}

    monkeypatch.setattr("event_intel.sources.indexer.sync_sources", _spy_sync)
    res = _ingest()(workspace_id="default", cards_path=cards_path, force_source_sync=True, sync_sources=True)
    assert res["ok"] is True
    assert seen["force"] is True


# --------------------------------------------------------------------------- #
# indexer force=True
# --------------------------------------------------------------------------- #
def test_indexer_force_reindexes_unchanged(tmp_path):
    from event_intel.sources import indexer as I
    from tests.test_source_indexer import FakeEmbedding, FakeVectorStore

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.md").write_text("notes", encoding="utf-8")
    vs = FakeVectorStore()
    common = dict(
        sources_dir=src, workspace_id="default", vectorstore_provider=vs,
        manifest_path=tmp_path / "m.json", now_iso="2026-06-08T00:00:00+00:00",
        max_chars=50, overlap=10,
    )
    I.sync_sources(embedding_provider=FakeEmbedding(), **common)
    emb2 = FakeEmbedding()
    res = I.sync_sources(embedding_provider=emb2, force=True, **common)
    assert res["changed_files"] == 1  # forced re-index despite identical content
    assert emb2.calls == 1
