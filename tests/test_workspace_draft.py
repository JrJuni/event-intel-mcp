"""W3 — workspace drafting: sources.retrieval.gather_workspace_source_text +
draft_capability_cards(source_kind="workspace") wiring.

Live imports + string-target patches (cold-start purge safe, playbook #2).
"""
from __future__ import annotations

import importlib

import pytest

from event_intel.errors import ErrorCode
from event_intel.sources import retrieval as R


def _hit(cid, doc, dist, path, **md):
    return {"id": cid, "document": doc, "distance": dist, "metadata": {"source_path": path, **md}}


class _Emb:
    def embed(self, texts):
        return [[float(i)] for i, _ in enumerate(texts)]


class _VS:
    """Returns the supplied per-query hit lists (one per query embedding)."""

    def __init__(self, per_query_hits):
        self.per_query_hits = per_query_hits

    def query(self, *, collection, query_embeddings, top_k, where=None):
        return [hits[:top_k] for hits in self.per_query_hits]


# --------------------------------------------------------------------------- #
# retrieval
# --------------------------------------------------------------------------- #
def test_dedup_by_id_keeps_best_distance():
    vs = _VS([
        [_hit("c1", "TEXT1", 0.5, "d1.md")],
        [_hit("c1", "TEXT1", 0.2, "d1.md")],
    ])
    blob, meta = R.gather_workspace_source_text(
        workspace_id="default",
        embedding_provider=_Emb(),
        vectorstore_provider=vs,
        queries=["a", "b"],
    )
    assert blob.count("TEXT1") == 1
    assert meta["chunks_used"] == 1
    assert meta["files"] == 1
    assert meta["collection"] == "product_sources_default"


def test_per_document_round_robin_order():
    vs = _VS([
        [
            _hit("a", "A", 0.2, "d1.md"),
            _hit("c", "C", 0.3, "d2.md"),
            _hit("b", "B", 0.4, "d1.md"),
        ]
    ])
    blob, meta = R.gather_workspace_source_text(
        workspace_id="default",
        embedding_provider=_Emb(),
        vectorstore_provider=vs,
        queries=["only"],
    )
    # doc_order = d1(0.2), d2(0.3); round-robin rank0: A, C ; rank1: B
    assert blob.index("A") < blob.index("C") < blob.index("B")
    assert meta["files"] == 2
    assert meta["chunks_used"] == 3


def test_max_chars_cap_truncates():
    vs = _VS([
        [
            _hit("a", "X" * 40, 0.1, "d1.md"),
            _hit("b", "Y" * 40, 0.2, "d2.md"),
        ]
    ])
    blob, meta = R.gather_workspace_source_text(
        workspace_id="default",
        embedding_provider=_Emb(),
        vectorstore_provider=vs,
        queries=["q"],
        max_chars=60,
    )
    assert meta["truncated"] is True
    assert meta["chunks_used"] == 1


def test_empty_collection_raises_invalid_input():
    vs = _VS([[]])
    with pytest.raises(Exception) as ei:
        R.gather_workspace_source_text(
            workspace_id="default",
            embedding_provider=_Emb(),
            vectorstore_provider=vs,
            queries=["q"],
        )
    assert ei.value.error_code == ErrorCode.INVALID_INPUT


def test_provenance_labels():
    assert R._provenance_label({"source_path": "d.pdf", "page": 3}) == "d.pdf p3"
    assert (
        R._provenance_label({"source_path": "t.csv", "row_start": 1, "row_end": 2})
        == "t.csv rows 1-2"
    )
    assert R._provenance_label({"source_path": "n.md"}) == "n.md"


def test_blob_carries_provenance_header():
    vs = _VS([[_hit("a", "the body", 0.1, "product/brief.pdf", page=2)]])
    blob, _ = R.gather_workspace_source_text(
        workspace_id="default",
        embedding_provider=_Emb(),
        vectorstore_provider=vs,
        queries=["q"],
    )
    assert "[source: product/brief.pdf p2]" in blob
    assert "the body" in blob


