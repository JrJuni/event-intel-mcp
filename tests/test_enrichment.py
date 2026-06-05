"""S4 — enrichment tests with fake search provider.

Covers official URL detection, news collection, per-(query,kind) cache reuse,
resume artifact, max_companies cap, upstream failure surface.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from event_intel.errors import ErrorCode, MCPError
from event_intel.events.enrichment import (
    ENRICH_CACHE_VERSION,
    EnrichedExhibitor,
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
            "brave_count_web": 5,
            "brave_count_news": 5,
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


def test_resume_skips_done_rows_and_only_retries_remaining(tmp_path):
    cands = _candidates_5()
    resume_path = tmp_path / "resume.jsonl"

    # Pre-seed resume with two already-enriched rows.
    with resume_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "name": "Mobius Labs",
            "source_snippet": "from prior run",
            "official_url": "https://prior.example.com",
            "news_signals": [],
            "extraction_confidence": 1.0,
            "enrichment_status": "enriched",
            "enrichment_warnings": [],
            "_cache_version": ENRICH_CACHE_VERSION,
        }) + "\n")
        f.write(json.dumps({
            "name": "NeuroDrive Inc.",
            "source_snippet": "from prior run",
            "official_url": "https://prior2.example.com",
            "news_signals": [],
            "extraction_confidence": 1.0,
            "enrichment_status": "enriched",
            "enrichment_warnings": [],
            "_cache_version": ENRICH_CACHE_VERSION,
        }) + "\n")

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


def test_news_drops_non_article_pages_and_carries_published_at(tmp_path):
    """Utility/non-article news pages (login/docs/privacy) are dropped by path;
    real articles keep their published_at (carried from SearchResult)."""
    from datetime import datetime, timezone

    cands = [ExhibitorCandidate(name="Acme AI", source_snippet="AI agents platform", url="https://acme.example")]
    search = FakeSearch()
    search.news_by_name["Acme AI"] = [
        _SR(title="Acme raises Series B", url="https://news.example.com/acme-series-b",
            snippet="funding", published_at=datetime(2026, 6, 1, tzinfo=timezone.utc)),
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


def test_cache_key_includes_version(monkeypatch):
    """A ENRICH_CACHE_VERSION bump changes the cache key so stale entries
    (e.g. v1's empty news) are never reused."""
    import event_intel.events.enrichment as enr

    k1 = enr._SearchCache._key("Acme AI", "news", "en")
    monkeypatch.setattr(enr, "ENRICH_CACHE_VERSION", ENRICH_CACHE_VERSION + 1)
    k2 = enr._SearchCache._key("Acme AI", "news", "en")
    assert k1 != k2
