"""W2 — sync_product_sources tool handler: path resolution, kind/slug validation,
preflight + provider wiring, envelope on failure. Fake providers (no bge-m3 /
chromadb); EVENT_INTEL_DATA_DIR + workspace env isolate all writes to tmp.

Module-identity discipline (playbook #2): test_mcp_cold_start purges
``event_intel.*`` between tests, so we never bind tool/provider modules at import
time. The ``wired`` fixture patches via STRING targets (resolved against the live
sys.modules object) and tests import the handler fresh inside the body — so the
handler and the patches always share the same live module objects, even when the
CLI re-imports the tool lazily.
"""
from __future__ import annotations

import importlib

import pytest


class _FakeEmb:
    model_id = "fake-embed"

    def __init__(self, **_):
        pass

    def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


class _FakeVS:
    def __init__(self, **_):
        self.store: dict = {}
        self.meta: dict = {}

    def upsert(self, *, collection, ids, embeddings, metadatas, documents):
        col = self.store.setdefault(collection, {})
        for i, _id in enumerate(ids):
            col[_id] = documents[i]

    def existing_ids(self, collection):
        return set(self.store.get(collection, {}))

    def delete_ids(self, collection, ids):
        for _id in ids:
            self.store.get(collection, {}).pop(_id, None)

    def set_collection_metadata(self, collection, metadata):
        self.meta.setdefault(collection, {}).update(metadata)

    def get_collection_metadata(self, collection):
        return dict(self.meta.get(collection, {}))


def _handler():
    return importlib.import_module("event_intel.tools.sync_product_sources").sync_product_sources


def _errors():
    return importlib.import_module("event_intel.errors")


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Bypass preflight, inject fakes, isolate the data root (manifest) to tmp.

    String-target setattr resolves the live module attribute at patch time, so a
    prior cold-start purge can't leave us patching a stale module object.
    """
    monkeypatch.setattr("event_intel.runtime.preflight.run_preflight", lambda *a, **kw: {"ok": True})
    monkeypatch.setattr("event_intel.runtime.preflight.load_config", lambda *a, **kw: {})
    monkeypatch.setattr("event_intel.providers.embedding.BgeM3Provider", _FakeEmb)
    monkeypatch.setattr("event_intel.providers.vectorstore.ChromaProvider", _FakeVS)
    monkeypatch.setenv("EVENT_INTEL_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_sync_with_source_dir_override(wired):
    src = wired / "lib"
    src.mkdir()
    (src / "a.md").write_text("product overview notes", encoding="utf-8")
    (src / "b.csv").write_text("k,v\nfoo,bar\n", encoding="utf-8")

    res = _handler()(workspace_id="default", source_dir=str(src))
    assert res["ok"] is True
    assert res["collection"] == "product_sources_default"
    assert res["kind"] == "all"
    assert res["sources_dir"] == str(src)
    assert res["total_files"] == 2
    assert res["chunk_count"] >= 2
    # manifest landed under the isolated data root, not real ~/.event-intel
    assert (wired / "data" / "source-index" / "default" / "manifest.json").is_file()


def test_sync_kind_all_resolves_workspace_sources(wired, monkeypatch):
    ws_root = wired / "ws"
    monkeypatch.setenv("EVENT_INTEL_WORKSPACE_DIR", str(ws_root))
    prod = ws_root / "default" / "sources" / "product"
    prod.mkdir(parents=True)
    (prod / "brief.md").write_text("capabilities and customers", encoding="utf-8")

    res = _handler()(workspace_id="default")  # kind defaults to "all"
    assert res["ok"] is True
    assert res["sources_dir"].endswith("sources")
    assert res["total_files"] == 1
    assert res["chunk_count"] >= 1


def test_sync_kind_product_resolves_product_subdir(wired, monkeypatch):
    ws_root = wired / "ws"
    monkeypatch.setenv("EVENT_INTEL_WORKSPACE_DIR", str(ws_root))
    prod = ws_root / "default" / "sources" / "product"
    prod.mkdir(parents=True)
    (prod / "x.txt").write_text("hello", encoding="utf-8")

    res = _handler()(workspace_id="default", kind="product")
    assert res["ok"] is True
    assert res["sources_dir"].replace("\\", "/").endswith("sources/product")
    assert res["total_files"] == 1


def test_sync_empty_dir_is_ok_with_warning(wired):
    empty = wired / "empty"
    empty.mkdir()
    res = _handler()(workspace_id="default", source_dir=str(empty))
    assert res["ok"] is True
    assert res["total_files"] == 0
    assert any("no indexable" in w for w in res["warnings"])


def test_sync_invalid_kind_returns_invalid_input(wired):
    err = _errors()
    res = _handler()(workspace_id="default", kind="bogus")
    assert res["ok"] is False
    assert res["error_code"] == err.ErrorCode.INVALID_INPUT
    assert res["stage"] == err.Stage.INGEST
    assert res["hint"]["allowed"] == ["all", "product", "company"]


def test_sync_invalid_workspace_slug_returns_invalid_input():
    # sanitize runs before preflight/providers, so no wiring needed.
    err = _errors()
    res = _handler()(workspace_id="bad slug!", source_dir="/whatever")
    assert res["ok"] is False
    assert res["error_code"] == err.ErrorCode.INVALID_INPUT


def test_sync_preflight_failure_propagates_envelope(monkeypatch, tmp_path):
    err = _errors()
    monkeypatch.setattr("event_intel.runtime.preflight.load_config", lambda *a, **kw: {})

    def _boom(*a, **kw):
        raise err.MCPError(
            error_code=err.ErrorCode.MODEL_NOT_READY,
            stage=err.Stage.PREFLIGHT,
            message="bge-m3 not cached",
        )

    monkeypatch.setattr("event_intel.runtime.preflight.run_preflight", _boom)
    res = _handler()(workspace_id="default", source_dir=str(tmp_path))
    assert res["ok"] is False
    assert res["error_code"] == err.ErrorCode.MODEL_NOT_READY
    assert res["stage"] == err.Stage.PREFLIGHT


def test_mcp_server_registers_tenth_tool():
    server = importlib.import_module("event_intel.mcp_server")
    err = _errors()
    # invalid slug short-circuits before preflight/providers → safe smoke
    res = server.sync_product_sources(workspace_id="bad slug!")
    assert isinstance(res, dict)
    assert res["ok"] is False
    assert res["error_code"] == err.ErrorCode.INVALID_INPUT


# --------------------------------------------------------------------------- #
# CLI smoke
# --------------------------------------------------------------------------- #
def test_cli_sources_help_lists_sync():
    from typer.testing import CliRunner

    app = importlib.import_module("event_intel.cli").app
    res = CliRunner().invoke(app, ["sources", "--help"])
    assert res.exit_code == 0
    assert "sync" in res.output


def test_cli_sources_sync_runs_through(wired):
    import json

    from typer.testing import CliRunner

    app = importlib.import_module("event_intel.cli").app
    src = wired / "lib"
    src.mkdir()
    (src / "a.md").write_text("notes", encoding="utf-8")

    res = CliRunner().invoke(
        app, ["sources", "sync", "--workspace", "default", "--source-dir", str(src)]
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["ok"] is True
    assert payload["collection"] == "product_sources_default"
    assert payload["total_files"] == 1
