"""B1 — news body fetch lane: cache, robots gate, byte cap, per-item
degradation, enrichment wiring. No live network (robots patched, fetch faked).
"""
from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from event_intel.events.enrichment import NewsSignal, _from_dict, _to_dict, enrich_exhibitors
from event_intel.events.extraction import ExhibitorCandidate
from event_intel.events.news_body import NewsBodyConfig, NewsBodyFetcher
from event_intel.runtime.failure_log import FailureLog

NOW = datetime(2026, 6, 11, tzinfo=UTC)

ARTICLE_HTML = (
    "<html><body><article><h1>Mobius Labs ships NPU compiler</h1>"
    + "".join(
        f"<p>Paragraph {i}: Mobius Labs announced its on-device NPU compiler "
        "product for edge AI workloads, targeting automotive customers.</p>"
        for i in range(10)
    )
    + "</article></body></html>"
)


@pytest.fixture(autouse=True)
def _robots_allow(monkeypatch):
    """Default: robots allows everything and makes NO network call."""
    monkeypatch.setattr(
        "event_intel.acquisition.robots.is_allowed",
        lambda url, *, user_agent="event-intel-mcp": True,
    )


def _cfg(**kw) -> NewsBodyConfig:
    base: dict = {"enabled": True, "max_per_company": 12, "min_body_chars": 50,
                  "cache_ttl_days": 14}
    base.update(kw)
    return NewsBodyConfig.from_dict(base)


def _ok_fetch(url):
    return {"status": 200, "text": ARTICLE_HTML, "final_url": url, "truncated": False}


def _sig(url="https://news.example.com/a1"):
    return NewsSignal(title="Mobius Labs ships", url=url, snippet="npu")


# ---------- fetch + cache ----------


def test_attach_body_success_and_cache_reuse(tmp_path):
    f1 = NewsBodyFetcher(cfg=_cfg(), cache_dir=tmp_path / "b", now=NOW, fetch_fn=_ok_fetch)
    sig = _sig()
    assert f1.attach_bodies([sig]) == 1
    assert sig.body_sha and sig.body_chars > 50
    body = f1.load_body(sig.url)
    assert body and "NPU compiler" in body

    def _boom(url):
        raise AssertionError("must be served from cache")

    f2 = NewsBodyFetcher(cfg=_cfg(), cache_dir=tmp_path / "b", now=NOW, fetch_fn=_boom)
    sig2 = _sig()
    assert f2.attach_bodies([sig2]) == 1  # cache hit, no fetch
    assert sig2.body_sha == sig.body_sha


def test_cache_ttl_expiry_refetches(tmp_path):
    f1 = NewsBodyFetcher(cfg=_cfg(cache_ttl_days=1), cache_dir=tmp_path / "b",
                         now=NOW, fetch_fn=_ok_fetch)
    f1.attach_bodies([_sig()])
    later = datetime(2026, 6, 20, tzinfo=UTC)  # 9 days later, ttl 1 day
    calls = []

    def _counting(url):
        calls.append(url)
        return _ok_fetch(url)

    f2 = NewsBodyFetcher(cfg=_cfg(cache_ttl_days=1), cache_dir=tmp_path / "b",
                         now=later, fetch_fn=_counting)
    assert f2.attach_bodies([_sig()]) == 1
    assert len(calls) == 1  # stale → re-fetched


