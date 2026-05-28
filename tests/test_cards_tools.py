"""S2 — MCP tool boundary tests (envelope rendering + monkeypatch through ref imports).

These tests verify that the MCP wrappers in `event_intel.tools.*_capability_cards`
catch exceptions from the underlying modules and render them as MCPError
envelopes, AND that the module-reference import pattern actually lets us swap
behavior at test time without re-hosting the cold-start regression.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from event_intel.cards import drafter as _drafter
from event_intel.cards import ingest as _ingest
from event_intel.cards import validator as _validator
from event_intel.errors import ErrorCode, MCPError, Stage
from event_intel.providers import embedding as _embedding
from event_intel.providers import llm as _llm
from event_intel.providers import vectorstore as _vectorstore
from event_intel.runtime import preflight as _preflight
from event_intel.tools.draft_capability_cards import draft_capability_cards
from event_intel.tools.ingest_capability_cards import ingest_product_context
from event_intel.tools.validate_capability_cards import validate_capability_cards


# ---------- validate boundary ----------


def test_validate_tool_returns_envelope_on_schema_error(tmp_path, monkeypatch):
    """Validator raises MCPError(SCHEMA_ERROR); tool must render to envelope."""

    def fake_load_and_validate(path):
        raise MCPError(
            error_code=ErrorCode.SCHEMA_ERROR,
            stage=Stage.INGEST,
            message="capability_cards failed validation at capabilities[0].keywords: too few",
            hint={"errors": [{"path": "capabilities[0].keywords", "type": "too_short", "msg": "min 3"}]},
            retryable=False,
        )

    monkeypatch.setattr(_validator, "load_and_validate", fake_load_and_validate)
    out = validate_capability_cards(cards_path=str(tmp_path / "x.yaml"))
    assert out["ok"] is False
    assert out["error_code"] == "SCHEMA_ERROR"
    assert out["stage"] == "ingest"
    assert "capabilities[0].keywords" in out["hint"]["errors"][0]["path"]


def test_validate_tool_success_returns_summary(repo_root: Path):
    """Happy path against the committed fixture."""
    cards_path = repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml"
    out = validate_capability_cards(cards_path=str(cards_path))
    assert out["ok"] is True
    assert out["product_name"] == "Mobius"
    assert out["capability_count"] >= 1
    assert out["competitor_count"] >= 1


# ---------- draft boundary ----------


def test_draft_tool_wraps_provider_errors_in_envelope(monkeypatch, tmp_path):
    """When the LLM ping reports missing_key, tool returns CONFIG_ERROR envelope."""

    class _DeadLLM:
        def __init__(self, **kwargs):
            self.model = kwargs.get("model", "?")

        def ping(self):
            return {"status": "missing_key"}

        def chat_once(self, **kwargs):  # pragma: no cover
            raise NotImplementedError

        def chat_cached(self, **kwargs):  # pragma: no cover
            raise NotImplementedError

    monkeypatch.setattr(_llm, "AnthropicProvider", _DeadLLM)
    monkeypatch.setattr(_preflight, "load_config", lambda *a, **kw: {
        "llm": {"draft_cards_model": "fake", "draft_cards_max_tokens": 256}
    })
    out = draft_capability_cards(
        workspace_id="default",
        source_kind="text",
        source_content="x",
    )
    assert out["ok"] is False
    assert out["error_code"] == "CONFIG_ERROR"
    assert out["stage"] == "ingest"


def test_draft_tool_writes_yaml_to_out_path(monkeypatch, tmp_path):
    """Happy path: fake LLM emits good YAML, tool writes to out_path and returns ok."""
    yaml_text = (
        "schema_version: 1\n"
        "product_name: X\n"
        "one_liner: Y\n"
        "capabilities:\n"
        "  - name: Z\n"
        "    keywords: [a, b, c]\n"
        "    buyer_pains: [p]\n"
        "    evidence_queries: [q]\n"
        "ideal_customer:\n"
        "  industries: [auto]\n"
        "  company_signals: [hiring]\n"
    )

    class _OKLLM:
        def __init__(self, **kwargs):
            self.model = kwargs.get("model", "fake")

        def ping(self):
            return {"status": "ok", "model": self.model}

        def chat_once(self, **kwargs):
            return _llm.LLMResponse(
                text=yaml_text, usage={"input_tokens": 1, "output_tokens": 1}, model=self.model
            )

        def chat_cached(self, **kwargs):  # pragma: no cover
            raise NotImplementedError

    monkeypatch.setattr(_llm, "AnthropicProvider", _OKLLM)
    monkeypatch.setattr(_preflight, "load_config", lambda *a, **kw: {
        "llm": {"draft_cards_model": "fake", "draft_cards_max_tokens": 256}
    })

    out_path = tmp_path / "draft.yaml"
    out = draft_capability_cards(
        workspace_id="default",
        source_kind="text",
        source_content="some product description",
        out_path=str(out_path),
    )
    assert out["ok"] is True, out
    assert out["draft_path"] == str(out_path)
    written = out_path.read_text(encoding="utf-8")
    assert "product_name: X" in written


# ---------- ingest boundary ----------


def test_ingest_tool_requires_cards_path():
    out = ingest_product_context(workspace_id="default", cards_path="")
    assert out["ok"] is False
    # cards_path missing → ValueError → INTERNAL envelope
    assert out["error_code"] in {"INTERNAL", "INVALID_INPUT"}


def test_ingest_tool_validates_workspace_id():
    out = ingest_product_context(workspace_id="bad slug!", cards_path="anything.yaml")
    assert out["ok"] is False
    assert out["error_code"] == "INVALID_INPUT"


def test_ingest_tool_runs_through_when_providers_mocked(repo_root, monkeypatch):
    """End-to-end with the cards fixture + fake providers + preflight bypass."""
    cards_path = repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml"

    # Preflight will fail without bge-m3 cached + keys set — bypass it.
    monkeypatch.setattr(_preflight, "run_preflight", lambda *a, **kw: {"ok": True, "checks": {}})

    embed_calls: list[list[str]] = []
    upserts: list[dict] = []

    class _FakeEmb:
        def __init__(self):
            pass

        def embed(self, texts):
            embed_calls.append(list(texts))
            return [[0.1] * 4 for _ in texts]

        def is_ready(self):  # pragma: no cover
            return {"status": "ready"}

    class _FakeVS:
        def __init__(self):
            pass

        def upsert(self, **kwargs):
            upserts.append(kwargs)

        def collection_info(self, name):  # pragma: no cover
            return {"exists": False, "count": 0}

        def ensure_writable(self):  # pragma: no cover
            return {"status": "writable"}

        def query(self, **kwargs):  # pragma: no cover
            raise NotImplementedError

    monkeypatch.setattr(_embedding, "BgeM3Provider", _FakeEmb)
    monkeypatch.setattr(_vectorstore, "ChromaProvider", _FakeVS)

    result = ingest_product_context(
        workspace_id="default",
        cards_path=str(cards_path),
    )
    assert result["ok"] is True, result
    assert result["collection"] == "product_default"
    assert result["chunks"] > 0
    assert len(upserts) == 1
    assert upserts[0]["collection"] == "product_default"
    assert len(upserts[0]["ids"]) == result["chunks"]
