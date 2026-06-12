"""Tests for #16-⑤ — per-chunk LLM extraction response cache (events/llm_cache.py).

Adversarial set: hit→0 LLM calls + tokens NOT summed / miss→put / VERSION,
model, lang, max_tokens change→invalidate / TTL expiry / corrupt JSON→miss /
uncreatable cache root→disabled, no crash / refresh bypass / same chunk at a
different index→parsed with CURRENT index / absent config key→cache off /
ledger calls_cached wiring (unit-level here; ledger math in test_llm_ledger).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from event_intel.errors import MCPError
from event_intel.events import extraction as _extraction
from event_intel.events import llm_cache as _llm_cache
from event_intel.events.extraction import extract_exhibitors
from event_intel.events.source_capture import SourceCapture

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)


@dataclass
class _LLMResp:
    text: str
    usage: dict[str, int]
    model: str = "fake-claude"
    stop_reason: str | None = None


class FakeLLM:
    """Pops canned responses in call order; records every call."""

    def __init__(self, responses: list[str] | None = None, *, model: str = "fake-claude"):
        self.responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []
        self.model = model

    def chat_once(self, *, system: str, user: str, max_tokens: int, temperature: float):
        self.calls.append({"system": system, "user": user, "max_tokens": max_tokens})
        text = self.responses.pop(0) if self.responses else "[]"
        return _LLMResp(text=text, usage={"input_tokens": 10, "output_tokens": 5}, model=self.model)


def _config(*, cache: dict | None = None, max_chars: int = 260, **llm_overrides):
    cfg = {
        "extraction": {
            "max_chars_per_chunk": max_chars,
            "max_chunks_per_event": 12,
            "source_snippet_min_chars": 20,
            "extraction_confidence_min": 0.6,
        },
        "llm": {"extract_max_tokens": 1024, **llm_overrides},
    }
    if cache is not None:
        cfg["extraction"]["llm_cache"] = cache
    return cfg


def _capture(text: str) -> SourceCapture:
    return SourceCapture(text=text, kind="html_file", source_ref="fixture.html")


def _canned(name: str) -> str:
    return json.dumps(
        [{"name": name, "source_snippet": f"{name} builds NPU compiler stacks."}]
    )


# Two ~250-char paragraphs → exactly 2 chunks at max_chars=260.
_TEXT = ("A" * 250) + "\n\n" + ("B" * 250)


def _run(llm, cache_dir, *, cache_cfg=None, text=_TEXT, lang="en", refresh=False,
         now=NOW, config=None):
    return extract_exhibitors(
        capture=_capture(text),
        lang=lang,
        llm_provider=llm,
        config=config or _config(cache={"enabled": True, "ttl_days": 14}
                                 if cache_cfg is None else cache_cfg),
        refresh=refresh,
        llm_cache_dir=cache_dir,
        now=now,
    )


# ---------- hit / miss core ----------


def test_first_run_misses_and_puts_second_run_hits(tmp_path):
    llm1 = FakeLLM([_canned("Alpha"), _canned("Beta")])
    r1 = _run(llm1, tmp_path)
    assert len(llm1.calls) == 2
    assert r1.chunks_cached == 0
    assert r1.usage == {"input_tokens": 20, "output_tokens": 10}
    assert len(list(tmp_path.glob("*.json"))) == 2  # miss → put

    llm2 = FakeLLM([_canned("SHOULD-NOT-BE-CALLED")])
    r2 = _run(llm2, tmp_path)
    assert llm2.calls == []                       # full hit → 0 LLM calls
    assert r2.chunks_cached == 2
    assert r2.chunks_processed == 2
    assert r2.usage == {"input_tokens": 0, "output_tokens": 0}  # tokens NOT summed
    assert sorted(c.name for c in r1.candidates) == sorted(c.name for c in r2.candidates)
    assert any("served from LLM cache" in w for w in r2.warnings)


def test_partial_hit_only_new_chunk_calls_llm(tmp_path):
    _run(FakeLLM([_canned("Alpha"), _canned("Beta")]), tmp_path)
    # Same first paragraph, new second one → 1 hit + 1 miss.
    new_text = ("A" * 250) + "\n\n" + ("C" * 250)
    llm = FakeLLM([_canned("Gamma")])
    r = _run(llm, tmp_path, text=new_text)
    assert len(llm.calls) == 1
    assert r.chunks_cached == 1
    assert sorted(c.name for c in r.candidates) == ["Alpha", "Gamma"]


def test_hit_is_parsed_with_current_chunk_index(tmp_path):
    # Prime: "B"*250 was chunk index 1. Re-run with only that paragraph →
    # it becomes chunk 0 and attribution must follow the CURRENT run.
    _run(FakeLLM([_canned("Alpha"), _canned("Beta")]), tmp_path)
    llm = FakeLLM()
    r = _run(llm, tmp_path, text="B" * 250)
    assert llm.calls == []
    (cand,) = r.candidates
    assert cand.name == "Beta"
    assert cand.chunk_indices == [0]


# ---------- invalidation axes ----------


def test_version_bump_invalidates(tmp_path, monkeypatch):
    _run(FakeLLM([_canned("Alpha"), _canned("Beta")]), tmp_path)
    monkeypatch.setattr(_llm_cache, "LLM_CACHE_VERSION", 999)
    llm = FakeLLM([_canned("Alpha"), _canned("Beta")])
    _run(llm, tmp_path)
    assert len(llm.calls) == 2


def test_model_change_invalidates(tmp_path):
    _run(FakeLLM([_canned("Alpha"), _canned("Beta")]), tmp_path)
    llm = FakeLLM([_canned("Alpha"), _canned("Beta")], model="other-model")
    _run(llm, tmp_path)
    assert len(llm.calls) == 2


def test_lang_change_invalidates(tmp_path):
    _run(FakeLLM([_canned("Alpha"), _canned("Beta")]), tmp_path)
    llm = FakeLLM([_canned("Alpha"), _canned("Beta")])
    _run(llm, tmp_path, lang="ko")
    assert len(llm.calls) == 2


def test_max_tokens_change_invalidates(tmp_path):
    # A different output cap can truncate differently — it joins the fingerprint.
    _run(FakeLLM([_canned("Alpha"), _canned("Beta")]), tmp_path)
    llm = FakeLLM([_canned("Alpha"), _canned("Beta")])
    _run(llm, tmp_path, config=_config(cache={"enabled": True, "ttl_days": 14},
                                       extract_max_tokens=2048))
    assert len(llm.calls) == 2


def test_ttl_expiry_invalidates_and_null_ttl_never_expires(tmp_path):
    _run(FakeLLM([_canned("Alpha"), _canned("Beta")]), tmp_path)
    # 15 days later with ttl_days=14 → stale, LLM called again.
    llm = FakeLLM([_canned("Alpha"), _canned("Beta")])
    _run(llm, tmp_path, now=NOW + timedelta(days=15))
    assert len(llm.calls) == 2
    # ttl_days: null → infinite: same 15-day-old entries (refreshed above, so
    # age is 0 — push the clock instead) still hit.
    llm2 = FakeLLM()
    r = _run(llm2, tmp_path, cache_cfg={"enabled": True, "ttl_days": None},
             now=NOW + timedelta(days=4000))
    assert llm2.calls == []
    assert r.chunks_cached == 2


def test_refresh_bypasses_reads_but_rewrites(tmp_path):
    _run(FakeLLM([_canned("Alpha"), _canned("Beta")]), tmp_path)
    llm = FakeLLM([_canned("Alpha2"), _canned("Beta2")])
    r = _run(llm, tmp_path, refresh=True)
    assert len(llm.calls) == 2
    assert r.chunks_cached == 0
    # refresh rewrote the entries — a following normal run sees the NEW text.
    llm3 = FakeLLM()
    r3 = _run(llm3, tmp_path)
    assert llm3.calls == []
    assert sorted(c.name for c in r3.candidates) == ["Alpha2", "Beta2"]


# ---------- robustness ----------


def test_corrupt_cache_file_is_a_miss_not_an_error(tmp_path):
    _run(FakeLLM([_canned("Alpha"), _canned("Beta")]), tmp_path)
    for f in tmp_path.glob("*.json"):
        f.write_text("{not json", encoding="utf-8")
    llm = FakeLLM([_canned("Alpha"), _canned("Beta")])
    r = _run(llm, tmp_path)
    assert len(llm.calls) == 2
    assert len(r.candidates) == 2


def test_non_dict_payload_and_non_string_response_are_misses(tmp_path):
    cache = _llm_cache.LlmExtractionCache(tmp_path, ttl_days=14)
    key_kwargs = {"model": "m", "lang": "en", "prompt_sha": "p", "chunk_text": "c"}
    path = tmp_path / f"{cache._key(**key_kwargs)}.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert cache.get(**key_kwargs, now=NOW) is None
    path.write_text(
        json.dumps({"cached_at": NOW.isoformat(), "response_text": 42}),
        encoding="utf-8",
    )
    assert cache.get(**key_kwargs, now=NOW) is None


def test_uncreatable_cache_root_disables_cache_without_failing(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the cache dir should go", encoding="utf-8")
    llm = FakeLLM([_canned("Alpha"), _canned("Beta")])
    r = _run(llm, blocker)  # mkdir on an existing FILE → OSError → disabled
    assert len(llm.calls) == 2
    assert r.chunks_cached == 0
    assert len(r.candidates) == 2


def test_cache_off_when_config_key_absent(tmp_path):
    llm = FakeLLM([_canned("Alpha"), _canned("Beta")])
    _run(llm, tmp_path, config=_config())  # no extraction.llm_cache block
    assert len(llm.calls) == 2
    assert list(tmp_path.glob("*.json")) == []  # nothing written either

    llm2 = FakeLLM([_canned("Alpha"), _canned("Beta")])
    r = _run(llm2, tmp_path, cache_cfg={"enabled": False})
    assert len(llm2.calls) == 2
    assert r.chunks_cached == 0


def test_failed_then_retried_run_replays_finished_chunks(tmp_path, monkeypatch):
    """The motivating loss shape: chunk 2 dies after retry → re-run pays only
    for the unfinished chunk."""
    monkeypatch.setattr(_extraction, "_CHUNK_RETRY_SLEEP_SECONDS", 0.0)

    class DiesOnSecondChunk(FakeLLM):
        def chat_once(self, **kwargs):
            if "B" * 250 in kwargs["user"]:
                self.calls.append(kwargs)
                raise RuntimeError("boom")
            return super().chat_once(**kwargs)

    dying = DiesOnSecondChunk([_canned("Alpha")])
    with pytest.raises(MCPError):
        _run(dying, tmp_path)

    llm = FakeLLM([_canned("Beta")])
    r = _run(llm, tmp_path)
    assert len(llm.calls) == 1            # chunk 0 replayed from cache
    assert r.chunks_cached == 1
    assert sorted(c.name for c in r.candidates) == ["Alpha", "Beta"]
