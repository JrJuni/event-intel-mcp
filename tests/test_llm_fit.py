"""Y1D D1 — LLM capability fit: parse / compute / apply units.

Adversarial set: malformed & truncated JSON, NaN/inf scores, out-of-range
clamping, provider exceptions, ledger recording on unparseable replies,
missing capability chunks, duplicate-name caching, rows/fit misalignment,
and the never-raise contract of apply().
"""
from __future__ import annotations

from event_intel.events.enrichment import EnrichedExhibitor, NewsSignal
from event_intel.rag.retriever import FitResult
from event_intel.runtime.llm_ledger import LlmUsageLedger
from event_intel.scoring import llm_fit

# ---------- parse_fit_response ----------


def test_parse_valid_json():
    out = llm_fit.parse_fit_response('{"score": 0.72, "reasoning": "Edge AI needs DB."}')
    assert out == (0.72, "Edge AI needs DB.")


def test_parse_clamps_out_of_range():
    assert llm_fit.parse_fit_response('{"score": 1.7}')[0] == 1.0
    assert llm_fit.parse_fit_response('{"score": -0.3}')[0] == 0.0


def test_parse_rejects_nan_and_inf():
    # min/max clamping would silently turn NaN into 1.0 — must be None instead.
    assert llm_fit.parse_fit_response('{"score": NaN}') is None
    assert llm_fit.parse_fit_response('{"score": Infinity}') is None


def test_parse_fenced_and_embedded_json():
    out = llm_fit.parse_fit_response('```json\n{"score": 0.5, "reasoning": "ok"}\n```')
    assert out == (0.5, "ok")
    out = llm_fit.parse_fit_response('Sure! Here it is: {"score": 0.4} hope it helps')
    assert out == (0.4, None)


def test_parse_truncated_json_score_net():
    # reasoning cut off by max_tokens → JSON does not parse → regex net.
    out = llm_fit.parse_fit_response('{"score": 0.65, "reasoning": "this got cut')
    assert out == (0.65, None)


def test_parse_unusable_inputs():
    assert llm_fit.parse_fit_response(None) is None
    assert llm_fit.parse_fit_response("") is None
    assert llm_fit.parse_fit_response("I cannot answer that.") is None
    assert llm_fit.parse_fit_response("[0.5, 0.6]") is None
    assert llm_fit.parse_fit_response('{"reasoning": "no score key"}') is None
    assert llm_fit.parse_fit_response('{"score": "high"}') is None


def test_parse_score_as_numeric_string_and_nonstr_reasoning():
    assert llm_fit.parse_fit_response('{"score": "0.66"}') == (0.66, None)
    assert llm_fit.parse_fit_response('{"score": 0.3, "reasoning": 42}') == (0.3, None)


# ---------- fakes ----------


class _Resp:
    def __init__(self, text):
        self.text = text
        self.usage = {"input_tokens": 30, "output_tokens": 12}


class _FitLLM:
    model = "fake-fit-model"

    def __init__(self, reply='{"score": 0.8, "reasoning": "fits"}', exc=None):
        self.reply = reply
        self.exc = exc
        self.calls = []

    def chat_once(self, *, system, user, max_tokens, temperature):
        self.calls.append({
            "system": system, "user": user,
            "max_tokens": max_tokens, "temperature": temperature,
        })
        if self.exc:
            raise self.exc
        return _Resp(self.reply)


def _hit(doc, kind="capability"):
    return {"id": "x", "document": doc, "metadata": {"kind": kind}, "distance": 0.2}


def _fit(name="Acme", cosine=0.55, hits=None):
    return FitResult(
        name=name, capability_fit=cosine,
        top_hits=[_hit("vector database chunk")] if hits is None else hits,
    )


def _row(name="Acme"):
    return EnrichedExhibitor(
        name=name, source_snippet="builds AI agents", description="agent infra",
        news_signals=[NewsSignal(title="Acme raises B", url="https://x", snippet="")],
    )


# ---------- compute_llm_capability_fit ----------


def test_compute_happy_path_records_ledger_and_prompt_contents():
    llm = _FitLLM()
    led = LlmUsageLedger()
    out = llm_fit.compute_llm_capability_fit(
        "Acme", "builds AI agents", ["vector database chunk"], llm, ledger=led,
    )
    assert out == (0.8, "fits")
    call = llm.calls[0]
    assert "Acme" in call["user"]
    assert "builds AI agents" in call["user"]
    assert "vector database chunk" in call["user"]
    assert call["temperature"] == 0.0 and call["max_tokens"] == 96
    s = led.summary()
    assert s["stages"]["llm_fit"]["calls"] == 1
    assert s["stages"]["llm_fit"]["models"] == ["fake-fit-model"]


def test_compute_no_chunks_makes_no_call():
    llm = _FitLLM()
    assert llm_fit.compute_llm_capability_fit("Acme", "x", [], llm) is None
    assert llm.calls == []


def test_compute_provider_exception_returns_none():
    llm = _FitLLM(exc=RuntimeError("api down"))
    assert llm_fit.compute_llm_capability_fit("Acme", "x", ["c"], llm) is None