def test_robots_denied_not_cached_and_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "event_intel.acquisition.robots.is_allowed",
        lambda url, *, user_agent="event-intel-mcp": False,
    )
    flog_path = tmp_path / "fetch.jsonl"
    f = NewsBodyFetcher(cfg=_cfg(), cache_dir=tmp_path / "b", now=NOW,
                        fetch_fn=_ok_fetch, failure_log=FailureLog(flog_path))
    sig = _sig()
    assert f.attach_bodies([sig]) == 0
    assert sig.body_sha is None
    assert list((tmp_path / "b").glob("*.json")) == []  # transient → not cached
    rows = [json.loads(ln) for ln in flog_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["outcome"] == "robots_denied" and rows[0]["kind"] == "body"


def test_http_error_not_cached_and_logged(tmp_path):
    flog_path = tmp_path / "fetch.jsonl"
    f = NewsBodyFetcher(
        cfg=_cfg(), cache_dir=tmp_path / "b", now=NOW,
        fetch_fn=lambda url: {"status": 403, "text": None, "error": "HTTP 403"},
        failure_log=FailureLog(flog_path),
    )
    assert f.attach_bodies([_sig()]) == 0
    assert list((tmp_path / "b").glob("*.json")) == []
    rows = [json.loads(ln) for ln in flog_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["outcome"] == "error" and rows[0]["status"] == 403


def test_too_short_body_cached_negative(tmp_path):
    calls = []

    def _thin(url):
        calls.append(url)
        return {"status": 200, "text": ARTICLE_HTML, "final_url": url}

    cfg = _cfg(min_body_chars=100_000)  # nothing passes
    f = NewsBodyFetcher(cfg=cfg, cache_dir=tmp_path / "b", now=NOW, fetch_fn=_thin)
    sig = _sig()
    assert f.attach_bodies([sig]) == 0 and sig.body_sha is None
    assert len(list((tmp_path / "b").glob("*.json"))) == 1  # negative verdict cached
    assert f.attach_bodies([_sig()]) == 0
    assert len(calls) == 1  # second pass served the cached verdict


def test_max_per_company_caps_fetches(tmp_path):
    calls = []

    def _counting(url):
        calls.append(url)
        return _ok_fetch(url)

    f = NewsBodyFetcher(cfg=_cfg(max_per_company=2), cache_dir=tmp_path / "b",
                        now=NOW, fetch_fn=_counting)
    sigs = [_sig(f"https://news.example.com/{i}") for i in range(5)]
    assert f.attach_bodies(sigs) == 2
    assert len(calls) == 2


def test_raising_fetch_fn_degrades_not_raises(tmp_path):
    def _boom(url):
        raise RuntimeError("socket reset")

    flog_path = tmp_path / "fetch.jsonl"
    f = NewsBodyFetcher(cfg=_cfg(), cache_dir=tmp_path / "b", now=NOW,
                        fetch_fn=_boom, failure_log=FailureLog(flog_path))
    assert f.attach_bodies([_sig()]) == 0  # no raise
    rows = [json.loads(ln) for ln in flog_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["outcome"] == "error" and "RuntimeError" in rows[0]["exc_classes"][0]


# ---------- live fetch mechanics (MockTransport, no network) ----------


def test_fetch_live_streaming_byte_cap(tmp_path):
    import httpx

    big = b"<html>" + b"x" * 100_000 + b"</html>"

    def handler(request):
        return httpx.Response(200, content=big, headers={"content-type": "text/html"})

    f = NewsBodyFetcher(
        cfg=_cfg(max_bytes_per_page=10_000), cache_dir=tmp_path / "b", now=NOW,
        transport=httpx.MockTransport(handler),
    )
    result = f._fetch_live("https://news.example.com/big")
    assert result["truncated"] is True
    assert result["text"] is not None and len(result["text"]) <= 10_000
    assert result["status"] == 200


def test_fetch_live_http_500_returns_error(tmp_path):
    import httpx

    f = NewsBodyFetcher(
        cfg=_cfg(), cache_dir=tmp_path / "b", now=NOW,
        transport=httpx.MockTransport(lambda req: httpx.Response(500)),
    )
    result = f._fetch_live("https://news.example.com/x")
    assert result["text"] is None and "HTTP 500" in result["error"]


# ---------- cold import ----------


def test_news_body_module_import_stays_cold():
    # Snapshot + restore: permanently deleting httpx from sys.modules breaks
    # later tests whose module-level `import httpx` object no longer matches
    # what production code re-imports (identity-based monkeypatching fails).
    saved = {
        m: sys.modules[m]
        for m in list(sys.modules)
        if m in ("httpx", "trafilatura")
        or m.startswith(("httpx.", "trafilatura."))
    }
    saved_self = sys.modules.pop("event_intel.events.news_body", None)
    for m in saved:
        del sys.modules[m]
    try:
        importlib.import_module("event_intel.events.news_body")
        assert "httpx" not in sys.modules and "trafilatura" not in sys.modules
    finally:
        sys.modules.update(saved)
        if saved_self is not None:
            sys.modules["event_intel.events.news_body"] = saved_self


# ---------- enrichment wiring ----------


def _enrich_config(**enrichment_overrides):
    cfg = {
        "enrichment": {
            "max_companies": 30, "count_web": 5, "count_news": 5,
            "news_days_back": 180, "cache_enabled": True,
            "official_url_levenshtein_threshold": 0.4,
        },
    }
    cfg["enrichment"].update(enrichment_overrides)
    return cfg


class _FakeSearch:
    def __init__(self, news):
        self._news = news
        self.last_call_degraded = False

    def search(self, query, *, kind, count, days=None, lang="en"):
        return list(self._news) if kind == "news" else []

    def ping(self):  # pragma: no cover
        return {"status": "ok"}


@dataclass
class _SR:
    title: str
    url: str
    snippet: str
    source: str | None = None
    published_at: object = None


class _RecordingFetcher:
    def __init__(self):
        self.batches: list[list] = []

    def attach_bodies(self, signals):
        self.batches.append(list(signals))
        for s in signals:
            s.body_sha = "deadbeef"
            s.body_chars = 1234
        return len(signals)


def test_enrichment_fetches_bodies_for_gated_news_only(tmp_path):
    news = [
        _SR(title="Mobius Labs raises Series B", url="https://news.example.com/on", snippet="npu"),
        _SR(title="Weather report for Berlin", url="https://news.example.com/off", snippet="sunny"),
    ]
    fetcher = _RecordingFetcher()
    result = enrich_exhibitors(
        candidates=[ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)],
        workspace_id="b1ws", lang="en", config=_enrich_config(),
        search_provider=_FakeSearch(news),
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
        body_fetcher=fetcher,
    )
    assert len(fetcher.batches) == 1
    gated = fetcher.batches[0]
    assert [n.url for n in gated] == ["https://news.example.com/on"]  # off-topic excluded
    row = result.rows[0]
    on = next(n for n in row.news_signals if n.url.endswith("/on"))
    assert on.body_sha == "deadbeef" and on.body_chars == 1234


def test_enrichment_body_lane_off_by_default(tmp_path):
    news = [_SR(title="Mobius Labs raises", url="https://news.example.com/on", snippet="")]
    result = enrich_exhibitors(
        candidates=[ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)],
        workspace_id="b1off", lang="en", config=_enrich_config(),  # no news_body key
        search_provider=_FakeSearch(news),
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    assert all(n.body_sha is None for n in result.rows[0].news_signals)


def test_enrichment_builds_fetcher_from_config_when_enabled(tmp_path, monkeypatch):
    built = []

    class _StubFetcher:
        def __init__(self, **kw):
            built.append(kw)

        def attach_bodies(self, signals):
            return 0

    monkeypatch.setattr("event_intel.events.news_body.NewsBodyFetcher", _StubFetcher)
    enrich_exhibitors(
        candidates=[ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)],
        workspace_id="b1auto", lang="en",
        config=_enrich_config(news_body={"enabled": True, "max_per_company": 3}),
        search_provider=_FakeSearch([]),
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    assert len(built) == 1
    assert built[0]["cfg"].max_per_company == 3
    assert built[0]["failure_log"] is not None


def test_news_signal_body_fields_roundtrip_resume():
    from event_intel.events.enrichment import EnrichedExhibitor

    row = EnrichedExhibitor(
        name="A", source_snippet="s",
        news_signals=[NewsSignal(title="t", url="u", snippet="s",
                                 body_sha="abc123", body_chars=999)],
    )
    restored = _from_dict(_to_dict(row))
    assert restored.news_signals[0].body_sha == "abc123"
    assert restored.news_signals[0].body_chars == 999


# ---------- B2 (4) near-duplicate detection ----------


def _bodied_fetcher(tmp_path, bodies: dict[str, str]):
    """Fetcher whose fetch_fn serves canned HTML per URL."""

    def _fetch(url):
        return {"status": 200, "text": bodies[url], "final_url": url}

    return NewsBodyFetcher(cfg=_cfg(min_body_chars=10), cache_dir=tmp_path / "b",
                           now=NOW, fetch_fn=_fetch)


def _html(text: str) -> str:
    return "<html><body><article><h1>head</h1>" + "".join(
        f"<p>{text} sentence {i} continues with more context.</p>" for i in range(6)
    ) + "</article></body></html>"


def test_find_near_duplicates_exact_and_shingle(tmp_path):
    u1, u2, u3 = (f"https://news{i}.example.com/a" for i in range(3))
    base = "Mobius Labs launched its NPU compiler for automotive edge AI"
    bodies = {
        u1: _html(base),
        u2: _html(base),  # identical body, different outlet -> dup
        u3: _html("Totally different company ships quantum networking hardware"),
    }
    f = _bodied_fetcher(tmp_path, bodies)
    sigs = [NewsSignal(title="t", url=u, snippet="s") for u in (u1, u2, u3)]
    assert f.attach_bodies(sigs) == 3
    dups = f.find_near_duplicates(sigs)
    assert [d.url for d in dups] == [u2]  # later copy flagged, distinct kept


def test_find_near_duplicates_skips_bodiless(tmp_path):
    f = _bodied_fetcher(tmp_path, {})
    sigs = [NewsSignal(title="t", url="https://x.example.com", snippet="s")]
    assert f.find_near_duplicates(sigs) == []  # no body_sha -> never a dup


def test_shingle_jaccard_primitives():
    from event_intel.events.news_body import _jaccard, _shingles

    a = _shingles("one two three four five six seven eight nine ten")
    assert _jaccard(a, a) == 1.0
    assert _jaccard(a, set()) == 0.0
    assert _shingles("") == set()
    assert _shingles("short text") == {"short text"}  # < n words -> one shingle


# ---------- B2 (2) body content gate + (4) dedupe wiring in enrichment ----------


class _GateFetcher:
    """Bodies pre-baked per URL; mimics the B1 fetcher contract."""

    def __init__(self, bodies: dict[str, str], dups: list | None = None):
        self._bodies = bodies
        self._dups = set(dups or [])

    def attach_bodies(self, signals):
        n = 0
        for s in signals:
            if s.url in self._bodies:
                s.body_sha = "sha-" + s.url[-4:]
                s.body_chars = len(self._bodies[s.url])
                n += 1
        return n

    def load_body(self, url):
        return self._bodies.get(url)

    def find_near_duplicates(self, signals):
        return [s for s in signals if s.url in self._dups]


def test_body_gate_excludes_wrong_entity_from_floor(tmp_path):
    """Snippet matches the name but the BODY is about dust storms (criterion 2):
    not floor evidence. A body that does mention the company stays."""
    on_url = "https://news.example.com/on"
    wrong_url = "https://news.example.com/wrong"
    news = [
        _SR(title="Mobius Labs raises", url=on_url, snippet="round"),
        _SR(title="Mobius Labs update", url=wrong_url, snippet="story"),
    ]
    fetcher = _GateFetcher({
        on_url: "Mobius Labs shipped a new NPU compiler product for edge AI.",
        wrong_url: "A giant dust storm crossed the desert; weather alerts issued.",
    })
    result = enrich_exhibitors(
        candidates=[ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)],
        workspace_id="b2gate", lang="en", config=_enrich_config(),
        search_provider=_FakeSearch(news),
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
        body_fetcher=fetcher,
    )
    row = result.rows[0]
    ev_urls = {e.url for e in row.evidence}
    assert on_url in ev_urls
    assert wrong_url not in ev_urls          # body gate excluded it from floor
    assert len(row.news_signals) == 2        # still listed as news (buying signal)


def test_body_gate_fails_open_without_body(tmp_path):
    """Snippet-gated news without a fetchable body keeps pre-B2 behavior."""
    url = "https://news.example.com/nobody"
    news = [_SR(title="Mobius Labs raises", url=url, snippet="round")]
    fetcher = _GateFetcher({})  # no bodies at all
    result = enrich_exhibitors(
        candidates=[ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)],
        workspace_id="b2open", lang="en", config=_enrich_config(),
        search_provider=_FakeSearch(news),
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
        body_fetcher=fetcher,
    )
    assert url in {e.url for e in result.rows[0].evidence}


