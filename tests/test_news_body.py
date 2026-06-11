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