def test_compute_unparseable_reply_still_records_usage():
    # tokens were spent even when the reply is junk — ledger must see them.
    llm = _FitLLM(reply="I refuse.")
    led = LlmUsageLedger()
    out = llm_fit.compute_llm_capability_fit("Acme", "x", ["c"], llm, ledger=led)
    assert out is None
    assert led.summary()["stages"]["llm_fit"]["input_tokens"] == 30


def test_compute_caps_chunk_count_and_length():
    llm = _FitLLM()
    chunks = ["A" * 1000, "B" * 1000, "C" * 1000, "D-NEVER-SENT"]
    llm_fit.compute_llm_capability_fit("Acme", "x", chunks, llm)
    user = llm.calls[0]["user"]
    assert "D-NEVER-SENT" not in user
    assert user.count("A") <= 450  # 400-char cap (+ a little template slack)


def test_prompt_lang_fallback():
    en = llm_fit.load_fit_prompt("en")
    ko = llm_fit.load_fit_prompt("ko")
    nonexistent = llm_fit.load_fit_prompt("fr")
    assert "{capabilities}" in en and "{name}" in en
    assert "채점 기준" in ko
    assert nonexistent == en


# ---------- apply_llm_capability_fit ----------


def test_apply_replaces_fit_and_preserves_cosine():
    fit = _fit(cosine=0.55)
    warnings = llm_fit.apply_llm_capability_fit(
        rows=[_row()], fit_results=[fit], llm_provider=_FitLLM(),
    )
    assert warnings == []
    assert fit.capability_fit == 0.8
    assert fit.capability_fit_source == "llm"
    assert fit.capability_fit_reasoning == "fits"
    assert fit.cosine_capability_fit == 0.55


def test_apply_failure_keeps_cosine_with_one_aggregated_warning():
    fits = [_fit("Acme", 0.55), _fit("Globex", 0.42)]
    warnings = llm_fit.apply_llm_capability_fit(
        rows=[_row("Acme"), _row("Globex")], fit_results=fits,
        llm_provider=_FitLLM(exc=RuntimeError("down")),
    )
    assert len(warnings) == 1
    assert "2/2" in warnings[0]
    assert fits[0].capability_fit == 0.55 and fits[0].capability_fit_source == "cosine"
    assert fits[1].capability_fit == 0.42 and fits[1].cosine_capability_fit is None


def test_apply_partial_failure_counts_only_failures():
    class _FlakyLLM(_FitLLM):
        def chat_once(self, **kw):
            if not self.calls:
                super().chat_once(**kw)
                raise RuntimeError("first call dies")
            return super().chat_once(**kw)

    fits = [_fit("Acme"), _fit("Globex")]
    warnings = llm_fit.apply_llm_capability_fit(
        rows=[_row("Acme"), _row("Globex")], fit_results=fits, llm_provider=_FlakyLLM(),
    )
    assert len(warnings) == 1 and "1/2" in warnings[0]
    assert fits[0].capability_fit_source == "cosine"
    assert fits[1].capability_fit_source == "llm"


def test_apply_duplicate_names_share_one_call():
    llm = _FitLLM()
    fits = [_fit("Acme"), _fit(" ACME ")]  # strip+casefold dedupe
    llm_fit.apply_llm_capability_fit(rows=[_row("Acme")], fit_results=fits, llm_provider=llm)
    assert len(llm.calls) == 1
    assert all(f.capability_fit == 0.8 for f in fits)


def test_apply_filters_non_capability_hits():
    llm = _FitLLM()
    fit = _fit(hits=[
        _hit("capability text"),
        _hit("competitor text", kind="competitor"),
        {"id": "broken"},                       # no document / metadata
        {"document": None, "metadata": {"kind": "capability"}},
    ])
    llm_fit.apply_llm_capability_fit(rows=[_row()], fit_results=[fit], llm_provider=llm)
    user = llm.calls[0]["user"]
    assert "capability text" in user
    assert "competitor text" not in user


def test_apply_no_capability_chunks_falls_back():
    fit = _fit(hits=[_hit("neg", kind="bad_fit")])
    warnings = llm_fit.apply_llm_capability_fit(
        rows=[_row()], fit_results=[fit], llm_provider=_FitLLM(),
    )
    assert "1/1" in warnings[0]
    assert fit.capability_fit_source == "cosine"


def test_apply_tolerates_none_top_hits_and_missing_row():
    fit = FitResult(name="Orphan", capability_fit=0.5, top_hits=None)
    # rows does not contain "Orphan" — evidence falls back, never raises.
    warnings = llm_fit.apply_llm_capability_fit(
        rows=[_row("SomeoneElse")], fit_results=[fit], llm_provider=_FitLLM(),
    )
    assert len(warnings) == 1  # no chunks → fallback


def test_apply_empty_inputs():
    assert llm_fit.apply_llm_capability_fit(
        rows=[], fit_results=[], llm_provider=_FitLLM(),
    ) == []


def test_apply_missing_row_still_calls_llm_with_name_only_evidence():
    llm = _FitLLM()
    fit = _fit("Orphan")
    llm_fit.apply_llm_capability_fit(rows=[], fit_results=[fit], llm_provider=llm)
    assert "(no evidence beyond the name)" in llm.calls[0]["user"]
    assert fit.capability_fit_source == "llm"