def test_enrichment_drops_near_duplicate_news(tmp_path):
    u1, u2 = "https://a.example.com/x", "https://b.example.com/x"
    news = [
        _SR(title="Mobius Labs raises", url=u1, snippet="round"),
        _SR(title="Mobius Labs raises (syndicated)", url=u2, snippet="round"),
    ]
    body = "Mobius Labs announced a funding round for its NPU compiler."
    fetcher = _GateFetcher({u1: body, u2: body}, dups=[u2])
    result = enrich_exhibitors(
        candidates=[ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)],
        workspace_id="b2dup", lang="en", config=_enrich_config(),
        search_provider=_FakeSearch(news),
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
        body_fetcher=fetcher,
    )
    row = result.rows[0]
    assert [n.url for n in row.news_signals] == [u1]   # dup dropped entirely
    assert u2 not in {e.url for e in row.evidence}
    assert any("dedup" in w for w in row.enrichment_warnings)


# ---------- B2 (3) product relatedness (report-only) ----------


class _FakeEmbedding:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]


class _FakeVectorStore:
    def __init__(self, distances):
        self._distances = distances  # one list of hit-distances per query

    def query(self, *, collection, query_embeddings, top_k=5, where=None):
        assert collection == "product_wsx"
        return [
            [{"id": f"c{i}", "distance": d, "document": "", "metadata": {}}
             for d in dists]
            for i, dists in enumerate(self._distances[: len(query_embeddings)])
        ]


