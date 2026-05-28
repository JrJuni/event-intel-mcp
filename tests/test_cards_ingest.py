"""S2 — cards ingest tests with fake embedding + vectorstore providers.

We test the orchestration (flatten → embed → upsert) without exercising real
bge-m3 / Chroma. The real providers have their own tests once integration is
wired in S6.
"""
from __future__ import annotations

from pathlib import Path

from event_intel.cards.ingest import (
    flatten_cards_to_chunks,
    ingest_cards,
    product_collection_name,
)
from event_intel.cards.validator import load_and_validate


class FakeEmbedding:
    def __init__(self):
        self.calls: list[list[str]] = []

    def embed(self, texts):
        self.calls.append(list(texts))
        # 4-dim fake vector per text — content-derived so identical texts get identical vecs
        return [[float(len(t)), 0.0, 1.0, 2.0] for t in texts]

    def is_ready(self):  # pragma: no cover
        return {"status": "ready", "path": "/fake"}


class FakeVectorStore:
    def __init__(self):
        self.collections: dict[str, dict] = {}

    def upsert(self, *, collection, ids, embeddings, metadatas, documents):
        col = self.collections.setdefault(
            collection,
            {"ids": {}, "embeddings": {}, "metadatas": {}, "documents": {}},
        )
        for i, _id in enumerate(ids):
            col["ids"][_id] = _id
            col["embeddings"][_id] = embeddings[i]
            col["metadatas"][_id] = metadatas[i]
            col["documents"][_id] = documents[i]

    def collection_info(self, collection):
        col = self.collections.get(collection)
        if col is None:
            return {"exists": False, "count": 0}
        return {"exists": True, "count": len(col["ids"])}

    # query / ensure_writable not exercised here
    def query(self, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def ensure_writable(self):  # pragma: no cover
        return {"status": "writable", "path": "/fake"}


def _load_fixture_cards(repo_root: Path):
    return load_and_validate(
        repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml"
    )


def test_flatten_emits_product_summary_and_one_chunk_per_capability(repo_root):
    cards = _load_fixture_cards(repo_root)
    chunks = flatten_cards_to_chunks(cards)
    kinds = [c.metadata["kind"] for c in chunks]
    assert "product_summary" in kinds
    capability_chunks = [c for c in chunks if c.metadata["kind"] == "capability"]
    assert len(capability_chunks) == len(cards.capabilities)
    # IDs are unique
    assert len({c.id for c in chunks}) == len(chunks)


def test_flatten_ids_are_stable_across_runs(repo_root):
    cards = _load_fixture_cards(repo_root)
    a = [c.id for c in flatten_cards_to_chunks(cards)]
    b = [c.id for c in flatten_cards_to_chunks(cards)]
    assert a == b


def test_ingest_writes_to_workspace_collection(repo_root):
    cards = _load_fixture_cards(repo_root)
    emb = FakeEmbedding()
    vs = FakeVectorStore()
    result = ingest_cards(
        cards=cards,
        workspace_id="acme",
        embedding_provider=emb,
        vectorstore_provider=vs,
    )
    assert result["ok"] is True
    assert result["collection"] == "product_acme"
    assert result["chunks"] > 0
    # One embed batch, with the right count
    assert len(emb.calls) == 1
    assert len(emb.calls[0]) == result["chunks"]
    info = vs.collection_info("product_acme")
    assert info["count"] == result["chunks"]


def test_reingest_is_idempotent_no_duplicates(repo_root):
    """Same cards re-ingested → upsert, not append."""
    cards = _load_fixture_cards(repo_root)
    emb = FakeEmbedding()
    vs = FakeVectorStore()
    r1 = ingest_cards(
        cards=cards,
        workspace_id="default",
        embedding_provider=emb,
        vectorstore_provider=vs,
    )
    r2 = ingest_cards(
        cards=cards,
        workspace_id="default",
        embedding_provider=emb,
        vectorstore_provider=vs,
    )
    assert r1["chunks"] == r2["chunks"]
    info = vs.collection_info("product_default")
    # Count is the per-id count, not 2x
    assert info["count"] == r1["chunks"]


def test_product_collection_name_matches_preflight_convention():
    """Must agree with runtime/preflight._product_collection_name."""
    from event_intel.runtime.preflight import _product_collection_name

    assert product_collection_name("default") == _product_collection_name("default")
    assert product_collection_name("acme") == _product_collection_name("acme")
