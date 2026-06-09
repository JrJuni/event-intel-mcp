"""S6 — build_event_tier_list MCP tool e2e + input-validation tests.

Strategy: monkeypatch every provider seam (embedding / vectorstore / llm /
search) to deterministic fakes, plus point preflight at an inline config so
we don't need defaults.yaml on disk. Then drive the tool exactly as Claude
Desktop would and assert on envelope + output artifacts.

These complement the existing per-stage tests (test_source_capture,
test_event_extraction, test_enrichment, test_rag_ingest_retrieve,
test_scoring, test_report) — those exercise units; this one wires them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from event_intel.providers import embedding as _embedding
from event_intel.providers import llm as _llm
from event_intel.providers import search as _search
from event_intel.providers import vectorstore as _vectorstore
from event_intel.runtime import preflight as _preflight
from event_intel.storage.identifiers import validate_slug
from event_intel.tools.build_event_tier_list import (
    build_event_tier_list as build_tool,
)

# ---------- shared fakes ----------


_MIN_CONFIG = {
    "schema_version": 1,
    "llm": {
        "draft_cards_model": "fake-sonnet",
        "extract_exhibitors_model": "fake-sonnet",
        "rationale_model": "fake-sonnet",
        "draft_cards_max_tokens": 1024,
        "extract_max_tokens": 2048,
        "rationale_max_tokens": 128,
    },
    "extraction": {
        "max_chunks_per_event": 12,
        "max_chars_per_chunk": 8000,
        "source_snippet_min_chars": 20,
        "extraction_confidence_min": 0.6,
    },
    "enrichment": {
        "max_companies": 30,
        "brave_count_web": 3,
        "brave_count_news": 3,
        "news_days_back": 180,
        "fetch_timeout_seconds": 10,
        "fetch_body_max_chars": 2000,
        "cache_enabled": True,
        "official_url_levenshtein_threshold": 0.4,
    },
    "scoring": {
        "weights": {
            "capability_fit": 0.30,
            "source_confidence": 0.15,
            "buying_signal": 0.15,
            "website_verification": 0.10,
            "category_fit": 0.15,
            "competitor_penalty": -0.10,
            "bad_fit_penalty": -0.10,
        },
        "tier_rules": {
            "S": {"min_final_score": 5.5, "evidence_floor_min": 2},
            "A": {"min_final_score": 4.0, "evidence_floor_min": 1},
            "B": {"min_final_score": 2.0, "evidence_floor_min": 0},
            "C": {"min_final_score": 0.0, "evidence_floor_min": 0},
        },
    },
    "paths": {"chroma_dir": "~/.event-intel/chroma"},
}


_CANNED_EXTRACTION_JSON = (
    "["
    '{"name":"Mobius Labs","source_snippet":"Mobius Labs Booth A-12 On-device NPU compiler stack for edge AI",'
    '"url":"https://mobius.example.com","description":"On-device NPU compiler","extraction_confidence":0.95},'
    '{"name":"NeuroDrive Inc.","source_snippet":"NeuroDrive Inc. Booth A-15 Autonomous driving perception stack",'
    '"url":"https://neurodrive.example.com","description":"Autonomous driving perception","extraction_confidence":0.9},'
    '{"name":"EdgeVision Co., Ltd.","source_snippet":"EdgeVision Computer vision SDK for smart-city traffic cameras",'
    '"description":"Computer vision SDK","extraction_confidence":0.8}'
    "]"
)


class _FakeLLM:
    """Returns the canned extraction JSON for extraction calls and a short
    rationale/angle for rationale calls. Distinguishes by the prompt body."""

    def __init__(self, *, model="fake-sonnet", **_):
        self.model = model
        self.calls = []

    def ping(self):
        return {"status": "ok", "model": self.model}

    def chat_once(self, *, system, user, **kwargs):
        self.calls.append({"system": system[:60], "user_preview": user[:80]})
        if "RATIONALE:" in system or "rationale" in system.lower() or "B2B" in system:
            return _llm.LLMResponse(
                text="RATIONALE: Strong fit on NPU edge stack.\nANGLE: Ask about ADAS pilot.",
                usage={"input_tokens": 50, "output_tokens": 20},
                model=self.model,
            )
        return _llm.LLMResponse(
            text=_CANNED_EXTRACTION_JSON,
            usage={"input_tokens": 200, "output_tokens": 100},
            model=self.model,
        )

    def chat_cached(self, **kwargs):  # pragma: no cover
        raise NotImplementedError


class _FakeSearch:
    """Returns one good web hit + one news hit per query."""

    def __init__(self, **_):
        pass

    def ping(self):
        return {"status": "ok", "remaining_quota": 100}

    def search(self, query, *, kind, count, days=None, lang="en"):
        # Pull a stem from the query (e.g. '"Mobius Labs" official site' -> 'mobius')
        stem = (query.split('"')[1] if '"' in query else query).split()[0].lower()
        if kind == "news":
            return [
                _search.SearchResult(
                    title=f"{stem.capitalize()} raises Series B",
                    url=f"https://news.example.com/{stem}-series-b",
                    snippet=f"{stem.capitalize()} closed funding round.",
                    source="example-news",
                )
            ]
        return [
            _search.SearchResult(
                title=f"{stem.capitalize()} official site",
                url=f"https://{stem}.example.com",
                snippet="company homepage",
                source="example",
            )
        ]


class _FakeEmbedding:
    def __init__(self, **_):
        pass

    def is_ready(self):
        return {"status": "ready", "path": "/fake/bge-m3", "size_mb": 1320}

    def embed(self, texts):
        return [[0.1] * 8 for _ in texts]


class _FakeVectorStore:
    """Configurable product chunk count → exercises ready vs PRODUCT_CONTEXT_MISSING."""

    def __init__(self, *, product_chunks=12, **_):
        self.product_chunks = product_chunks

    def ensure_writable(self):
        return {"status": "writable", "path": "/fake/chroma"}

    def collection_info(self, name):
        if name.startswith("product_") and self.product_chunks > 0:
            return {"exists": True, "count": self.product_chunks}
        return {"exists": False, "count": 0}

    def query(self, *, collection, query_embeddings, top_k, where=None):
        # Each query returns top_k hits, all labeled as capability matches so
        # capability_fit_breakdown is non-trivial and capability_fit > 0.
        # (where is honored by real Chroma; this fake returns capability chunks
        # regardless — the negative pool then simply finds no competitor/bad_fit.)
        per_query = [
            {
                "id": f"chunk-{i}",
                "distance": 0.2 + i * 0.05,  # → similarity ~ 0.9 .. 0.7
                "metadata": {"kind": "capability", "capability_name": f"Cap {i + 1}"},
                "document": f"capability chunk {i + 1}",
            }
            for i in range(top_k)
        ]
        return [list(per_query) for _ in query_embeddings]

    def upsert(self, **kwargs):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def all_fakes(monkeypatch, tmp_path):
    """Wire fake providers + inline config + isolated outputs dir."""
    monkeypatch.setattr(_preflight, "load_config", lambda *a, **kw: dict(_MIN_CONFIG))
    monkeypatch.setattr(_llm, "AnthropicProvider", _FakeLLM)
    monkeypatch.setattr(_search, "BraveSearchProvider", _FakeSearch)
    monkeypatch.setattr(_embedding, "BgeM3Provider", _FakeEmbedding)
    # FakeVS factory carries product_chunks via a closure so tests can override.
    state = {"product_chunks": 12}

    def _vs_factory(**kw):
        return _FakeVectorStore(product_chunks=state["product_chunks"])

    monkeypatch.setattr(_vectorstore, "ChromaProvider", _vs_factory)
    monkeypatch.setenv("EVENT_INTEL_OUTPUT_DIR", str(tmp_path / "outputs"))

    # Isolate enrichment's home dir so cache + resume don't touch real ~/.event-intel
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home", raising=True)

    return state


# ---------- Input validation (no provider mocks needed) ----------


def test_build_invalid_input_for_korean_event_slug_returns_suggested_slug(all_fakes, repo_root):
    """R3-#3 acceptance — Korean event_slug → INVALID_INPUT + hint.suggested_slug."""
    out = build_tool(
        workspace_id="default",
        event_name="서울 ITS 2026",
        event_slug="서울 ITS 2026",   # contains spaces + Hangul + digits
        source_kind="html_file",
        source_ref=str(repo_root / "tests" / "fixtures" / "events" / "sample_exhibitors.html"),
    )
    assert out["ok"] is False
    assert out["error_code"] == "INVALID_INPUT"
    assert out["stage"] == "preflight"
    suggested = out["hint"]["suggested_slug"]
    assert validate_slug(suggested), f"suggested {suggested!r} must itself be a valid slug"
    assert out["hint"]["field"] == "event_slug"