def test_gather_news_relatedness_maps_max_similarity():
    from event_intel.events.enrichment import EnrichedExhibitor
    from event_intel.events.news_body import gather_news_relatedness

    rows = [EnrichedExhibitor(
        name="Acme", source_snippet="s",
        news_signals=[
            NewsSignal(title="t", url="https://n/1", snippet="s",
                       body_sha="aa", body_chars=500),
            NewsSignal(title="t2", url="https://n/2", snippet="s"),  # no body
        ],
    )]
    out = gather_news_relatedness(
        rows=rows,
        body_loader=lambda url: "body text" if url == "https://n/1" else None,
        collection="product_wsx",
        embedding_provider=_FakeEmbedding(),
        vectorstore_provider=_FakeVectorStore([[0.4, 1.2]]),  # sims 0.8, 0.4
    )
    assert out == {"Acme": [{"url": "https://n/1", "relatedness": 0.8,
                             "body_chars": 500}]}


def test_gather_news_relatedness_graceful_on_failure_and_empty():
    from event_intel.events.enrichment import EnrichedExhibitor
    from event_intel.events.news_body import gather_news_relatedness

    class _Boom:
        def embed(self, texts):
            raise RuntimeError("model missing")

    bodied = EnrichedExhibitor(
        name="A", source_snippet="s",
        news_signals=[NewsSignal(title="t", url="u", snippet="s", body_sha="x")],
    )
    assert gather_news_relatedness(
        rows=[bodied], body_loader=lambda u: "body",
        collection="product_wsx",
        embedding_provider=_Boom(), vectorstore_provider=_FakeVectorStore([]),
    ) == {}
    assert gather_news_relatedness(
        rows=[], body_loader=lambda u: None, collection="product_wsx",
        embedding_provider=_FakeEmbedding(),
        vectorstore_provider=_FakeVectorStore([]),
    ) == {}


