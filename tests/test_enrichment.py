"""S4 — enrichment tests with fake search provider.

Covers official URL detection, news collection, per-(query,kind) cache reuse,
resume artifact, max_companies cap, upstream failure surface.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from event_intel.errors import ErrorCode, MCPError
from event_intel.events.enrichment import (
    ENRICH_CACHE_VERSION,
    enrich_exhibitors,
)
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
    """Fake search provider. Returns canned results per (query substring, kind)."""

    def __init__(self):
        self.calls: list[dict] = []
        # name fragment → list[_SR] for kind=web
        self.web_by_name: dict[str, list[_SR]] = {}
        # name fragment → list[_SR] for kind=news
        self.news_by_name: dict[str, list[_SR]] = {}
        self.fail_for: set[str] = set()

    def search(self, query, *, kind, count, days=None, lang="en"):
        self.calls.append({"query": query, "kind": kind, "count": count, "days": days, "lang": lang})
        if query in self.fail_for:
            raise RuntimeError(f"brave boom for {query!r}")
        bucket = self.web_by_name if kind == "web" else self.news_by_name
        for fragment, results in bucket.items():
            if fragment in query:
                return list(results)
        return []

    def ping(self):  # pragma: no cover
        return {"status": "ok", "remaining_quota": None}


def _config(**overrides):
    cfg = {
        "enrichment": {
            "max_companies": 30,
            "count_web": 5,
            "count_news": 5,
            "news_days_back": 180,
            "cache_enabled": True,
            "official_url_levenshtein_threshold": 0.4,  # loose enough for fakes
        },
    }
    cfg["enrichment"].update(overrides)
    return cfg


def _candidates_5():
    return [
        ExhibitorCandidate(
            name="Mobius Labs",
            source_snippet="On-device NPU compiler stack for edge AI",
            extraction_confidence=0.9,
        ),
        ExhibitorCandidate(
            name="NeuroDrive Inc.",
            source_snippet="Autonomous driving perception stack with lidar fusion",
            url="https://neurodrive.example.com",
            extraction_confidence=0.85,
        ),
        ExhibitorCandidate(
            name="EdgeVision",
            source_snippet="Computer vision SDK for smart-city traffic cameras",
            extraction_confidence=0.8,
        ),
        ExhibitorCandidate(
            name="Synaptik Robotics",
            source_snippet="Industrial robotic arm control with on-arm SLAM",
            extraction_confidence=0.7,
        ),
        ExhibitorCandidate(
            name="Quanta MedAI",
            source_snippet="On-device ultrasound interpretation tablet",
            extraction_confidence=0.65,
        ),
    ]


def _wire_fake_search() -> FakeSearch:
    s = FakeSearch()
    s.web_by_name["Mobius Labs"] = [
        _SR(title="Mobius Labs — official", url="https://mobiuslabs.example.com", snippet=""),
        _SR(title="LinkedIn", url="https://www.linkedin.com/company/mobius-labs/", snippet=""),
    ]
    s.web_by_name["EdgeVision"] = [
        _SR(title="EdgeVision", url="https://edgevision.example.com", snippet=""),
    ]
    s.web_by_name["Synaptik Robotics"] = [
        _SR(title="Synaptik", url="https://synaptik.example.com", snippet=""),
    ]
    s.web_by_name["Quanta MedAI"] = []  # no result → no official_url
    s.news_by_name["Mobius Labs"] = [
        _SR(title="Mobius Labs raises Series B for NPU compiler", url="https://news.example.com/m1", snippet="..."),
        _SR(title="Mobius Labs partners with auto OEM on ADAS Level 3", url="https://news.example.com/m2", snippet="ADAS milestone"),
    ]
    s.news_by_name["EdgeVision"] = [
        _SR(title="EdgeVision wins smart-city tender", url="https://news.example.com/e1", snippet=""),
    ]
    return s


def test_enrich_5_candidates_happy_path(tmp_path):
    cands = _candidates_5()
    search = _wire_fake_search()
    result = enrich_exhibitors(
        candidates=cands, workspace_id="t1", lang="en", config=_config(),
        search_provider=search,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "resume.jsonl",
    )
    by_name = {r.name: r for r in result.rows}
    # 1) Extraction-supplied URL is trusted directly.
    assert by_name["NeuroDrive Inc."].official_url == "https://neurodrive.example.com"
    # 2) Web search picks a clean host over LinkedIn.
    assert by_name["Mobius Labs"].official_url == "https://mobiuslabs.example.com"
    # 3) EdgeVision picked from single hit.
    assert by_name["EdgeVision"].official_url == "https://edgevision.example.com"
    # 4) Quanta MedAI has no web hit → official_url stays None.
    assert by_name["Quanta MedAI"].official_url is None
    # 5) News signals attached where available.
    assert len(by_name["Mobius Labs"].news_signals) == 2
    assert len(by_name["EdgeVision"].news_signals) == 1
    assert by_name["Quanta MedAI"].news_signals == []
    assert result.skipped_from_resume == 0


def test_extraction_supplied_url_skips_web_search(tmp_path):
    """Per Contract: trust the URL the extractor already had."""
    cands = [ExhibitorCandidate(
        name="NeuroDrive Inc.",
        source_snippet="autonomous driving perception",
        url="https://neurodrive.example.com",
    )]
    search = _wire_fake_search()
    enrich_exhibitors(
        candidates=cands, workspace_id="t2", lang="en", config=_config(),
        search_provider=search,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    web_calls = [c for c in search.calls if c["kind"] == "web"]
    # No web search at all for NeuroDrive since it had a URL.
    assert len(web_calls) == 0


def test_rerun_hits_cache_with_zero_new_search_calls(tmp_path):
    cands = _candidates_5()

    # First run — populates cache.
    s1 = _wire_fake_search()
    enrich_exhibitors(
        candidates=cands, workspace_id="t3", lang="en", config=_config(),
        search_provider=s1,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r1.jsonl",
    )
    first_calls = len(s1.calls)
    assert first_calls > 0

    # Second run — different resume file (so we don't skip via resume), same
    # cache dir. ALL search calls should be served from cache → fake search
    # records zero calls.
    s2 = _wire_fake_search()
    result2 = enrich_exhibitors(
        candidates=cands, workspace_id="t3", lang="en", config=_config(),
        search_provider=s2,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r2.jsonl",
    )
    assert len(s2.calls) == 0, f"expected zero search calls, got {s2.calls}"
    assert result2.cache_hits >= first_calls
    assert result2.cache_misses == 0


def _seed_resume_row(cand, config, *, official_url):
    """Build a resume row whose input_fp matches what enrich would compute for
    `cand` under `config` — so the fp gate (P2-1) treats it as reusable."""
    from event_intel.events.enrichment import _config_fingerprint, _input_fingerprint

    config_fp = _config_fingerprint(config["enrichment"])
    return {
        "name": cand.name,
        "source_snippet": cand.source_snippet,
        "url": cand.url,
        "official_url": official_url,
        "news_signals": [],
        "extraction_confidence": cand.extraction_confidence,
        "enrichment_status": "enriched",
        "enrichment_warnings": [],
        "_cache_version": ENRICH_CACHE_VERSION,
        "input_fp": _input_fingerprint(
            cand.name, cand.url, cand.source_snippet,
            cand.extraction_confidence, config_fp,
        ),
        "enriched_at": "2026-06-01T00:00:00+00:00",
    }


def test_resume_skips_done_rows_and_only_retries_remaining(tmp_path):
    cands = _candidates_5()
    resume_path = tmp_path / "resume.jsonl"
    cfg = _config()

    # Pre-seed resume with two already-enriched rows (fp matches → reusable).
    with resume_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(_seed_resume_row(cands[0], cfg, official_url="https://prior.example.com")) + "\n")
        f.write(json.dumps(_seed_resume_row(cands[1], cfg, official_url="https://prior2.example.com")) + "\n")

    search = _wire_fake_search()
    result = enrich_exhibitors(
        candidates=cands, workspace_id="t4", lang="en", config=_config(),
        search_provider=search,
        cache_dir=tmp_path / "cache", resume_path=resume_path,
    )
    assert result.skipped_from_resume == 2
    # Pre-seeded values survive verbatim.
    by_name = {r.name: r for r in result.rows}
    assert by_name["Mobius Labs"].official_url == "https://prior.example.com"
    assert by_name["NeuroDrive Inc."].official_url == "https://prior2.example.com"
    # Search only happened for the 3 remaining rows × 2 kinds (web + news).
    # Mobius/Neuro = 0 calls. Other 3 names = some calls.
    names_searched = {c["query"] for c in search.calls}
    assert not any("Mobius Labs" in q for q in names_searched)
    assert not any("NeuroDrive" in q for q in names_searched)


def test_max_companies_cap_applies(tmp_path):
    cands = _candidates_5()
    search = _wire_fake_search()
    cfg = _config(max_companies=2)
    result = enrich_exhibitors(
        candidates=cands, workspace_id="t5", lang="en", config=cfg,
        search_provider=search,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    assert len(result.rows) == 2
    assert any("capped" in w for w in result.warnings), result.warnings


def test_upstream_search_failure_surfaces_as_upstream_error(tmp_path):
    cands = [ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)]
    search = _wire_fake_search()
    # Fail the EXACT web query the enricher will issue.
    search.fail_for.add('"Mobius Labs" official site')
    with pytest.raises(MCPError) as exc_info:
        enrich_exhibitors(
            candidates=cands, workspace_id="t6", lang="en", config=_config(),
            search_provider=search,
            cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
        )
    assert exc_info.value.error_code == ErrorCode.UPSTREAM_ERROR
    assert exc_info.value.retryable is True


def test_official_url_threshold_filters_low_score_hits(tmp_path):
    """If every web hit is a bad host (LinkedIn etc.), official_url stays None
    and a warning is recorded."""
    cands = [ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)]
    search = FakeSearch()
    search.web_by_name["Mobius Labs"] = [
        _SR(title="LinkedIn", url="https://www.linkedin.com/company/mobius-labs/", snippet=""),
        _SR(title="Wikipedia", url="https://en.wikipedia.org/wiki/Mobius_Labs", snippet=""),
    ]
    search.news_by_name["Mobius Labs"] = []
    row = enrich_exhibitors(
        candidates=cands, workspace_id="t7", lang="en", config=_config(),
        search_provider=search,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    ).rows[0]
    assert row.official_url is None
    assert any("official-site" in w for w in row.enrichment_warnings), row.enrichment_warnings


def test_legacy_brave_count_keys_still_work_and_warn(tmp_path):
    """R1#7 back-compat: legacy brave_count_* keys are honored (alias) but emit a
    rename deprecation warning."""
    cfg = {"enrichment": {
        "max_companies": 30, "brave_count_web": 5, "brave_count_news": 5,
        "news_days_back": 180, "official_url_levenshtein_threshold": 0.4,
    }}
    cands = [ExhibitorCandidate(name="Acme AI", source_snippet="x" * 30)]
    result = enrich_exhibitors(
        candidates=cands, workspace_id="tlegacy", lang="en", config=cfg,
        search_provider=FakeSearch(),
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    assert any("legacy" in w and "count_web" in w for w in result.warnings), result.warnings


def test_degraded_search_surfaces_run_warning(tmp_path):
    """Blind review R1#2: a rate-limited (degraded) backend's empty results are
    surfaced as a run-level warning, not silently indistinguishable from genuine
    'no evidence'."""
    class _DegradedSearch(FakeSearch):
        degraded = True
        degraded_queries = 2

    cands = [ExhibitorCandidate(name="Acme AI", source_snippet="x" * 30)]
    result = enrich_exhibitors(
        candidates=cands, workspace_id="tdeg", lang="en", config=_config(),
        search_provider=_DegradedSearch(),
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    assert any("search degraded" in w for w in result.warnings), result.warnings


def test_news_drops_non_article_pages_and_carries_published_at(tmp_path):
    """Utility/non-article news pages (login/docs/privacy) are dropped by path;
    real articles keep their published_at (carried from SearchResult)."""
    from datetime import datetime

    cands = [ExhibitorCandidate(name="Acme AI", source_snippet="AI agents platform", url="https://acme.example")]
    search = FakeSearch()
    search.news_by_name["Acme AI"] = [
        _SR(title="Acme raises Series B", url="https://news.example.com/acme-series-b",
            snippet="funding", published_at=datetime(2026, 6, 1, tzinfo=UTC)),
        _SR(title="Acme privacy policy", url="https://acme.example/privacy", snippet="legal"),
        _SR(title="Acme docs", url="https://acme.example/docs/start", snippet="how-to"),
    ]
    row = enrich_exhibitors(
        candidates=cands, workspace_id="tnews", lang="en", config=_config(),
        search_provider=search,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    ).rows[0]
    # Only the real article survives; privacy + docs are dropped by path.
    assert len(row.news_signals) == 1
    assert row.news_signals[0].title == "Acme raises Series B"
    assert row.news_signals[0].published_at == "2026-06-01T00:00:00+00:00"


def test_cache_key_includes_version(monkeypatch, tmp_path):
    """A ENRICH_CACHE_VERSION bump changes the cache key so stale entries
    (e.g. v1's empty news) are never reused."""
    import event_intel.events.enrichment as enr

    c = enr._SearchCache(tmp_path)
    k1 = c._key("Acme AI", "news", "en")
    monkeypatch.setattr(enr, "ENRICH_CACHE_VERSION", ENRICH_CACHE_VERSION + 1)
    k2 = c._key("Acme AI", "news", "en")
    assert k1 != k2


def test_cache_key_includes_provider_signature(tmp_path):
    """Blind review R1#1: the provider signature is part of the key, so a
    brave-cached result is never served to a different backend."""
    from event_intel.events.enrichment import _SearchCache

    brave = _SearchCache(tmp_path, provider_sig="brave/v1")
    ddgs = _SearchCache(tmp_path, provider_sig="ddgs/9.9.0")
    assert brave._key("acme", "news", "en") != ddgs._key("acme", "news", "en")


def test_typed_evidence_populated_and_deduped(tmp_path):
    """Phase 18V item 1: official_url + news classify into typed evidence; with
    evidence_queries enabled, a press-release page is added and the same URL
    returned by multiple queries dedupes to one item."""
    search = FakeSearch()
    search.web_by_name["Acme Data"] = [
        _SR(title="Acme Data — official", url="https://acmedata.example", snippet=""),
    ]
    # Both the press-release query AND the news query surface the SAME press URL.
    search.web_by_name["Acme Data press release"] = [
        _SR(title="Acme launches v2", url="https://acmedata.example/press/v2", snippet=""),
    ]
    search.news_by_name["Acme Data"] = [
        _SR(title="Acme launches v2", url="https://acmedata.example/press/v2", snippet="launch"),
        _SR(title="Acme on TechBlog", url="https://techblog.example/acme", snippet="profile"),
    ]
    cands = [
        ExhibitorCandidate(
            name="Acme Data",
            source_snippet="Realtime feature store",
            extraction_confidence=0.9,
        )
    ]
    cfg = _config(
        evidence_queries={
            "product": False,
            "partners": False,
            "press_release": True,
            "max_extra_calls_per_event": 10,
        }
    )
    result = enrich_exhibitors(
        candidates=cands, workspace_id="ev1", lang="en", config=cfg,
        search_provider=search,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    row = result.rows[0]
    types = sorted(e.type for e in row.evidence)
    # official homepage → official_url; techblog → news; press path → press_release.
    assert "official_url" in types
    assert "news" in types
    # The /press/v2 URL appears in BOTH press-release web query and news query but
    # is deduped to a single press_release item (path precedence).
    press_items = [e for e in row.evidence if e.url.rstrip("/").endswith("/press/v2")]
    assert len(press_items) == 1
    assert press_items[0].type == "press_release"


def test_evidence_query_budget_caps_extra_api_calls(tmp_path):
    """max_extra_calls_per_event bounds real (cache-miss) extra evidence queries."""
    search = FakeSearch()
    cands = _candidates_5()
    cfg = _config(
        evidence_queries={
            "product": True,
            "partners": True,
            "press_release": True,
            "max_extra_calls_per_event": 2,
        }
    )
    enrich_exhibitors(
        candidates=cands, workspace_id="ev2", lang="en", config=cfg,
        search_provider=search,
        cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    # 5 companies × 3 evidence query types = 15 possible; budget caps extra web
    # queries (beyond the official-site lookups) at 2.
    extra_web = [
        c for c in search.calls
        if c["kind"] == "web" and ("product" in c["query"] or "partners" in c["query"] or "press release" in c["query"])
    ]
    assert len(extra_web) == 2, extra_web


def test_extra_evidence_query_drops_irrelevant_third_party(tmp_path):
    """Review #1: extra evidence-query hits on an unrelated third-party domain
    (no company-name token) are NOT added; same-domain hits ARE."""
    search = FakeSearch()
    # Keyed by a fragment present in the actual (quoted) query string:
    #   official-url query → '"Acme Data" official site'
    #   product query      → '"Acme Data" product'
    search.web_by_name["official site"] = [
        _SR(title="Acme Data — official", url="https://acmedata.example", snippet=""),
    ]
    search.web_by_name["product"] = [
        # own-domain product page → kept
        _SR(title="Acme Data product", url="https://acmedata.example/products/store", snippet=""),
        # unrelated third-party page, no "Acme" token → dropped
        _SR(title="Top 10 databases of 2026", url="https://randomblog.example/products/list", snippet=""),
    ]
    cands = [
        ExhibitorCandidate(name="Acme Data", source_snippet="feature store", extraction_confidence=0.9)
    ]
    cfg = _config(
        evidence_queries={"product": True, "partners": False, "press_release": False,
                          "max_extra_calls_per_event": 10}
    )
    result = enrich_exhibitors(
        candidates=cands, workspace_id="ev3", lang="en", config=cfg,
        search_provider=search, cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    urls = {e.url for e in result.rows[0].evidence}
    assert "https://acmedata.example/products/store" in urls
    assert "https://randomblog.example/products/list" not in urls


def test_evidence_budget_per_company_no_starvation(tmp_path):
    """Review #3: with a per-company cap and no event ceiling, EVERY company gets
    its extra queries — later companies aren't starved by earlier ones."""
    search = FakeSearch()
    cands = _candidates_5()
    cfg = _config(
        evidence_queries={"product": True, "partners": True, "press_release": True,
                          "max_per_company": 3, "max_extra_calls_per_event": 0}
    )
    enrich_exhibitors(
        candidates=cands, workspace_id="evb1", lang="en", config=cfg,
        search_provider=search, cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    extra = [
        c for c in search.calls
        if c["kind"] == "web" and any(s in c["query"] for s in ("product", "partners", "press release"))
    ]
    # 5 companies x 3 query types, none starved.
    assert len(extra) == 15
    assert any("Quanta MedAI" in c["query"] for c in extra)  # the LAST candidate served


def test_evidence_budget_per_company_cap(tmp_path):
    search = FakeSearch()
    cands = _candidates_5()
    cfg = _config(
        evidence_queries={"product": True, "partners": True, "press_release": True,
                          "max_per_company": 2, "max_extra_calls_per_event": 0}
    )
    enrich_exhibitors(
        candidates=cands, workspace_id="evb2", lang="en", config=cfg,
        search_provider=search, cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    extra = [
        c for c in search.calls
        if c["kind"] == "web" and any(s in c["query"] for s in ("product", "partners", "press release"))
    ]
    assert len(extra) == 10  # 5 companies x 2 per company


def test_search_cache_key_includes_count_and_days(tmp_path):
    """Review #4: count + days are part of the cache key — a 30-day news request
    must not be served a cached 180-day payload, nor a count=5 a count=20 one."""
    from event_intel.events.enrichment import _SearchCache

    c = _SearchCache(tmp_path)
    k_base = c._key("acme", "news", "en", 5, 180)
    assert k_base != c._key("acme", "news", "en", 5, 30)
    assert k_base != c._key("acme", "news", "en", 20, 180)
    assert k_base == c._key("acme", "news", "en", 5, 180)


def test_news_gate_drops_offtopic_from_floor_evidence(tmp_path):
    """Review round-2 #1: an off-topic news article (name not mentioned) is still
    collected for the buying signal but must NOT become floor activity evidence,
    so official_url + 1 unrelated article can't reach floor 2."""
    from event_intel.scoring.rules import compute_evidence_floor

    search = FakeSearch()
    search.web_by_name["official site"] = [
        _SR(title="Acme Robotics", url="https://acmerobotics.example", snippet=""),
    ]
    search.news_by_name["Acme Robotics"] = [
        _SR(title="General industry trends 2026", url="https://news.example/x", snippet="market"),
    ]
    cands = [
        ExhibitorCandidate(name="Acme Robotics", source_snippet="robot arms", extraction_confidence=0.9)
    ]
    result = enrich_exhibitors(
        candidates=cands, workspace_id="ng1", lang="en", config=_config(),
        search_provider=search, cache_dir=tmp_path / "c", resume_path=tmp_path / "r.jsonl",
    )
    row = result.rows[0]
    # news_signal still collected (feeds buying_signal), but not floor evidence.
    assert len(row.news_signals) == 1
    ev_types = {e.type for e in row.evidence}
    assert "news" not in ev_types and "official_url" in ev_types
    assert compute_evidence_floor(row) == 1  # identity only, no activity


# ---------- P2-1: cache TTL + input fingerprint + true refresh ----------


_T0 = datetime(2026, 6, 1, tzinfo=UTC)


def _enrich_once(cands, *, cfg, search, cache_dir, resume_path, now=_T0, refresh=False):
    return enrich_exhibitors(
        candidates=cands, workspace_id="p21", lang="en", config=cfg,
        search_provider=search, cache_dir=cache_dir, resume_path=resume_path,
        now=now, refresh=refresh,
    )


def test_cache_ttl_expires_stale_entries(tmp_path):
    """A cached Brave answer is reused within ttl_days but re-fetched once stale —
    the core 'recent-180-days reused months later' bug (review r2 #2)."""
    cand = [ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)]
    cfg = _config(cache_ttl_days=7)
    search = _wire_fake_search()
    cache_dir = tmp_path / "cache"

    _enrich_once(cand, cfg=cfg, search=search, cache_dir=cache_dir,
                 resume_path=tmp_path / "r1.jsonl", now=_T0)
    assert len(search.calls) > 0  # populated cache (web + news)

    # +3 days, fresh resume file (so resume can't skip) → cache fresh → 0 live calls.
    search.calls.clear()
    _enrich_once(cand, cfg=cfg, search=search, cache_dir=cache_dir,
                 resume_path=tmp_path / "r2.jsonl", now=_T0 + timedelta(days=3))
    assert search.calls == []

    # +10 days → cache past TTL → live calls again.
    search.calls.clear()
    _enrich_once(cand, cfg=cfg, search=search, cache_dir=cache_dir,
                 resume_path=tmp_path / "r3.jsonl", now=_T0 + timedelta(days=10))
    assert len(search.calls) > 0


def test_unchanged_input_skips_but_changed_snippet_reenriches(tmp_path):
    """Same name + same inputs → resume skip; a changed snippet busts the
    input_fp so the row re-enriches instead of being skipped forever (r2 #3)."""
    cfg = _config()
    search = _wire_fake_search()
    cache_dir, resume_path = tmp_path / "c", tmp_path / "r.jsonl"

    c1 = [ExhibitorCandidate(name="Mobius Labs", source_snippet="original snippet aaaaa")]
    _enrich_once(c1, cfg=cfg, search=search, cache_dir=cache_dir, resume_path=resume_path)

    # identical inputs → skipped via resume.
    again = _enrich_once(c1, cfg=cfg, search=search, cache_dir=cache_dir, resume_path=resume_path)
    assert again.skipped_from_resume == 1

    # changed snippet → fp mismatch → re-enriched, not skipped.
    c2 = [ExhibitorCandidate(name="Mobius Labs", source_snippet="DIFFERENT snippet bbbbb")]
    changed = _enrich_once(c2, cfg=cfg, search=search, cache_dir=cache_dir, resume_path=resume_path)
    assert changed.skipped_from_resume == 0


def test_config_fingerprint_isolates_enrichment_fields():
    """config_fp hashes ONLY enrichment-affecting fields — a scoring-weight change
    must not invalidate Brave enrichment, but an enrichment field change must."""
    from event_intel.events.enrichment import _config_fingerprint

    base_fp = _config_fingerprint(_config()["enrichment"])
    # Same enrichment fields → same fp (scoring config lives elsewhere entirely).
    assert _config_fingerprint(_config()["enrichment"]) == base_fp
    # An enrichment field change DOES bust it.
    assert _config_fingerprint(_config(news_days_back=30)["enrichment"]) != base_fp


def test_refresh_bypasses_resume_and_cache(tmp_path):
    """--refresh re-fetches every company: no resume skip AND no cache read — a
    real refresh, not just resume bypass (review r2 #3)."""
    cand = [ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)]
    cfg = _config()
    search = _wire_fake_search()
    cache_dir, resume_path = tmp_path / "c", tmp_path / "r.jsonl"

    _enrich_once(cand, cfg=cfg, search=search, cache_dir=cache_dir, resume_path=resume_path)
    first_calls = len(search.calls)
    assert first_calls > 0

    # refresh: same resume + cache, but both bypassed → live calls repeat.
    search.calls.clear()
    res = _enrich_once(cand, cfg=cfg, search=search, cache_dir=cache_dir,
                       resume_path=resume_path, refresh=True)
    assert res.skipped_from_resume == 0
    assert len(search.calls) == first_calls  # cache not read → re-fetched


def test_ttl_days_zero_never_reuses_cache(tmp_path):
    """ttl_days=0 → always stale: cache is never reused even at the same instant."""
    cand = [ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)]
    cfg = _config(cache_ttl_days=0)
    search = _wire_fake_search()
    cache_dir = tmp_path / "c"

    _enrich_once(cand, cfg=cfg, search=search, cache_dir=cache_dir,
                 resume_path=tmp_path / "r1.jsonl", now=_T0)
    search.calls.clear()
    # fresh resume so resume can't skip; ttl=0 → cache stale → live calls again.
    _enrich_once(cand, cfg=cfg, search=search, cache_dir=cache_dir,
                 resume_path=tmp_path / "r2.jsonl", now=_T0)
    assert len(search.calls) > 0


# ---------- P2-2: round-robin evidence allocation + resume durability ----------


def test_round_robin_no_starvation():
    """Event cap 4 across 4 companies → each gets exactly 1 (round-robin), not
    company A grabbing 3 and B getting 1 (review r2 #6)."""
    from event_intel.events.enrichment import allocate_round_robin

    names = ["A", "B", "C", "D"]
    suffixes = ["product", "partners", "press release"]
    alloc = allocate_round_robin(names, suffixes, per_company_cap=3, event_cap=4)
    assert [len(alloc[n]) for n in names] == [1, 1, 1, 1]
    assert sum(len(v) for v in alloc.values()) == 4


def test_round_robin_unlimited_gives_full_allowance():
    """event_cap=0 (default) → every company gets its full per-company allowance,
    equivalent to the pre-P2-2 per-company-only behavior."""
    from event_intel.events.enrichment import allocate_round_robin

    alloc = allocate_round_robin(
        ["A", "B"], ["product", "partners", "press release"],
        per_company_cap=2, event_cap=0,
    )
    assert alloc == {"A": ["product", "partners"], "B": ["product", "partners"]}


def test_round_robin_cap_smaller_than_company_count():
    """event_cap 2 with 4 companies → only the first two (rank-0 pass) get a slot."""
    from event_intel.events.enrichment import allocate_round_robin

    alloc = allocate_round_robin(
        ["A", "B", "C", "D"], ["product", "partners"], per_company_cap=2, event_cap=2,
    )
    assert alloc["A"] == ["product"] and alloc["B"] == ["product"]
    assert alloc["C"] == [] and alloc["D"] == []


def test_resume_durable_when_later_company_fails(tmp_path):
    """P2-2 keeps per-company resume.append immediate: a later company's API error
    never loses an already-completed earlier company (review r2 #4)."""
    resume_path = tmp_path / "r.jsonl"
    search = FakeSearch()
    search.web_by_name["Mobius Labs"] = [
        _SR(title="Mobius", url="https://mobiuslabs.example.com", snippet=""),
    ]
    search.fail_for.add('"NeuroDrive Inc." official site')  # 2nd company blows up
    cands = [
        ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30),
        ExhibitorCandidate(name="NeuroDrive Inc.", source_snippet="y" * 30),
    ]
    with pytest.raises(MCPError):
        enrich_exhibitors(
            candidates=cands, workspace_id="dur", lang="en", config=_config(),
            search_provider=search, cache_dir=tmp_path / "c", resume_path=resume_path,
        )
    lines = [
        json.loads(ln)
        for ln in resume_path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    names = {r["name"] for r in lines}
    assert "Mobius Labs" in names           # finished + durably persisted
    assert "NeuroDrive Inc." not in names   # failed before append