def test_build_invalid_input_for_bad_workspace_id(all_fakes, repo_root):
    out = build_tool(
        workspace_id="bad slug!",
        event_name="X",
        event_slug="x",
        source_kind="html_file",
        source_ref=str(repo_root / "tests" / "fixtures" / "events" / "sample_exhibitors.html"),
    )
    assert out["ok"] is False
    assert out["error_code"] == "INVALID_INPUT"
    assert out["hint"]["field"] == "workspace_id"


def test_build_invalid_input_for_empty_event_name(all_fakes, repo_root):
    out = build_tool(
        workspace_id="default", event_name="", event_slug="x",
        source_kind="html_file",
        source_ref=str(repo_root / "tests" / "fixtures" / "events" / "sample_exhibitors.html"),
    )
    assert out["ok"] is False
    assert out["error_code"] == "INVALID_INPUT"
    assert "event_name" in (out["message"] + str(out["hint"]))


def test_build_invalid_input_for_empty_source_ref(all_fakes):
    out = build_tool(
        workspace_id="default", event_name="X", event_slug="x",
        source_kind="html_file", source_ref="",
    )
    assert out["ok"] is False
    assert out["error_code"] == "INVALID_INPUT"


# ---------- R3-#1: PRODUCT_CONTEXT_MISSING ----------


