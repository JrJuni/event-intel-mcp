"""Y2.1e — path-free e2e (over stdio, no server-local paths).

Proves the Y2.1 contract end to end: upload content as an artifact, run the
Product Context lifecycle by artifact_id, and retrieve a background job's result
by artifact_id — no filesystem path crosses the tool boundary. Providers/preflight
are faked (no bge-m3 / Chroma); the build path-free case is covered in
test_mcp_tools (Y2.1b-2).
"""
from __future__ import annotations

import importlib
import time

import pytest

from event_intel.runtime import job_store as J
from event_intel.storage import artifact_registry as R


@pytest.fixture(autouse=True)
def wired(monkeypatch, tmp_path):
    monkeypatch.setenv("EVENT_INTEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EVENT_INTEL_WORKSPACE_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr("event_intel.runtime.preflight.run_preflight", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("event_intel.runtime.preflight.load_config", lambda *a, **k: {"paths": {}})

    class _Emb:
        model_id = "fake"

        def embed(self, texts):
            return [[0.1] * 3 for _ in texts]

    class _VS:
        def __init__(self, **_):
            self.s: dict = {}

        def upsert(self, *, collection, ids, embeddings, metadatas, documents):
            self.s.setdefault(collection, {}).update(dict.fromkeys(ids))

        def existing_ids(self, c):
            return set(self.s.get(c, {}))

        def delete_ids(self, c, ids):
            pass

        def set_collection_metadata(self, c, m):
            pass

        def get_collection_metadata(self, c):
            return {}

    monkeypatch.setattr("event_intel.providers.embedding.BgeM3Provider", _Emb)
    monkeypatch.setattr("event_intel.providers.vectorstore.ChromaProvider", _VS)
    return tmp_path


def test_path_free_cards_lifecycle_and_job(repo_root):
    sample = (repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml").read_text(
        encoding="utf-8"
    )
    # 1. upload cards as an artifact — no path involved
    aid = R.put_artifact(workspace_id="default", content=sample)["artifact_id"]

    # 2. validate BY artifact_id (path-free)
    validate = importlib.import_module(
        "event_intel.tools.validate_capability_cards"
    ).validate_capability_cards
    v = validate(cards_artifact_id=aid, workspace_id="default")
    assert v["ok"] is True and v["source"] == f"artifact:{aid}"

    # 3. ingest BY artifact_id (path-free)
    ingest = importlib.import_module(
        "event_intel.tools.ingest_capability_cards"
    ).ingest_product_context
    ing = ingest(workspace_id="default", cards_artifact_id=aid)
    assert ing["ok"] is True and ing["collection"] == "product_default"
    assert ing["source"] == f"artifact:{aid}"

    # 4. a background job returns its result BY artifact_id (path-free output)
    out_art = R.put_artifact(workspace_id="default", content="job result blob")
    started = J.run_as_job(
        workspace_id="default", tool="demo", fn=lambda: [out_art["artifact_id"]]
    )
    deadline = time.monotonic() + 3.0
    m = None
    while time.monotonic() < deadline:
        m = J.get_job(workspace_id="default", job_id=started["job_id"])
        if m and m["status"] == J.DONE:
            break
        time.sleep(0.01)
    assert m and m["status"] == J.DONE
    result_id = m["result_artifact_ids"][0]
    assert R.get_artifact(workspace_id="default", artifact_id=result_id) == b"job result blob"
