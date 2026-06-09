"""Y2.1b — input contract (path | content | artifact_id, exactly one).

Covers runtime.io_contract.materialize_input + validate/ingest 3-way wiring,
including local-path no-regression. Live import + string-target patches.
"""
from __future__ import annotations

import importlib

import pytest

from event_intel.errors import ErrorCode
from event_intel.runtime import io_contract as IO
from event_intel.storage import artifact_registry as R


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EVENT_INTEL_WORKSPACE_DIR", str(tmp_path / "ws"))
    return tmp_path


# --------------------------------------------------------------------------- #
# materialize_input
# --------------------------------------------------------------------------- #
def test_path_mode_yields_path_no_temp(tmp_path):
    f = tmp_path / "x.yaml"
    f.write_text("hi", encoding="utf-8")
    with IO.materialize_input(workspace_id="default", field="cards", path=str(f)) as p:
        assert p == f
        assert p.read_text(encoding="utf-8") == "hi"
    assert f.exists()  # path input is never cleaned up


def test_content_mode_materializes_temp_then_cleans():
    seen = {}
    with IO.materialize_input(workspace_id="default", field="cards", content="body!") as p:
        seen["path"] = p
        assert p.read_bytes() == b"body!"
    assert not seen["path"].exists()  # temp removed on exit


def test_artifact_mode_round_trip():
    put = R.put_artifact(workspace_id="default", content="from-artifact")
    with IO.materialize_input(
        workspace_id="default", field="cards", artifact_id=put["artifact_id"]
    ) as p:
        assert p.read_bytes() == b"from-artifact"


def test_zero_inputs_invalid():
    with pytest.raises(Exception) as ei:
        with IO.materialize_input(workspace_id="default", field="cards"):
            pass
    assert ei.value.error_code == ErrorCode.INVALID_INPUT


def test_two_inputs_invalid_no_priority():
    with pytest.raises(Exception) as ei:
        with IO.materialize_input(
            workspace_id="default", field="cards", content="a", artifact_id="b"
        ):
            pass
    assert ei.value.error_code == ErrorCode.INVALID_INPUT
    assert "exactly one" in ei.value.message


def test_inline_over_cap_invalid():
    big = "x" * (IO.INLINE_CONTENT_MAX_BYTES + 1)
    with pytest.raises(Exception) as ei:
        with IO.materialize_input(workspace_id="default", field="cards", content=big):
            pass
    assert ei.value.error_code == ErrorCode.INVALID_INPUT
    assert "put_artifact" in ei.value.message


def test_artifact_not_found_invalid():
    with pytest.raises(Exception) as ei:
        with IO.materialize_input(
            workspace_id="default", field="cards", artifact_id="Zm9vMTIzNDU2Nzg5MDEy"
        ):
            pass
    assert ei.value.error_code == ErrorCode.INVALID_INPUT


# --------------------------------------------------------------------------- #
# validate_capability_cards 3-way
# --------------------------------------------------------------------------- #
def _sample_cards(repo_root):
    return (repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml").read_text(
        encoding="utf-8"
    )


def _validate():
    return importlib.import_module(
        "event_intel.tools.validate_capability_cards"
    ).validate_capability_cards


def test_validate_via_content(repo_root):
    res = _validate()(cards_content=_sample_cards(repo_root))
    assert res["ok"] is True
    assert res["source"] == "content"
    assert res["capability_count"] >= 1


def test_validate_via_artifact(repo_root):
    put = R.put_artifact(workspace_id="default", content=_sample_cards(repo_root))
    res = _validate()(cards_artifact_id=put["artifact_id"], workspace_id="default")
    assert res["ok"] is True
    assert res["source"] == f"artifact:{put['artifact_id']}"


def test_validate_via_path_no_regression(repo_root, tmp_path):
    f = tmp_path / "cards.yaml"
    f.write_text(_sample_cards(repo_root), encoding="utf-8")
    res = _validate()(cards_path=str(f))
    assert res["ok"] is True
    assert res["source"] == "path"
    assert res["cards_path"] == str(f)


def test_validate_two_inputs_invalid(repo_root):
    res = _validate()(cards_path="x.yaml", cards_content=_sample_cards(repo_root))
    assert res["ok"] is False
    assert res["error_code"] == ErrorCode.INVALID_INPUT


# --------------------------------------------------------------------------- #
# ingest_product_context via content (providers mocked)
# --------------------------------------------------------------------------- #
def test_ingest_via_content(repo_root, monkeypatch, tmp_path):
    monkeypatch.setattr("event_intel.runtime.preflight.run_preflight", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(
        "event_intel.runtime.preflight.load_config", lambda *a, **k: {"paths": {}}
    )

    class _Emb:
        model_id = "fake"

        def embed(self, texts):
            return [[0.1] * 3 for _ in texts]

    class _VS:
        def __init__(self, **_):
            self.store = {}

        def upsert(self, *, collection, ids, embeddings, metadatas, documents):
            self.store.setdefault(collection, {}).update(dict.fromkeys(ids))

        def existing_ids(self, c):
            return set(self.store.get(c, {}))

        def delete_ids(self, c, ids):
            pass

        def set_collection_metadata(self, c, m):
            pass

        def get_collection_metadata(self, c):
            return {}

    monkeypatch.setattr("event_intel.providers.embedding.BgeM3Provider", _Emb)
    monkeypatch.setattr("event_intel.providers.vectorstore.ChromaProvider", _VS)

    ingest = importlib.import_module(
        "event_intel.tools.ingest_capability_cards"
    ).ingest_product_context
    res = ingest(workspace_id="default", cards_content=_sample_cards(repo_root))
    assert res["ok"] is True, res
    assert res["collection"] == "product_default"
    assert res["source"] == "content"
    # receipt lands in the workspace dir (no source path), where build looks
    from pathlib import Path

    assert res["receipt_path"] and Path(res["receipt_path"]).name == "ingest_receipt.json"
    assert (tmp_path / "ws" / "default") == Path(res["receipt_path"]).parent