def test_build_returns_product_context_missing_when_no_ingest(all_fakes, repo_root):
    """R3-#1 acceptance — build before any ingest → preflight raises PRODUCT_CONTEXT_MISSING."""
    all_fakes["product_chunks"] = 0   # FakeVS now says "no product collection"
    out = build_tool(
        workspace_id="default",
        event_name="Sample Expo",
        event_slug="sample_expo",
        source_kind="html_file",
        source_ref=str(repo_root / "tests" / "fixtures" / "events" / "sample_exhibitors.html"),
    )
    assert out["ok"] is False, out
    assert out["error_code"] == "PRODUCT_CONTEXT_MISSING"
    assert out["stage"] == "preflight"
    assert "event-intel ingest" in str(out["hint"]).lower() or "ingest" in str(out["hint"])
    assert out["hint"]["collection"] == "product_default"


# ---------- e2e happy path ----------


def test_build_e2e_runs_full_pipeline_and_writes_artifacts(all_fakes, repo_root, tmp_path):
    """Full pipeline with fakes: capture → extract → enrich → fit → score → render → write."""
    out = build_tool(
        workspace_id="default",
        event_name="Sample Expo 2026",
        event_slug="sample_expo_2026",
        source_kind="html_file",
        source_ref=str(repo_root / "tests" / "fixtures" / "events" / "sample_exhibitors.html"),
        run_rationale=True,
    )
    assert out["ok"] is True, out
    assert out["workspace_id"] == "default"
    assert out["event_slug"] == "sample_expo_2026"
    assert out["candidates_extracted"] >= 3
    counts = out["tier_counts"]
    assert set(counts.keys()) >= {"S", "A", "B", "C", "needs_review"}
    # md + yaml files exist + non-empty.
    md_path = Path(out["tier_list_md_path"])
    yaml_path = Path(out["tier_list_yaml_path"])
    assert md_path.is_file() and md_path.stat().st_size > 0
    assert yaml_path.is_file() and yaml_path.stat().st_size > 0
    md = md_path.read_text(encoding="utf-8")
    assert "# Sample Expo 2026" in md
    assert "## Tier S" in md
    assert "## Tier A" in md
    assert "## Needs Review" in md
    # At least one exhibitor name surfaced.
    assert "Mobius" in md or "NeuroDrive" in md or "EdgeVision" in md
    # CS1: run-summary emitted next to the reports, with run_id + fingerprint.
    import json as _json
    rs_path = Path(out["run_summary_path"])
    assert rs_path.is_file() and rs_path.name == "run_summary.json"
    rs_doc = _json.loads(rs_path.read_text(encoding="utf-8"))
    assert rs_doc["run_id"] and rs_doc["run_fingerprint"]
    assert rs_doc["target_mode"] == "customer"
    # Y2.1d: reports also registered as artifacts (path-free remote download).
    from event_intel.storage import artifact_registry as _reg

    md_aid = out["tier_list_md_artifact_id"]
    yaml_aid = out["tier_list_yaml_artifact_id"]
    assert md_aid and yaml_aid
    # Compare against newline-normalized text (the on-disk file is CRLF on Windows
    # text-mode write; the artifact stores the in-memory LF content).
    assert _reg.get_artifact(workspace_id="default", artifact_id=md_aid) == md.encode("utf-8")
    assert _reg.get_artifact(workspace_id="default", artifact_id=yaml_aid) == yaml_path.read_text(
        encoding="utf-8"
    ).encode("utf-8")
    assert rs_doc["scored"] == sum(v for k, v in counts.items() if k != "needs_review")
    assert rs_doc["companies"] and "capability_fit" in rs_doc["companies"][0]["dimensions"]


