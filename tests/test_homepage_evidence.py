"""#16 S4 — homepage-crawl evidence lane. No live network (robots patched,
fetch faked; trafilatura runs on synthetic HTML like test_news_body).

Adversarial set: happy path 2-component floor / robots deny → zero fetch /
press 404 → identity-only + negative verdict cached / thin press → no activity /
subpage cap / third-party press link not followed / parked homepage / thin
homepage still discovers press / fetch raise → never raises / transient 500
retried once vs 4xx not retried / cache reuse + TTL expiry / press_page floor
semantics + merge precedence units / cold import.
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from event_intel.events import evidence as _evidence
from event_intel.events.homepage_evidence import (
    HomepageCrawlConfig,
    HomepageCrawler,
)
from event_intel.runtime.failure_log import FailureLog

NOW = datetime(2026, 6, 12, 12, 0, 0, tzinfo=UTC)

HOME = "https://acme.com/"


@pytest.fixture(autouse=True)
def _robots_allow(monkeypatch):
    """Default: robots allows everything and makes NO network call."""
    monkeypatch.setattr(
        "event_intel.acquisition.robots.is_allowed",
        lambda url, *, user_agent="event-intel-mcp": True,
    )


def _page(*, paragraphs: int = 10, links: tuple[str, ...] = ()) -> str:
    anchors = "".join(f'<a class="nav" href="{h}">more</a>' for h in links)
    body = "".join(
        f"<p>Paragraph {i}: Acme Robotics builds on-device NPU compiler "
        "toolchains for edge AI workloads, targeting automotive customers.</p>"
        for i in range(paragraphs)
    )
    return (
        f"<html><body><nav>{anchors}</nav>"
        f"<article><h1>Acme Robotics</h1>{body}</article></body></html>"
    )


def _thin(links: tuple[str, ...] = ()) -> str:
    anchors = "".join(f'<a href="{h}">more</a>' for h in links)
    return f"<html><body><nav>{anchors}</nav><p>Loading application…</p></body></html>"


PARKED_HTML = (
    "<html><body><p>This domain is for sale. Contact our broker today "
    "to make an offer on this premium name.</p></body></html>"
)


class FakeFetch:
    """url → response dict (or Exception to raise). Unknown url → 404."""

    def __init__(self, pages: dict):
        self.pages = pages
        self.calls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.calls.append(url)
        r = self.pages.get(url)
        if r is None:
            return {"status": 404, "text": None, "error": "HTTP 404"}
        if isinstance(r, Exception):
            raise r
        return r


def _ok(html: str, url: str) -> dict:
    return {"status": 200, "text": html, "final_url": url}


def _crawler(tmp_path, fetch, *, cfg=None, now=NOW, failure_log=None, sleep=None):
    return HomepageCrawler(
        cfg=cfg or HomepageCrawlConfig(),
        cache_dir=tmp_path / "homepage",
        now=now,
        fetch_fn=fetch,
        failure_log=failure_log,
        sleep=sleep or (lambda s: None),
    )


def _row(evidence):
    return SimpleNamespace(evidence=evidence, official_url=HOME, news_signals=[])


# ---------- happy path ----------


def test_homepage_plus_press_pages_two_floor_components(tmp_path):
    fetch = FakeFetch({
        HOME: _ok(_page(links=("/news/", "/press/releases")), HOME),
        "https://acme.com/news/": _ok(_page(), "https://acme.com/news/"),
        "https://acme.com/press/releases": _ok(_page(), "https://acme.com/press/releases"),
    })
    r = _crawler(tmp_path, fetch).crawl(HOME)
    assert [e.type for e in r.evidence] == [
        _evidence.OFFICIAL_URL, _evidence.PRESS_PAGE, _evidence.PRESS_PAGE,
    ]
    assert {e.source_domain for e in r.evidence} == {"acme.com"}
    assert r.excerpt and "NPU compiler" in r.excerpt
    assert r.pages_fetched == 3
    assert _evidence.floor_components(_row(r.evidence)) == (True, True)


def test_excerpt_is_capped(tmp_path):
    fetch = FakeFetch({HOME: _ok(_page(), HOME)})
    cfg = HomepageCrawlConfig(excerpt_max_chars=50)
    r = _crawler(tmp_path, fetch, cfg=cfg).crawl(HOME)
    assert r.excerpt is not None and len(r.excerpt) == 50


def test_subdomain_press_link_followed_and_fragment_stripped_dedup(tmp_path):
    investor = "https://investor.acme.com/press"
    fetch = FakeFetch({
        HOME: _ok(_page(links=(investor, "/news/", "/news/#latest")), HOME),
        investor: _ok(_page(), investor),
        "https://acme.com/news/": _ok(_page(), "https://acme.com/news/"),
        "https://acme.com/news/#latest": _ok(_page(), "https://acme.com/news/"),
    })
    r = _crawler(tmp_path, fetch).crawl(HOME)
    press = [e for e in r.evidence if e.type == _evidence.PRESS_PAGE]
    # "#latest" variant dedupes onto /news/ — 2 distinct press pages, not 3.
    assert [e.url for e in press] == [investor, "https://acme.com/news/"]
    assert press[0].source_domain == "investor.acme.com"
    assert r.pages_fetched == 3


# ---------- degrade ladder ----------


def test_robots_denied_homepage_zero_evidence_zero_fetch(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "event_intel.acquisition.robots.is_allowed",
        lambda url, *, user_agent="event-intel-mcp": False,
    )
    flog_path = tmp_path / "fetch.jsonl"
    fetch = FakeFetch({HOME: _ok(_page(), HOME)})
    r = _crawler(tmp_path, fetch, failure_log=FailureLog(flog_path)).crawl(HOME)
    assert r.evidence == [] and r.excerpt is None
    assert fetch.calls == []  # robots gate sits BEFORE the fetch
    assert any("unreachable/denied" in w for w in r.warnings)
    row = json.loads(flog_path.read_text(encoding="utf-8").splitlines()[0])
    assert row["lane"] == "homepage" and row["outcome"] == "robots_denied"
    # Transient verdict → nothing cached.
    assert not list((tmp_path / "homepage").glob("*.json"))


def test_press_404_identity_only_and_refusal_cached(tmp_path):
    fetch = FakeFetch({HOME: _ok(_page(links=("/news/",)), HOME)})  # /news/ → 404
    r = _crawler(tmp_path, fetch).crawl(HOME)
    assert [e.type for e in r.evidence] == [_evidence.OFFICIAL_URL]
    assert any("press page" in w for w in r.warnings)
    assert _evidence.floor_components(_row(r.evidence)) == (True, False)

    # 404 is deterministic → cached; a rerun fetches NOTHING.
    fetch2 = FakeFetch({})
    r2 = _crawler(tmp_path, fetch2).crawl(HOME)
    assert fetch2.calls == []
    assert [e.type for e in r2.evidence] == [_evidence.OFFICIAL_URL]


def test_thin_press_body_no_activity(tmp_path):
    fetch = FakeFetch({
        HOME: _ok(_page(links=("/news/",)), HOME),
        "https://acme.com/news/": _ok(_thin(), "https://acme.com/news/"),
    })
    r = _crawler(tmp_path, fetch).crawl(HOME)
    assert [e.type for e in r.evidence] == [_evidence.OFFICIAL_URL]


def test_subpage_cap_fetches_first_three_only(tmp_path):
    links = ("/news/", "/press/", "/newsroom/", "/media/")
    pages = {HOME: _ok(_page(links=links), HOME)}
    for path in links:
        url = f"https://acme.com{path}"
        pages[url] = _ok(_page(), url)
    fetch = FakeFetch(pages)
    r = _crawler(tmp_path, fetch).crawl(HOME)
    assert len(fetch.calls) == 1 + 3
    assert "https://acme.com/media/" not in fetch.calls
    assert sum(1 for e in r.evidence if e.type == _evidence.PRESS_PAGE) == 3
    assert any("max_subpages cap" in w for w in r.warnings)


def test_third_party_press_link_not_followed(tmp_path):
    fetch = FakeFetch({
        HOME: _ok(_page(links=("https://other.com/press", "/about")), HOME),
    })
    r = _crawler(tmp_path, fetch).crawl(HOME)
    assert fetch.calls == [HOME]  # neither third-party nor non-press followed
    assert [e.type for e in r.evidence] == [_evidence.OFFICIAL_URL]


def test_parked_homepage_no_identity_no_link_following(tmp_path):
    fetch = FakeFetch({HOME: {"status": 200, "text": PARKED_HTML, "final_url": HOME}})
    r = _crawler(tmp_path, fetch).crawl(HOME)
    assert r.evidence == [] and r.excerpt is None
    assert any("parked" in w for w in r.warnings)
    assert fetch.calls == [HOME]


def test_thin_homepage_still_discovers_press(tmp_path):
    # JS-heavy shell: thin trafilatura body, but raw HTML carries the nav link.
    fetch = FakeFetch({
        HOME: _ok(_thin(links=("/news/",)), HOME),
        "https://acme.com/news/": _ok(_page(), "https://acme.com/news/"),
    })
    r = _crawler(tmp_path, fetch).crawl(HOME)
    assert [e.type for e in r.evidence] == [_evidence.PRESS_PAGE]
    assert r.excerpt is None
    assert _evidence.floor_components(_row(r.evidence)) == (False, True)


# ---------- robustness / retry ----------


def test_fetch_fn_raising_never_raises(tmp_path):
    sleeps: list[float] = []
    fetch = FakeFetch({HOME: RuntimeError("boom")})
    r = _crawler(tmp_path, fetch, sleep=sleeps.append).crawl(HOME)
    assert r.evidence == [] and r.pages_fetched == 0
    # Exception = transient shape → exactly ONE retry (R3), then give up.
    assert len(fetch.calls) == 2 and sleeps == [2.0]
    assert not list((tmp_path / "homepage").glob("*.json"))  # not cached


def test_transient_500_retried_once_then_success(tmp_path):
    state = {"n": 0}

    def flaky(url):
        state["n"] += 1
        if state["n"] == 1:
            return {"status": 503, "text": None, "error": "HTTP 503"}
        return _ok(_page(), url)

    sleeps: list[float] = []
    r = _crawler(tmp_path, flaky, sleep=sleeps.append).crawl(HOME)
    assert [e.type for e in r.evidence] == [_evidence.OFFICIAL_URL]
    assert state["n"] == 2 and sleeps == [2.0]


def test_deterministic_404_homepage_not_retried(tmp_path):
    sleeps: list[float] = []
    fetch = FakeFetch({})  # everything 404s
    r = _crawler(tmp_path, fetch, sleep=sleeps.append).crawl(HOME)
    assert r.evidence == [] and sleeps == [] and len(fetch.calls) == 1
    assert any("refused" in w for w in r.warnings)
    # Refusal cached → rerun fetches nothing.
    fetch2 = FakeFetch({})
    r2 = _crawler(tmp_path, fetch2).crawl(HOME)
    assert fetch2.calls == [] and r2.evidence == []


def test_cache_reuse_and_ttl_expiry(tmp_path):
    pages = {
        HOME: _ok(_page(links=("/news/",)), HOME),
        "https://acme.com/news/": _ok(_page(), "https://acme.com/news/"),
    }
    r1 = _crawler(tmp_path, FakeFetch(pages)).crawl(HOME)
    assert r1.pages_fetched == 2

    def _boom(url):
        raise AssertionError("must be served from cache")

    r2 = _crawler(tmp_path, _boom).crawl(HOME)
    assert r2.pages_fetched == 0
    assert [e.type for e in r2.evidence] == [e.type for e in r1.evidence]
    assert r2.excerpt == r1.excerpt

    # 15 days later with ttl 14 → stale → live refetch.
    fetch3 = FakeFetch(pages)
    r3 = _crawler(tmp_path, fetch3, now=NOW + timedelta(days=15)).crawl(HOME)
    assert r3.pages_fetched == 2 and len(fetch3.calls) == 2


def test_config_from_dict_roundtrip_and_null_ttl():
    cfg = HomepageCrawlConfig.from_dict(
        {"enabled": False, "max_subpages": 5, "cache_ttl_days": None,
         "min_body_chars": 50}
    )
    assert cfg.enabled is False and cfg.max_subpages == 5
    assert cfg.cache_ttl_days is None and cfg.min_body_chars == 50
    assert HomepageCrawlConfig.from_dict({}).cache_ttl_days == 14


# ---------- press_page floor semantics (units) ----------


def test_press_page_same_site_is_activity_not_identity():
    item = _evidence.EvidenceItem(
        type=_evidence.PRESS_PAGE, url="https://acme.com/news/",
        source_domain="acme.com",
    )
    assert _evidence._is_activity(item, official_domain="acme.com") is True
    assert _evidence._is_identity(item, official_domain="acme.com") is False
    # press_page ALONE: activity without identity → (False, True).
    assert _evidence.floor_components(_row([item])) == (False, True)


def test_press_page_third_party_is_defensively_nothing():
    item = _evidence.EvidenceItem(
        type=_evidence.PRESS_PAGE, url="https://other.com/press",
        source_domain="other.com",
    )
    assert _evidence._is_activity(item, official_domain="acme.com") is False
    assert _evidence._is_identity(item, official_domain="acme.com") is False


def test_merge_evidence_press_page_beats_official_url_loses_to_press_release():
    url = "https://acme.com/news/"
    merged = _evidence.merge_evidence([
        _evidence.EvidenceItem(type=_evidence.OFFICIAL_URL, url=url),
        _evidence.EvidenceItem(type=_evidence.PRESS_PAGE, url=url),
    ])
    assert [e.type for e in merged] == [_evidence.PRESS_PAGE]
    merged2 = _evidence.merge_evidence([
        _evidence.EvidenceItem(type=_evidence.PRESS_PAGE, url=url),
        _evidence.EvidenceItem(type=_evidence.PRESS_RELEASE, url=url),
    ])
    assert [e.type for e in merged2] == [_evidence.PRESS_RELEASE]


def test_classify_url_type_never_emits_press_page():
    # Path-based classification of search results is unaffected by the new type.
    assert _evidence.classify_url_type("https://x.com/press/a") == _evidence.PRESS_RELEASE
    assert _evidence.PRESS_PAGE not in {
        _evidence.classify_url_type(u)
        for u in ("https://x.com/news/", "https://x.com/newsroom/", "https://x.com/")
    }


# ---------- cold import ----------


def test_homepage_evidence_module_import_stays_cold():
    saved = {
        m: sys.modules[m]
        for m in list(sys.modules)
        if m in ("httpx", "trafilatura")
        or m.startswith(("httpx.", "trafilatura."))
    }
    saved_self = sys.modules.pop("event_intel.events.homepage_evidence", None)
    for m in saved:
        del sys.modules[m]
    try:
        importlib.import_module("event_intel.events.homepage_evidence")
        assert "httpx" not in sys.modules and "trafilatura" not in sys.modules
    finally:
        sys.modules.update(saved)
        if saved_self is not None:
            sys.modules["event_intel.events.homepage_evidence"] = saved_self
