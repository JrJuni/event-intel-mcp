"""N4 — LLM query-rescue rung: eligibility, caps, deterministic re-search,
resume finality, defensive parsing. FakeLLM only — no live LLM/network.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from event_intel.events.enrichment import _parse_rescue_queries, enrich_exhibitors
from event_intel.events.extraction import ExhibitorCandidate


@dataclass
class _SR:
    title: str
    url: str
    snippet: str
    source: str | None = None
    published_at: object = None


@dataclass
class _LLMResp:
    text: str


class FakeLLM:
    def __init__(self, reply='["NeuroFahrzeug GmbH", "노이로 자동차"]'):
        self.reply = reply
        self.calls: list[dict] = []
        self.raise_exc: Exception | None = None

    def chat_once(self, *, system, user, max_tokens=4096, temperature=0.0):
        self.calls.append({"system": system, "user": user})
        if self.raise_exc:
            raise self.raise_exc
        return _LLMResp(text=self.reply)


class RescuableSearch:
    """Degrades the ORIGINAL name's queries; answers the rescue queries."""

    def __init__(self, *, rescue_web=None, rescue_news=None, degrade_names=("Neuro",)):
        self.rescue_web = rescue_web or []
        self.rescue_news = rescue_news or []
        self.degrade_names = degrade_names
        self.calls: list[str] = []
        self.last_call_degraded = False

    def search(self, query, *, kind, count, days=None, lang="en"):
        self.calls.append(query)
        self.last_call_degraded = False
        if any(n in query for n in self.degrade_names):
            self.last_call_degraded = True
            return []
        if kind == "web":
            return list(self.rescue_web)
        return list(self.rescue_news)

    def ping(self):  # pragma: no cover
        return {"status": "ok"}


def _config(rescue: dict | None = None):
    cfg = {
        "enrichment": {
            "max_companies": 30, "count_web": 5, "count_news": 5,
            "news_days_back": 180, "cache_enabled": True,
            "official_url_levenshtein_threshold": 0.3,
        },
    }
    if rescue is not None:
        cfg["enrichment"]["query_rescue"] = rescue
    return cfg


def _cand(name="Neuro Fahrzeug"):
    return ExhibitorCandidate(
        name=name, source_snippet="automotive perception supplier from Germany",
    )


