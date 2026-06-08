"""Y1 L1 — GPT-OAuth draft labeling (silver). FakeLLM, no network.
Batches, tolerant parsing, missing/invalid → needs_review, confidence coercion."""
from __future__ import annotations

import json
from types import SimpleNamespace

from event_intel.eval import label_draft as LD


class _FakeLLM:
    """Returns a canned JSON response per chat_once call, in order."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def chat_once(self, *, system, user, max_tokens, temperature):
        self.calls.append({"system": system, "user": user})
        text = self._responses[len(self.calls) - 1] if len(self.calls) <= len(self._responses) else "[]"
        return SimpleNamespace(text=text, usage={}, model="fake", stop_reason="end_turn")


def _rows(names):
    return [{"index": i, "name": n, "overview": f"{n} does things", "url": None, "label": ""}
            for i, n in enumerate(names)]


def _resp(*pairs):
    return json.dumps([
        {"name": n, "label": lab, "confidence": c, "rationale": "r"} for n, lab, c in pairs
    ], ensure_ascii=False)


# ---------- batching ----------

def test_batches_by_size():
    rows = _rows([f"C{i}" for i in range(5)])
    llm = _FakeLLM(["[]", "[]", "[]"])
    LD.draft_labels(sheet_rows=rows, product_header="P", llm_provider=llm, batch_size=2, lang="en")
    assert len(llm.calls) == 3  # 2 + 2 + 1


# ---------- happy path mapping ----------

def test_maps_label_confidence_rationale():
    rows = _rows(["Acme", "Globex"])
    llm = _FakeLLM([_resp(("Acme", "competitor", 0.9), ("Globex", "target", 0.7))])
    out = LD.draft_labels(sheet_rows=rows, product_header="P", llm_provider=llm, lang="en")
    by = {r["name"]: r for r in out}
    assert by["Acme"]["suggested_label"] == "competitor" and by["Acme"]["confidence"] == 0.9
    assert by["Globex"]["suggested_label"] == "target" and by["Globex"]["needs_review"] is False
    assert all(r["source"] == "gpt_draft" for r in out)
    # the human label field is left blank — draft only fills the suggestion
    assert all(r["label"] == "" for r in out)


# ---------- failure modes → needs_review ----------

def test_invalid_label_flags_needs_review():
    rows = _rows(["Acme"])
    llm = _FakeLLM([_resp(("Acme", "rival", 0.9))])  # not in vocab
    out = LD.draft_labels(sheet_rows=rows, product_header="P", llm_provider=llm, lang="en")
    assert out[0]["needs_review"] is True and out[0]["suggested_label"] == ""


def test_missing_company_in_response_flags_needs_review():
    rows = _rows(["Acme", "Globex"])
    llm = _FakeLLM([_resp(("Acme", "target", 0.8))])  # Globex omitted
    out = LD.draft_labels(sheet_rows=rows, product_header="P", llm_provider=llm, lang="en")
    by = {r["name"]: r for r in out}
    assert by["Acme"]["needs_review"] is False
    assert by["Globex"]["needs_review"] is True and by["Globex"]["confidence"] == 0.0


def test_llm_exception_flags_whole_batch():
    class _BoomLLM:
        def chat_once(self, **kw):
            raise RuntimeError("provider down")

    rows = _rows(["Acme", "Globex"])
    out = LD.draft_labels(sheet_rows=rows, product_header="P", llm_provider=_BoomLLM(), lang="en")
    assert all(r["needs_review"] is True for r in out)


# ---------- tolerant parsing ----------

def test_parses_fenced_and_dict_wrapped():
    fenced = "```json\n" + _resp(("Acme", "target", 0.6)) + "\n```"
    assert LD._parse_draft_response(fenced, {"Acme"})["Acme"]["label"] == "target"
    wrapped = json.dumps({"labels": [{"name": "Acme", "label": "target", "confidence": 0.6}]})
    assert LD._parse_draft_response(wrapped, {"Acme"})["Acme"]["label"] == "target"
    # garbage → empty, never raises
    assert LD._parse_draft_response("not json at all", {"Acme"}) == {}


def test_confidence_coercion():
    assert LD._coerce_confidence(1.5) == 1.0
    assert LD._coerce_confidence("high") == 0.9
    assert LD._coerce_confidence("???") == 0.5


# ---------- prompt + system header ----------

def test_system_prompt_includes_product_header():
    rows = _rows(["Acme"])
    llm = _FakeLLM([_resp(("Acme", "target", 0.8))])
    LD.draft_labels(sheet_rows=rows, product_header="PRODUCT=MongoDB", llm_provider=llm, lang="en")
    assert "PRODUCT=MongoDB" in llm.calls[0]["system"]
    assert "JSON array" in llm.calls[0]["system"]  # from prompts/en/draft_labels.txt