# ---------- B2 (3) report wiring: scoring fields untouched (W4 pattern) ----------


def _scored_for_report(name):
    from event_intel.events.enrichment import EnrichedExhibitor
    from event_intel.rag.retriever import FitResult
    from event_intel.scoring.compute import ScoredExhibitor
    from event_intel.scoring.dimensions import DimensionScores

    row = EnrichedExhibitor(
        name=name, source_snippet=f"snippet for {name}",
        official_url=f"https://example.com/{name.lower()}",
        news_signals=[NewsSignal(title=f"{name} news", url="https://n/x",
                                 snippet="n", body_sha="aa", body_chars=900)],
    )
    fit = FitResult(name=name, capability_fit=0.85, top_hits=[],
                    capability_fit_breakdown={"Cap A": 3})
    return ScoredExhibitor(
        name=name, tier="S", final_score=8.0, evidence_floor=2,
        dimensions=DimensionScores(
            capability_fit=0.9, source_confidence=1.0, buying_signal=0.6,
            website_verification=1.0, category_fit=0.5,
            competitor_penalty=0.0, bad_fit_penalty=0.0,
        ),
        weights_used={}, tier_reasons=[], rationale="why", angle="angle",
        row=row, fit=fit,
    )


def _report_summary(*scored):
    from event_intel.scoring.compute import ScoringSummary

    counts = {"S": 0, "A": 0, "B": 0, "C": 0}
    for s in scored:
        counts[s.tier] += 1
    return ScoringSummary(rows=list(scored), tier_counts=counts, rationale_calls=0)