def test_rescue_recovers_official_url_and_news(tmp_path):
    llm = FakeLLM()
    search = RescuableSearch(
        rescue_web=[_SR(title="NeuroFahrzeug GmbH — official",
                        url="https://neurofahrzeug.example.de", snippet="")],
        rescue_news=[_SR(title="NeuroFahrzeug GmbH wins OEM contract",
                         url="https://news.example.com/nf1", snippet="perception")],
    )
    result = enrich_exhibitors(
        candidates=[_cand()], workspace_id="n4a", lang="en",
        config=_config({"enabled": True, "max_companies": 5, "max_queries": 3}),
        search_provider=search, llm_provider=llm,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    row = result.rows[0]
    assert len(llm.calls) == 1
    assert row.official_url == "https://neurofahrzeug.example.de"
    assert len(row.news_signals) == 1
    assert row.degraded is False  # recovered → resume row is final/reusable
    assert any("query_rescue: recovered" in w for w in row.enrichment_warnings)
    # The rescue queries ran through the normal lanes (visible in search calls).
    assert any("NeuroFahrzeug GmbH" in q for q in search.calls)

    # Run 2 (same resume): row reused → zero LLM calls, zero searches.
    llm2, search2 = FakeLLM(), RescuableSearch()
    r2 = enrich_exhibitors(
        candidates=[_cand()], workspace_id="n4a", lang="en",
        config=_config({"enabled": True, "max_companies": 5, "max_queries": 3}),
        search_provider=search2, llm_provider=llm2,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    assert r2.skipped_from_resume == 1
    assert llm2.calls == [] and search2.calls == []


def test_genuine_empty_company_is_never_rescued(tmp_path):
    """Empty results WITHOUT degradation = the company really has nothing —
    no LLM call (criterion: rescue only when we likely MISSED)."""
    llm = FakeLLM()
    search = RescuableSearch(degrade_names=())  # answers (empty) without degrading
    enrich_exhibitors(
        candidates=[_cand()], workspace_id="n4b", lang="en",
        config=_config({"enabled": True, "max_companies": 5, "max_queries": 3}),
        search_provider=search, llm_provider=llm,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    assert llm.calls == []


def test_rescue_off_or_no_provider_means_zero_llm_calls(tmp_path):
    llm = FakeLLM()
    search = RescuableSearch()
    # (a) config absent → off even with a provider.
    enrich_exhibitors(
        candidates=[_cand()], workspace_id="n4c1", lang="en", config=_config(),
        search_provider=search, llm_provider=llm,
        cache_dir=tmp_path / "c1", resume_path=tmp_path / "r1.jsonl",
    )
    assert llm.calls == []
    # (b) enabled but no provider → off.
    result = enrich_exhibitors(
        candidates=[_cand()], workspace_id="n4c2", lang="en",
        config=_config({"enabled": True}),
        search_provider=RescuableSearch(),
        cache_dir=tmp_path / "c2", resume_path=tmp_path / "r2.jsonl",
    )
    assert result.rows[0].degraded is True  # still degraded, no rescue


def test_max_companies_caps_llm_calls(tmp_path):
    llm = FakeLLM()
    cands = [
        ExhibitorCandidate(name=f"Neuro Unit {i}", source_snippet="x" * 30)
        for i in range(4)
    ]
    enrich_exhibitors(
        candidates=cands, workspace_id="n4d", lang="en",
        config=_config({"enabled": True, "max_companies": 2, "max_queries": 3}),
        search_provider=RescuableSearch(degrade_names=("Neuro",)),
        llm_provider=llm,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    assert len(llm.calls) == 2  # cap enforced, deterministic candidate order


def test_llm_failure_degrades_gracefully(tmp_path):
    llm = FakeLLM()
    llm.raise_exc = RuntimeError("model offline")
    result = enrich_exhibitors(
        candidates=[_cand()], workspace_id="n4e", lang="en",
        config=_config({"enabled": True}),
        search_provider=RescuableSearch(), llm_provider=llm,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    row = result.rows[0]
    assert any("LLM proposal failed" in w for w in row.enrichment_warnings)
    assert row.degraded is True  # no recovery → still retried next run


def test_malformed_llm_reply_skips_company(tmp_path):
    llm = FakeLLM(reply="I think you should try searching differently.")
    result = enrich_exhibitors(
        candidates=[_cand()], workspace_id="n4f", lang="en",
        config=_config({"enabled": True}),
        search_provider=RescuableSearch(), llm_provider=llm,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    assert len(llm.calls) == 1
    assert result.rows[0].degraded is True  # stage completed, no recovery


def test_rescued_resume_row_persisted_with_final_state(tmp_path):
    llm = FakeLLM()
    search = RescuableSearch(
        rescue_news=[_SR(title="NeuroFahrzeug GmbH ships", url="https://n/1", snippet="")],
    )
    enrich_exhibitors(
        candidates=[_cand()], workspace_id="n4g", lang="en",
        config=_config({"enabled": True}),
        search_provider=search, llm_provider=llm,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    rows = [
        json.loads(ln)
        for ln in (tmp_path / "r.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["degraded"] is False
    assert len(rows[-1]["news_signals"]) == 1


# ---------- _parse_rescue_queries ----------


def test_parse_handles_fenced_and_embedded_json():
    fenced = 'Sure! Here you go:\n```json\n["q one", "q two"]\n```'
    assert _parse_rescue_queries(fenced, max_queries=3) == ["q one", "q two"]


def test_parse_dedupes_caps_and_drops_nonstrings():
    raw = '["Alpha", "alpha", 42, " Beta ", "Gamma", "Delta"]'
    assert _parse_rescue_queries(raw, max_queries=3) == ["Alpha", "Beta", "Gamma"]


def test_parse_garbage_returns_empty():
    assert _parse_rescue_queries("no json here", max_queries=3) == []
    assert _parse_rescue_queries('{"not": "a list"}', max_queries=3) == []
    assert _parse_rescue_queries(None, max_queries=3) == []
    assert _parse_rescue_queries("[broken", max_queries=3) == []