# --------------------------------------------------------------------------- #
# draft_capability_cards(source_kind="workspace") wiring
# --------------------------------------------------------------------------- #
@pytest.fixture
def wired_draft(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "event_intel.runtime.preflight.load_config",
        lambda *a, **kw: {"llm": {"draft_cards_model": "m", "draft_cards_max_tokens": 100}},
    )
    monkeypatch.setattr("event_intel.runtime.preflight.run_preflight", lambda *a, **kw: {"ok": True})

    class _LLM:
        def ping(self):
            return {"status": "ok"}

    monkeypatch.setattr("event_intel.providers.llm.make_llm_provider", lambda *a, **kw: _LLM())
    monkeypatch.setattr("event_intel.providers.embedding.BgeM3Provider", lambda *a, **kw: _Emb())
    monkeypatch.setenv("EVENT_INTEL_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setenv("EVENT_INTEL_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_workspace_draft_feeds_retrieved_text_to_drafter(wired_draft, monkeypatch):
    captured = {}

    vs = _VS([[_hit("a", "PRODUCT SOURCE BODY", 0.1, "product/brief.md")]])
    monkeypatch.setattr("event_intel.providers.vectorstore.ChromaProvider", lambda *a, **kw: vs)

    drafter_mod = importlib.import_module("event_intel.cards.drafter")

    def _fake_draft_cards(*, source_kind, source_content, source_paths, lang, llm_provider, max_tokens):
        captured["source_kind"] = source_kind
        captured["source_content"] = source_content
        captured["source_paths"] = source_paths
        return drafter_mod.DraftResult(
            yaml_text="product_name: X\none_liner: y\n", warnings=[], model="m", usage={}
        )

    monkeypatch.setattr("event_intel.cards.drafter.draft_cards", _fake_draft_cards)

    draft = importlib.import_module("event_intel.tools.draft_capability_cards")
    res = draft.draft_capability_cards(workspace_id="default", source_kind="workspace", lang="en")

    assert res["ok"] is True, res
    # drafter received the retrieved blob as plain text
    assert captured["source_kind"] == "text"
    assert captured["source_paths"] is None
    assert "PRODUCT SOURCE BODY" in captured["source_content"]
    assert "[source: product/brief.md]" in captured["source_content"]
    # response surfaces retrieval meta + the draft file was written
    assert res["source_retrieval"]["chunks_used"] == 1
    from pathlib import Path

    assert Path(res["draft_path"]).is_file()


def test_workspace_draft_empty_library_returns_envelope(wired_draft, monkeypatch):
    vs = _VS([[]])  # nothing synced
    monkeypatch.setattr("event_intel.providers.vectorstore.ChromaProvider", lambda *a, **kw: vs)

    draft = importlib.import_module("event_intel.tools.draft_capability_cards")
    res = draft.draft_capability_cards(workspace_id="default", source_kind="workspace")
    assert res["ok"] is False
    assert res["error_code"] == ErrorCode.INVALID_INPUT


def test_text_source_kind_unaffected(wired_draft, monkeypatch):
    """The existing inline-text path must not touch retrieval/providers."""
    captured = {}
    drafter_mod = importlib.import_module("event_intel.cards.drafter")

    def _fake_draft_cards(*, source_kind, source_content, **kw):
        captured["source_kind"] = source_kind
        captured["source_content"] = source_content
        return drafter_mod.DraftResult(yaml_text="product_name: X\n", warnings=[], model="m", usage={})

    monkeypatch.setattr("event_intel.cards.drafter.draft_cards", _fake_draft_cards)

    draft = importlib.import_module("event_intel.tools.draft_capability_cards")
    res = draft.draft_capability_cards(
        workspace_id="default", source_kind="text", source_content="hello inline"
    )
    assert res["ok"] is True, res
    assert captured["source_kind"] == "text"
    assert captured["source_content"] == "hello inline"
    assert "source_retrieval" not in res


# --------------------------------------------------------------------------- #
# CLI flag plumbing
# --------------------------------------------------------------------------- #
def test_cli_draft_help_lists_from_workspace():
    from typer.testing import CliRunner

    app = importlib.import_module("event_intel.cli").app
    res = CliRunner().invoke(app, ["draft-cards", "--help"])
    assert res.exit_code == 0
    assert "--from-workspace" in res.output


def test_cli_draft_from_workspace_conflicts_with_other_modes():
    from typer.testing import CliRunner

    app = importlib.import_module("event_intel.cli").app
    res = CliRunner().invoke(app, ["draft-cards", "--from-workspace", "--text", "x"])
    assert res.exit_code == 2
