"""Y1 L6 — draft_labels MCP tool: GPT-OAuth draft (silver) + flag, envelope.
Provider monkeypatched (no network).

The tool + its monkeypatched provider are resolved through the SAME freshly
imported module object, so the test is robust to the cold-start purge
re-importing a fresh providers.llm (module-identity drift, playbook #2)."""
from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

_CARD = (
    "schema_version: 2\nproduct_name: TestDB\none_liner: a database\n"
    "capabilities:\n  - name: vectors\n    keywords: [v]\n    buyer_pains: [p]\n    evidence_queries: [q]\n"
    "ideal_customer:\n  industries: [saas]\n  company_signals: [s]\n"
    "competitors:\n  - name: ClickHouse\nbad_fit:\n  - reason: gpu clouds\n"
)


def _sheet(tmp_path):
    p = tmp_path / "sheet.json"
    p.write_text(json.dumps([
        {"index": 0, "name": "ClickHouse", "overview": "olap db", "url": None, "label": ""},
        {"index": 1, "name": "Globex", "overview": "ai app", "url": None, "label": ""},
    ]), encoding="utf-8")
    return p


def _tool(monkeypatch=None):
    """Resolve the tool module fresh from sys.modules; optionally patch its
    provider factory (on the module the tool actually calls into)."""
    mod = importlib.import_module("event_intel.tools.draft_labels")
    if monkeypatch is not None:
        class _Fake:
            def chat_once(self, *, system, user, max_tokens, temperature):
                txt = json.dumps([
                    {"name": "ClickHouse", "label": "competitor", "confidence": 0.95, "rationale": "db"},
                    {"name": "Globex", "label": "target", "confidence": 0.9, "rationale": "buyer"},
                ])
                return SimpleNamespace(text=txt, usage={}, model="fake", stop_reason="end_turn")

        monkeypatch.setattr(mod._llm, "make_llm_provider", lambda *a, **k: _Fake())
    return mod


def test_draft_labels_tool_drafts_and_flags(tmp_path, monkeypatch):
    mod = _tool(monkeypatch)
    sheet = _sheet(tmp_path)
    card = tmp_path / "card.yaml"
    card.write_text(_CARD, encoding="utf-8")
    out = tmp_path / "drafted.json"

    res = mod.draft_labels(workspace_id="default", sheet_path=str(sheet),
                           card_path=str(card), out_path=str(out), lang="en")
    assert res["ok"] is True
    assert res["n"] == 2 and res["silver_auto"] == 1  # Globex auto silver
    assert res["needs_review_companies"] == ["ClickHouse"]  # gate class flagged
    rows = {r["name"]: r for r in json.loads(out.read_text(encoding="utf-8"))}
    assert rows["Globex"]["grade"] == "silver" and rows["ClickHouse"]["needs_review"] is True


def test_draft_labels_tool_missing_sheet_envelope():
    res = _tool().draft_labels(workspace_id="default", sheet_path="")
    assert res["ok"] is False and res["error_code"] == "INVALID_INPUT"


def test_draft_labels_tool_missing_card_envelope(tmp_path, monkeypatch):
    mod = _tool(monkeypatch)
    res = mod.draft_labels(workspace_id="default", sheet_path=str(_sheet(tmp_path)),
                           card_path=str(tmp_path / "nope.yaml"))
    assert res["ok"] is False and res["error_code"] == "PRODUCT_CONTEXT_MISSING"


def test_draft_labels_tool_bad_workspace_envelope(tmp_path):
    res = _tool().draft_labels(workspace_id="bad slug!", sheet_path=str(_sheet(tmp_path)))
    assert res["ok"] is False and res["error_code"] == "INVALID_INPUT"


def test_draft_labels_registered_on_server():
    import event_intel.mcp_server as server

    assert hasattr(server, "draft_labels")