def _report_ctx():
    from event_intel.report.tier_list_md import ReportContext

    return ReportContext(workspace_id="acme", event_name="Expo",
                         event_slug="expo", lang="en", generated_at=NOW)


_SCORE_KEYS = (
    "tier", "final_score", "evidence_floor", "capability_fit",
    "capability_fit_breakdown", "rationale", "angle", "evidence",
    "official_url", "news_count", "source_snippet", "source_provenance",
)


def test_news_relatedness_does_not_change_any_scoring_field():
    from event_intel.report.tier_list_yaml import build_tier_list_payload

    summary = _report_summary(_scored_for_report("Acme"), _scored_for_report("Beta"))
    rel = {"Acme": [{"url": "https://n/x", "relatedness": 0.81, "body_chars": 900}]}
    without = build_tier_list_payload(summary=summary, needs_review=[], context=_report_ctx())
    with_rel = build_tier_list_payload(
        summary=summary, needs_review=[], context=_report_ctx(), news_relatedness=rel,
    )
    for a, b in zip(without["exhibitors"], with_rel["exhibitors"], strict=True):
        for k in _SCORE_KEYS:
            assert a[k] == b[k], f"scoring field {k} changed when relatedness attached"
    assert with_rel["exhibitors"][0]["news_relatedness"] == rel["Acme"]
    assert without["exhibitors"][0]["news_relatedness"] == []


def test_md_renders_relatedness_only_for_matched_rows():
    from event_intel.report.tier_list_md import render_tier_list_md

    summary = _report_summary(_scored_for_report("Acme"), _scored_for_report("Beta"))
    rel = {"Acme": [{"url": "https://n/x", "relatedness": 0.81, "body_chars": 900}]}
    md = render_tier_list_md(summary=summary, needs_review=[], context=_report_ctx(),
                             news_relatedness=rel)
    assert md.count("relatedness") == 1  # only Acme's row carries the line
    assert "0.81" in md
