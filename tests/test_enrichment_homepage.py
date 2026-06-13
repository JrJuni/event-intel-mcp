"""#16 S5 — enrichment wiring for ``evidence_source: homepage``.

Covers: search-budget contract (per-company queries ≤1, 0 when the candidate
already has a URL), news-mode behavior preserved when the key is absent or
pinned, fingerprint isolation between the two lanes, resume round-trip with
homepage evidence, rescue interaction, and the evidence_queries skip.

The crawler is always injected as a fake here — building the real one would
touch ``Path.home()`` and the network. ``tests/test_homepage_evidence.py``
covers the real crawler.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from event_intel.events import evidence as _evidence
from event_intel.events import homepage_evidence as _homepage
from event_intel.events.enrichment import enrich_exhibitors
from event_intel.events.extraction import ExhibitorCandidate


@dataclass
class _SR:
    title: str
    url: str
    snippet: str
    source: str | None = None
    published_at: object = None
    extra: dict | None = None


class FakeSearch:
    """Fake search provider — records calls, returns canned results."""

    def __init__(self):
        self.calls: list[dict] = []
        self.web_by_name: dict[str, list[_SR]] = {}
        self.news_by_name: dict[str, list[_SR]] = {}
        self.fail_for: set[str] = set()

    def search(self, query, *, kind, count, days=None, lang="en"):
        self.calls.append({"query": query, "kind": kind, "count": count,
                           "days": days, "lang": lang})
        if query in self.fail_for:
            raise RuntimeError(f"search boom for {query!r}")
        bucket = self.web_by_name if kind == "web" else self.news_by_name
        for fragment, results in bucket.items():
            if fragment in query:
                return list(results)
        return []

    def ping(self):  # pragma: no cover
        return {"status": "ok", "remaining_quota": None}


class _FakeHomepageCrawler:
    """Records crawl calls; returns a canned HomepageCrawlResult per URL."""

    def __init__(self, results: dict[str, _homepage.HomepageCrawlResult] | None = None):
        self.calls: list[str] = []
        self.results = results or {}

    def crawl(self, official_url: str) -> _homepage.HomepageCrawlResult:
        self.calls.append(official_url)
        return self.results.get(official_url, _homepage.HomepageCrawlResult())


def _crawl_result(url: str, *, press: bool = True, excerpt: str = "We build NPU compilers."):
    """Canned crawl result mirroring the real crawler's ok-path output:
    an official_url identity item (duplicate of enrichment's own — merge must
    dedupe it) + one press_page activity item."""
    ev = [
        _evidence.EvidenceItem(
            type=_evidence.OFFICIAL_URL, url=url,
            source_domain=_evidence.domain_of(url),
        ),
    ]
    if press:
        ev.append(
            _evidence.EvidenceItem(
                type=_evidence.PRESS_PAGE, url=f"{url.rstrip('/')}/news",
                source_domain=_evidence.domain_of(url),
            )
        )
    return _homepage.HomepageCrawlResult(evidence=ev, excerpt=excerpt, pages_fetched=2)


def _config(**overrides):
    cfg = {
        "enrichment": {
            "max_companies": 30,
            "count_web": 5,
            "count_news": 5,
            "news_days_back": 180,
            "cache_enabled": True,
            "official_url_levenshtein_threshold": 0.4,
        },
    }
    cfg["enrichment"].update(overrides)
    return cfg


def _homepage_config(**overrides):
    return _config(evidence_source="homepage", homepage={"enabled": True}, **overrides)


_URL = "https://mobiuslabs.example.com"


def _cand_with_url():
    return ExhibitorCandidate(
        name="Mobius Labs",
        source_snippet="On-device NPU compiler stack for edge AI",
        url=_URL,
        extraction_confidence=0.9,
    )


def _cand_without_url():
    return ExhibitorCandidate(
        name="Mobius Labs",
        source_snippet="On-device NPU compiler stack for edge AI",
        extraction_confidence=0.9,
    )


def _wire_web(search: FakeSearch) -> None:
    search.web_by_name["Mobius Labs"] = [
        _SR(title="Mobius Labs — official", url=_URL, snippet=""),
    ]
    search.news_by_name["Mobius Labs"] = [
        _SR(title="Mobius Labs raises Series B", url="https://news.example.com/m1", snippet="..."),
    ]


# ---------- homepage mode: search budget + evidence ----------


def test_homepage_mode_with_url_zero_search_calls_and_press_evidence(tmp_path):
    """Candidate already has a URL → ZERO search queries; the crawl supplies
    activity (press_page) + the fit excerpt; merge dedupes the duplicate
    official_url identity item."""
    search = FakeSearch()
    crawler = _FakeHomepageCrawler({_URL: _crawl_result(_URL)})
    result = enrich_exhibitors(
        candidates=[_cand_with_url()], workspace_id="hp1", lang="en",
        config=_homepage_config(), search_provider=search,
        homepage_crawler=crawler,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    assert search.calls == []
    assert crawler.calls == [_URL]
    row = result.rows[0]
    assert row.homepage_excerpt == "We build NPU compilers."
    assert row.news_signals == []
    types = [e.type for e in row.evidence]
    assert types.count(_evidence.OFFICIAL_URL) == 1  # crawler duplicate merged
    assert types.count(_evidence.PRESS_PAGE) == 1


def test_homepage_mode_without_url_one_web_query_zero_news(tmp_path):
    """No URL from extraction → exactly ONE web query (official-site pick),
    zero news queries; the crawl runs on the picked URL."""
    search = FakeSearch()
    _wire_web(search)
    crawler = _FakeHomepageCrawler({_URL: _crawl_result(_URL)})
    result = enrich_exhibitors(
        candidates=[_cand_without_url()], workspace_id="hp2", lang="en",
        config=_homepage_config(), search_provider=search,
        homepage_crawler=crawler,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    assert [c["kind"] for c in search.calls] == ["web"]
    assert crawler.calls == [_URL]
    row = result.rows[0]
    assert row.official_url == _URL
    assert any(e.type == _evidence.PRESS_PAGE for e in row.evidence)


def test_homepage_mode_no_official_url_identity_only_no_crawl(tmp_path):
    """Web search finds nothing → no crawl, no evidence — the graceful degrade
    matches the legacy 'news search returned 0' shape."""
    search = FakeSearch()  # empty buckets → no hits
    crawler = _FakeHomepageCrawler()
    result = enrich_exhibitors(
        candidates=[_cand_without_url()], workspace_id="hp3", lang="en",
        config=_homepage_config(), search_provider=search,
        homepage_crawler=crawler,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    assert crawler.calls == []
    row = result.rows[0]
    assert row.official_url is None
    assert row.evidence == []
    assert row.homepage_excerpt is None


def test_homepage_mode_skips_evidence_queries_even_when_enabled(tmp_path):
    """The ×3 evidence suffix queries belong to the news-search lane — homepage
    mode must not issue them even when the config enables all three."""
    search = FakeSearch()
    crawler = _FakeHomepageCrawler({_URL: _crawl_result(_URL)})
    cfg = _homepage_config(evidence_queries={
        "product": True, "partners": True, "press_release": True,
    })
    enrich_exhibitors(
        candidates=[_cand_with_url()], workspace_id="hp4", lang="en",
        config=cfg, search_provider=search,
        homepage_crawler=crawler,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    assert search.calls == []


# ---------- news mode preserved ----------


def test_absent_key_news_mode_never_touches_crawler(tmp_path):
    """No evidence_source key = exact pre-S5 behavior: news search runs, the
    injected crawler is never called, no excerpt."""
    search = FakeSearch()
    _wire_web(search)
    crawler = _FakeHomepageCrawler({_URL: _crawl_result(_URL)})
    result = enrich_exhibitors(
        candidates=[_cand_without_url()], workspace_id="hp5", lang="en",
        config=_config(), search_provider=search,
        homepage_crawler=crawler,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    assert crawler.calls == []
    kinds = sorted(c["kind"] for c in search.calls)
    assert kinds == ["news", "web"]
    row = result.rows[0]
    assert len(row.news_signals) == 1
    assert row.homepage_excerpt is None


def test_unknown_evidence_source_warns_and_falls_back_to_news(tmp_path):
    search = FakeSearch()
    _wire_web(search)
    crawler = _FakeHomepageCrawler({_URL: _crawl_result(_URL)})
    result = enrich_exhibitors(
        candidates=[_cand_without_url()], workspace_id="hp6", lang="en",
        config=_config(evidence_source="rss"), search_provider=search,
        homepage_crawler=crawler,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    assert any("unknown" in w and "rss" in w for w in result.warnings), result.warnings
    assert crawler.calls == []
    assert any(c["kind"] == "news" for c in search.calls)
    assert len(result.rows[0].news_signals) == 1


def test_homepage_mode_disabled_crawl_warns_identity_only(tmp_path):
    """evidence_source=homepage + homepage.enabled=false → news stays off AND
    no crawl runs: identity-only evidence + an explicit run warning."""
    search = FakeSearch()
    result = enrich_exhibitors(
        candidates=[_cand_with_url()], workspace_id="hp7", lang="en",
        config=_config(evidence_source="homepage", homepage={"enabled": False}),
        search_provider=search,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    assert any("homepage.enabled=false" in w for w in result.warnings), result.warnings
    assert search.calls == []  # news skipped, url present → web skipped
    row = result.rows[0]
    types = [e.type for e in row.evidence]
    assert types == [_evidence.OFFICIAL_URL]
    assert row.homepage_excerpt is None


# ---------- fingerprint + resume ----------


def test_news_mode_resume_rows_not_reused_under_homepage_mode(tmp_path):
    """The lane swap changes _config_fingerprint → input_fp mismatch → rows
    enriched under news mode must re-enrich, never be silently reused."""
    resume_path = tmp_path / "resume.jsonl"
    search1 = FakeSearch()
    _wire_web(search1)
    crawler = _FakeHomepageCrawler({_URL: _crawl_result(_URL)})
    enrich_exhibitors(
        candidates=[_cand_without_url()], workspace_id="hp8", lang="en",
        config=_config(), search_provider=search1,
        homepage_crawler=crawler,
        cache_dir=tmp_path / "cache1", resume_path=resume_path,
    )
    assert crawler.calls == []  # sanity: run 1 really was news mode

    search2 = FakeSearch()
    _wire_web(search2)
    result2 = enrich_exhibitors(
        candidates=[_cand_without_url()], workspace_id="hp8", lang="en",
        config=_homepage_config(), search_provider=search2,
        homepage_crawler=crawler,
        cache_dir=tmp_path / "cache2", resume_path=resume_path,
    )
    assert result2.skipped_from_resume == 0
    assert crawler.calls == [_URL]
    assert result2.rows[0].homepage_excerpt == "We build NPU compilers."


def test_homepage_mode_resume_round_trip_preserves_evidence_and_excerpt(tmp_path):
    """Re-run with the same resume artifact → row reused: zero crawls, zero
    searches, press_page evidence + excerpt restored from the JSONL."""
    resume_path = tmp_path / "resume.jsonl"
    crawler1 = _FakeHomepageCrawler({_URL: _crawl_result(_URL)})
    enrich_exhibitors(
        candidates=[_cand_with_url()], workspace_id="hp9", lang="en",
        config=_homepage_config(), search_provider=FakeSearch(),
        homepage_crawler=crawler1,
        cache_dir=tmp_path / "cache", resume_path=resume_path,
    )
    assert crawler1.calls == [_URL]

    search2 = FakeSearch()
    crawler2 = _FakeHomepageCrawler({_URL: _crawl_result(_URL)})
    result2 = enrich_exhibitors(
        candidates=[_cand_with_url()], workspace_id="hp9", lang="en",
        config=_homepage_config(), search_provider=search2,
        homepage_crawler=crawler2,
        cache_dir=tmp_path / "cache", resume_path=resume_path,
    )
    assert result2.skipped_from_resume == 1
    assert crawler2.calls == []
    assert search2.calls == []
    row = result2.rows[0]
    assert row.homepage_excerpt == "We build NPU compilers."
    assert any(e.type == _evidence.PRESS_PAGE for e in row.evidence)
    assert any(e.type == _evidence.OFFICIAL_URL for e in row.evidence)
    # The excerpt survived the JSONL round-trip on disk too.
    raw = resume_path.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(raw[0])["homepage_excerpt"] == "We build NPU compilers."


# ---------- rescue interaction ----------


class _RescueLLM:
    """Proposes one alternate query; enrichment must only re-run the WEB lane
    with it in homepage mode."""

    model = "fake-rescue"

    def chat_once(self, *, system, user, max_tokens, temperature):
        from types import SimpleNamespace

        return SimpleNamespace(text='["Mobius Labs GmbH"]', usage=None)


def test_rescue_in_homepage_mode_retries_web_only_then_crawls(tmp_path):
    """Degraded official-site query → rescue proposes a query → only kind=web
    retried (news lane stays off), recovered URL gets crawled."""
    search = FakeSearch()
    search.fail_for.add('"Mobius Labs" official site')
    search.web_by_name["Mobius Labs GmbH"] = [
        _SR(title="Mobius Labs — official", url=_URL, snippet=""),
    ]
    crawler = _FakeHomepageCrawler({_URL: _crawl_result(_URL)})
    result = enrich_exhibitors(
        candidates=[_cand_without_url()], workspace_id="hp10", lang="en",
        config=_homepage_config(query_rescue={"enabled": True}),
        search_provider=search, homepage_crawler=crawler,
        llm_provider=_RescueLLM(),
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    kinds = [c["kind"] for c in search.calls]
    assert kinds == ["web", "web"]  # original (failed) + rescue retry — no news
    row = result.rows[0]
    assert row.official_url == _URL
    assert crawler.calls == [_URL]
    assert any(e.type == _evidence.PRESS_PAGE for e in row.evidence)
