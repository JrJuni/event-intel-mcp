"""N3 — Google News RSS keyless news fallback lane + FallbackSearchProvider
composition + factory wiring. No live network (MockTransport / fakes).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from event_intel.errors import ErrorCode, MCPError
from event_intel.providers.search import (
    DdgsSearchProvider,
    FallbackSearchProvider,
    GoogleNewsRssSearchProvider,
    SearchResult,
    make_search_provider,
)

FIXTURE = Path(__file__).parent / "fixtures" / "google_news_rss.xml"


def _rss_provider(handler=None, **kw):
    import httpx

    if handler is None:
        def handler(request):  # noqa: ANN001
            return httpx.Response(
                200, content=FIXTURE.read_bytes(),
                headers={"content-type": "application/xml"},
            )
    kw.setdefault("min_interval_ms", 0)
    kw.setdefault("sleep", lambda s: None)
    return GoogleNewsRssSearchProvider(transport=httpx.MockTransport(handler), **kw)


# ---------- parsing ----------


def test_rss_mapping_title_link_snippet_source_pubdate():
    p = _rss_provider()
    results = p.search("Acme Robotics", kind="news", count=10, days=30, lang="en")
    assert len(results) == 3 and isinstance(results[0], SearchResult)
    first = results[0]
    assert first.title.startswith("Acme Robotics raises $50M")
    assert first.url.startswith("https://news.google.com/rss/articles/")
    assert "Series B" in first.snippet and "<" not in first.snippet  # tags stripped
    assert first.source == "TechCrunch"
    assert first.published_at is not None and first.published_at.year == 2026
    # Unparseable pubDate degrades to None, item still kept.
    assert results[2].published_at is None
    assert p.last_call_degraded is False


def test_rss_count_truncation():
    assert len(_rss_provider().search("q", kind="news", count=2)) == 2


def test_rss_query_params_days_and_korean_locale():
    captured = {}

    def handler(request):
        import httpx

        captured["params"] = dict(request.url.params)
        return httpx.Response(200, content=FIXTURE.read_bytes())

    _rss_provider(handler).search("에이크미", kind="news", count=5, days=30, lang="ko")
    assert captured["params"]["q"] == "에이크미 when:30d"
    assert captured["params"]["hl"] == "ko" and captured["params"]["gl"] == "KR"
    assert captured["params"]["ceid"] == "KR:ko"


def test_rss_web_kind_is_noop():
    p = _rss_provider()
    assert p.search("q", kind="web") == []
    assert p.last_call_degraded is False  # by contract, not a degradation


@pytest.mark.parametrize("status,content", [(503, b""), (200, b"this is not xml <<<")])
def test_rss_failures_degrade_never_raise(status, content):
    import httpx

    p = _rss_provider(lambda req: httpx.Response(status, content=content))
    assert p.search("q", kind="news") == []
    assert p.last_call_degraded is True and p.degraded_queries == 1
    events = p.drain_events()
    assert events[0]["outcome"] == "degraded" and events[0]["provider"] == "google_news_rss"


# ---------- FallbackSearchProvider composition ----------


class _LaneFake:
    """Configurable lane fake honoring the last_call_degraded contract."""

    def __init__(self, *, results=None, degrade=False, signature="lane/v1"):
        self.results = results or []
        self.degrade = degrade
        self.signature = signature
        self.calls: list[tuple] = []
        self.last_call_degraded = False
        self._degraded_queries = 0

    @property
    def cache_signature(self):
        return self.signature

    @property
    def degraded(self):
        return self._degraded_queries > 0

    @property
    def degraded_queries(self):
        return self._degraded_queries

    def search(self, query, *, kind, count=10, days=None, lang="en"):
        self.calls.append((query, kind))
        self.last_call_degraded = self.degrade
        if self.degrade:
            self._degraded_queries += 1
            return []
        return list(self.results)

    def ping(self):
        return {"status": "best_effort", "provider": self.signature}


def _sr(title="t"):
    return SearchResult(title=title, url="https://x/1", snippet="s")


def test_fallback_not_called_when_primary_ok():
    primary = _LaneFake(results=[_sr()])
    fb = _LaneFake(results=[_sr("fb")])
    w = FallbackSearchProvider(primary, fb)
    out = w.search("q", kind="news")
    assert [r.title for r in out] == ["t"]
    assert fb.calls == [] and w.last_call_degraded is False


def test_fallback_rescues_degraded_news_query():
    primary = _LaneFake(degrade=True)
    fb = _LaneFake(results=[_sr("rescued")])
    w = FallbackSearchProvider(primary, fb)
    out = w.search("q", kind="news")
    assert [r.title for r in out] == ["rescued"]
    assert w.last_call_degraded is False  # rescued = a real, cacheable answer


def test_fallback_skipped_for_web_kind():
    primary = _LaneFake(degrade=True)
    fb = _LaneFake(results=[_sr("fb")])
    w = FallbackSearchProvider(primary, fb)
    assert w.search("q", kind="web") == []
    assert fb.calls == []
    assert w.last_call_degraded is True  # web stays degraded (N1 non-stick)


def test_both_lanes_degraded_stays_degraded():
    w = FallbackSearchProvider(_LaneFake(degrade=True), _LaneFake(degrade=True))
    assert w.search("q", kind="news") == []
    assert w.last_call_degraded is True
    assert w.degraded is True and w.degraded_queries == 2


def test_fallback_signature_and_ping_disclose_composition():
    w = FallbackSearchProvider(
        _LaneFake(signature="ddgs/9/x"), _LaneFake(signature="gnrss/v1")
    )
    assert w.cache_signature == "ddgs/9/x+fb=gnrss/v1"
    assert w.ping()["news_fallback"] == "gnrss/v1"


# ---------- supplement mode (#15-1: supply ceiling fix) ----------


def _srs(n, prefix="p"):
    return [SearchResult(title=f"{prefix}{i}", url=f"https://{prefix}.example.com/{i}",
                         snippet="s") for i in range(n)]


def test_supplement_tops_up_thin_news_answer():
    primary = _LaneFake(results=_srs(4, "p"))
    fb = _LaneFake(results=_srs(8, "fb"))
    w = FallbackSearchProvider(primary, fb, supplement_min=10)
    out = w.search("q", kind="news", count=20)
    assert [r.title for r in out[:4]] == ["p0", "p1", "p2", "p3"]  # primary first
    assert len(out) == 12  # 4 primary + 8 RSS extras
    assert w.last_call_degraded is False  # supplemented = real answer
    assert len(fb.calls) == 1


def test_supplement_dedupes_by_canonical_url_and_caps_at_count():
    shared = SearchResult(title="dup", url="https://p.example.com/0", snippet="s")
    primary = _LaneFake(results=_srs(4, "p"))
    fb = _LaneFake(results=[shared] + _srs(30, "fb"))
    w = FallbackSearchProvider(primary, fb, supplement_min=10)
    out = w.search("q", kind="news", count=10)
    assert len(out) == 10  # capped at count
    assert sum(1 for r in out if r.url == "https://p.example.com/0") == 1  # deduped


def test_supplement_not_fired_when_primary_sufficient_or_web_or_disabled():
    primary = _LaneFake(results=_srs(10, "p"))
    fb = _LaneFake(results=_srs(5, "fb"))
    w = FallbackSearchProvider(primary, fb, supplement_min=10)
    assert len(w.search("q", kind="news", count=20)) == 10
    assert fb.calls == []  # >= threshold → no supplement
    w2 = FallbackSearchProvider(_LaneFake(results=_srs(2, "p")),
                                _LaneFake(results=_srs(5, "fb")), supplement_min=0)
    w2.search("q", kind="news", count=20)
    assert w2.fallback.calls == []  # disabled
    w3 = FallbackSearchProvider(_LaneFake(results=_srs(2, "p")),
                                _LaneFake(results=_srs(5, "fb")), supplement_min=10)
    w3.search("q", kind="web", count=20)
    assert w3.fallback.calls == []  # web kind never supplemented


def test_supplement_threshold_bounded_by_count():
    """count=5 with threshold 10 → fire only when fewer than 5 results."""
    primary = _LaneFake(results=_srs(5, "p"))
    fb = _LaneFake(results=_srs(5, "fb"))
    w = FallbackSearchProvider(primary, fb, supplement_min=10)
    w.search("q", kind="news", count=5)
    assert fb.calls == []  # 5 >= min(10, 5) → sufficient


def test_supplement_signature_isolates_cache():
    w0 = FallbackSearchProvider(_LaneFake(signature="d"), _LaneFake(signature="g"))
    w10 = FallbackSearchProvider(_LaneFake(signature="d"), _LaneFake(signature="g"),
                                 supplement_min=10)
    assert w0.cache_signature == "d+fb=g"
    assert w10.cache_signature == "d+fb=g/sup10"


def test_factory_threads_supplement_min():
    p = make_search_provider({"search": {"provider": "ddgs"}})
    assert p.supplement_min == 10  # defaults.yaml-mirrored factory default
    p2 = make_search_provider(
        {"search": {"provider": "ddgs", "news_supplement_min": 0}}
    )
    assert p2.supplement_min == 0


def test_degraded_path_unchanged_replace_not_merge():
    primary = _LaneFake(degrade=True)
    fb = _LaneFake(results=_srs(3, "fb"))
    w = FallbackSearchProvider(primary, fb, supplement_min=10)
    out = w.search("q", kind="news", count=20)
    assert [r.title for r in out] == ["fb0", "fb1", "fb2"]  # replaced, not merged
    assert w.last_call_degraded is False


# ---------- multi-lane pool (#15-1 follow-up) ----------


def test_supplement_pool_fires_next_lane_only_while_short():
    primary = _LaneFake(results=_srs(4, "p"))
    lane1 = _LaneFake(results=_srs(3, "g"))   # brings total to 7 — still short
    lane2 = _LaneFake(results=_srs(9, "b"))   # fires; merge caps at count, not target
    w = FallbackSearchProvider(primary, lane1, supplement_min=10,
                               extra_lanes=[lane2])
    out = w.search("q", kind="news", count=20)
    assert len(out) == 16 and len(lane2.calls) == 1  # 4+3+9, all under count=20
    # sufficient after lane1 → lane2 untouched
    lane1b = _LaneFake(results=_srs(10, "g"))
    lane2b = _LaneFake(results=_srs(9, "b"))
    w2 = FallbackSearchProvider(_LaneFake(results=_srs(4, "p")), lane1b,
                                supplement_min=10, extra_lanes=[lane2b])
    out2 = w2.search("q", kind="news", count=20)
    assert len(out2) == 14 and lane2b.calls == []


def test_degraded_pool_tries_lanes_in_order():
    primary = _LaneFake(degrade=True)
    lane1 = _LaneFake(degrade=True)
    lane2 = _LaneFake(results=_srs(2, "b"))
    w = FallbackSearchProvider(primary, lane1, supplement_min=10,
                               extra_lanes=[lane2])
    out = w.search("q", kind="news", count=20)
    assert [r.title for r in out] == ["b0", "b1"]
    assert w.last_call_degraded is False
    # all lanes degraded → degraded empty
    w2 = FallbackSearchProvider(_LaneFake(degrade=True), _LaneFake(degrade=True),
                                extra_lanes=[_LaneFake(degrade=True)])
    assert w2.search("q", kind="news") == []
    assert w2.last_call_degraded is True


def test_pool_signature_joins_lanes():
    w = FallbackSearchProvider(
        _LaneFake(signature="d"), _LaneFake(signature="g"),
        supplement_min=10, extra_lanes=[_LaneFake(signature="b")],
    )
    assert w.cache_signature == "d+fb=g,b/sup10"


def test_factory_builds_bing_first_google_second_by_default():
    """Bing FIRST: direct publisher URLs feed the body lane; Google wrapper
    URLs are robots-denied for body fetch (30/30 measured 2026-06-11)."""
    p = make_search_provider({"search": {"provider": "ddgs"}})
    assert len(p.lanes) == 2
    # Compare by name, not isinstance: the ddgs cold-import test re-imports the
    # search module, so class identities can differ across test order.
    assert type(p.lanes[0]).__name__ == "BingNewsRssSearchProvider"
    assert type(p.lanes[1]).__name__ == "GoogleNewsRssSearchProvider"
    assert "bingrss/v1" in p.cache_signature
    bad = {"search": {"provider": "ddgs", "news_extra_lanes": "nonsense_rss"}}
    with pytest.raises(MCPError) as ei:
        make_search_provider(bad)
    assert ei.value.error_code == ErrorCode.CONFIG_ERROR


def test_bing_rss_parses_items_and_mkt_param():
    import httpx

    from event_intel.providers.search import BingNewsRssSearchProvider

    captured = {}
    rss = (
        b'<?xml version="1.0"?><rss version="2.0"><channel>'
        b"<item><title>Snowflake expands in Seoul</title>"
        b"<link>https://publisher.example.com/article-1</link>"
        b"<description>Direct publisher link &lt;b&gt;markup&lt;/b&gt;</description>"
        b"<pubDate>Wed, 10 Jun 2026 01:00:00 GMT</pubDate></item>"
        b"<item><title>Second</title><link>https://publisher.example.com/a2</link></item>"
        b"</channel></rss>"
    )

    def handler(request):
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, content=rss)

    p = BingNewsRssSearchProvider(min_interval_ms=0, sleep=lambda s: None,
                                  transport=httpx.MockTransport(handler))
    out = p.search("스노우플레이크", kind="news", count=5, lang="ko")
    assert captured["params"]["format"] == "RSS"
    assert captured["params"]["mkt"] == "ko-KR"
    assert len(out) == 2
    assert out[0].url == "https://publisher.example.com/article-1"  # direct URL
    assert "<" not in out[0].snippet
    assert p.cache_signature == "bingrss/v1"


# ---------- factory ----------


def test_factory_wraps_ddgs_with_rss_fallback_by_default():
    p = make_search_provider({"search": {"provider": "ddgs"}})
    assert isinstance(p, FallbackSearchProvider)
    assert isinstance(p.primary, DdgsSearchProvider)
    assert type(p.fallback).__name__ == "BingNewsRssSearchProvider"
    assert "+fb=bingrss/v1" in p.cache_signature


def test_factory_news_fallback_none_returns_bare_ddgs():
    p = make_search_provider({"search": {"provider": "ddgs", "news_fallback": "none"}})
    assert isinstance(p, DdgsSearchProvider)


def test_factory_invalid_news_fallback_is_config_error():
    with pytest.raises(MCPError) as ei:
        make_search_provider(
            {"search": {"provider": "ddgs", "news_fallback": "bing_rss"}}
        )
    assert ei.value.error_code == ErrorCode.CONFIG_ERROR


def test_factory_never_wraps_brave(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    p = make_search_provider({"search": {"provider": "brave"}})
    assert not isinstance(p, FallbackSearchProvider)


# ---------- enrichment integration ----------


def test_enrichment_caches_fallback_answer_and_row_not_degraded(tmp_path):
    from event_intel.events.enrichment import enrich_exhibitors
    from event_intel.events.extraction import ExhibitorCandidate

    primary = _LaneFake(degrade=True)
    fb = _LaneFake(results=[SearchResult(
        title="Mobius Labs ships compiler", url="https://news.x/1", snippet="npu",
    )])
    w = FallbackSearchProvider(primary, fb)
    cfg = {"enrichment": {
        "max_companies": 30, "count_web": 5, "count_news": 5,
        "news_days_back": 180, "cache_enabled": True,
        "official_url_levenshtein_threshold": 0.4,
    }}
    result = enrich_exhibitors(
        candidates=[ExhibitorCandidate(
            name="Mobius Labs", source_snippet="x" * 30,
            url="https://mobius.example.com",  # skip the web lane
        )],
        workspace_id="n3ws", lang="en", config=cfg,
        search_provider=w,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    row = result.rows[0]
    assert row.degraded is False           # rescued answer is final
    assert len(row.news_signals) == 1
    persisted = [
        json.loads(ln)
        for ln in (tmp_path / "r.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert persisted[0]["degraded"] is False
    # The rescued answer was cached under the WRAPPER signature.
    assert len(list((tmp_path / "c").glob("*.json"))) == 1
