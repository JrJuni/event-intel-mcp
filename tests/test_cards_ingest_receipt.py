"""Y1 CS7 — ingest content_fingerprint (deterministic, ts-free) vs receipt
instance (ts), Chroma collection-metadata storage, and live drift detection."""
from __future__ import annotations

import json

from event_intel.cards import ingest as I
from event_intel.cards.validator import load_and_validate
from tests.test_cards_ingest import FakeEmbedding, FakeVectorStore


def _cards(repo_root):
    return load_and_validate(repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml")


# ---------- content fingerprint: deterministic, ts-free, input-sensitive ----------

def test_content_fingerprint_is_deterministic(repo_root):
    chunks = I.flatten_cards_to_chunks(_cards(repo_root))
    a = I.compute_content_fingerprint(chunks, embedding_model_id="bge-m3", collection="product_x")
    b = I.compute_content_fingerprint(chunks, embedding_model_id="bge-m3", collection="product_x")
    assert a == b


def test_content_fingerprint_changes_with_text_model_collection(repo_root):
    chunks = I.flatten_cards_to_chunks(_cards(repo_root))
    base = I.compute_content_fingerprint(chunks, embedding_model_id="bge-m3", collection="c")
    # different embedding model
    assert I.compute_content_fingerprint(chunks, embedding_model_id="other", collection="c") != base
    # different collection
    assert I.compute_content_fingerprint(chunks, embedding_model_id="bge-m3", collection="c2") != base
    # different content
    cards2 = _cards(repo_root)
    cards2.capabilities[0].name = "Totally Different Capability Name"
    chunks2 = I.flatten_cards_to_chunks(cards2)
    assert I.compute_content_fingerprint(chunks2, embedding_model_id="bge-m3", collection="c") != base


# ---------- receipt instance: ts excluded from fingerprint (R3-6) ----------

def test_receipt_ts_does_not_affect_fingerprint():
    fp = "deadbeef"
    r1 = I.build_ingest_receipt(
        content_fingerprint=fp, cards_sha256="s", collection="c",
        chunk_count=5, embedding_model_id="bge-m3", now_iso="2026-06-08T00:00:00+00:00",
    )
    r2 = I.build_ingest_receipt(
        content_fingerprint=fp, cards_sha256="s", collection="c",
        chunk_count=5, embedding_model_id="bge-m3", now_iso="2026-06-09T12:00:00+00:00",
    )
    assert r1["content_fingerprint"] == r2["content_fingerprint"]  # ts-free
    assert r1["ingested_at"] != r2["ingested_at"]                  # instance differs


def test_receipt_write_read_round_trip(tmp_path):
    receipt = I.build_ingest_receipt(
        content_fingerprint="fp", cards_sha256="abc", collection="product_x",
        chunk_count=7, embedding_model_id="bge-m3", now_iso="2026-06-08T00:00:00+00:00",
    )
    path = tmp_path / I.RECEIPT_FILENAME
    I.write_ingest_receipt(receipt, path)
    assert json.loads(path.read_text(encoding="utf-8"))["content_fingerprint"] == "fp"
    assert I.read_ingest_receipt(path) == receipt
    assert I.read_ingest_receipt(tmp_path / "missing.json") is None


# ---------- ingest_cards persists fingerprint to collection metadata ----------

class _MetaVectorStore(FakeVectorStore):
    def __init__(self):
        super().__init__()
        self.meta: dict[str, dict] = {}

    def set_collection_metadata(self, collection, metadata):
        self.meta.setdefault(collection, {}).update(metadata)

    def get_collection_metadata(self, collection):
        return dict(self.meta.get(collection, {}))


def test_ingest_returns_and_persists_fingerprint(repo_root):
    vs = _MetaVectorStore()
    result = I.ingest_cards(
        cards=_cards(repo_root), workspace_id="acme",
        embedding_provider=FakeEmbedding(), vectorstore_provider=vs,
    )
    fp = result["content_fingerprint"]
    assert fp and result["fingerprint_persisted"] is True
    assert vs.get_collection_metadata("product_acme")["content_fingerprint"] == fp


def test_reingest_identical_cards_same_fingerprint(repo_root):
    vs = _MetaVectorStore()
    r1 = I.ingest_cards(cards=_cards(repo_root), workspace_id="d",
                        embedding_provider=FakeEmbedding(), vectorstore_provider=vs)
    r2 = I.ingest_cards(cards=_cards(repo_root), workspace_id="d",
                        embedding_provider=FakeEmbedding(), vectorstore_provider=vs)
    assert r1["content_fingerprint"] == r2["content_fingerprint"]


def test_ingest_without_metadata_support_does_not_fail(repo_root):
    """A provider without set_collection_metadata (ABC default) → no crash, just
    fingerprint_persisted=False."""
    result = I.ingest_cards(
        cards=_cards(repo_root), workspace_id="x",
        embedding_provider=FakeEmbedding(), vectorstore_provider=FakeVectorStore(),
    )
    assert result["content_fingerprint"]
    assert result["fingerprint_persisted"] is False


# ---------- drift detection (measure-time) ----------

def test_verify_collection_fingerprint_match_mismatch_absent():
    vs = _MetaVectorStore()
    vs.set_collection_metadata("c", {"content_fingerprint": "fp1"})
    assert I.verify_collection_fingerprint(vs, "c", "fp1")["status"] == "match"
    assert I.verify_collection_fingerprint(vs, "c", "fp2")["status"] == "mismatch"
    assert I.verify_collection_fingerprint(vs, "absent", "fp1")["status"] == "absent"


def test_verify_handles_provider_without_metadata():
    """ABC-default provider (no stored fingerprint) → absent, never raises."""
    out = I.verify_collection_fingerprint(FakeVectorStore(), "c", "fp")
    assert out["status"] == "absent"