def test_build_skips_enrichment_when_disabled(all_fakes, repo_root):
    """enrichment_enabled=False → no Brave calls, no news, but pipeline still completes."""
    out = build_tool(
        workspace_id="default",
        event_name="Sample Expo",
        event_slug="sample_expo",
        source_kind="html_file",
        source_ref=str(repo_root / "tests" / "fixtures" / "events" / "sample_exhibitors.html"),
        enrichment_enabled=False,
        run_rationale=False,   # also skip rationale to keep this fast
    )
    assert out["ok"] is True, out
    assert out["cache_hits"] == 0
    assert out["cache_misses"] == 0
    # Warning about disabled enrichment surfaces.
    assert any("enrichment disabled" in w for w in out["warnings"])


def test_build_e2e_korean_lang(all_fakes, repo_root):
    out = build_tool(
        workspace_id="default",
        event_name="샘플 박람회",
        event_slug="sample_kr",
        source_kind="html_file",
        source_ref=str(repo_root / "tests" / "fixtures" / "events" / "sample_exhibitors_ko.html"),
        lang="ko",
        run_rationale=False,
    )
    assert out["ok"] is True, out
    md = Path(out["tier_list_md_path"]).read_text(encoding="utf-8")
    assert "# 샘플 박람회" in md
    assert "## Tier S — 최우선" in md or "검토 필요" in md


# ---------- 5-tool surface smoke ----------


def test_mcp_server_exposes_five_tools_with_real_handlers():
    """check_runtime / draft_capability_cards / validate_capability_cards /
    ingest_product_context / build_event_tier_list are all live (no S0 stub)."""
    import event_intel.mcp_server as server

    for name in (
        "check_runtime",
        "draft_capability_cards",
        "validate_capability_cards",
        "ingest_product_context",
        "build_event_tier_list",
    ):
        assert hasattr(server, name), f"mcp_server missing tool {name!r}"


def test_build_envelope_propagates_mcperror_from_capture(all_fakes, tmp_path):
    """If source_capture fails (e.g. file doesn't exist), the build tool must
    surface SOURCE_CAPTURE_FAILED at stage=extraction — not a generic
    INTERNAL/preflight envelope."""
    out = build_tool(
        workspace_id="default",
        event_name="X",
        event_slug="x",
        source_kind="html_file",
        source_ref=str(tmp_path / "does_not_exist.html"),
    )
    assert out["ok"] is False
    assert out["error_code"] == "SOURCE_CAPTURE_FAILED"
    assert out["stage"] == "extraction"
