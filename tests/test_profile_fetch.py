"""E1 — pre-triage profile-fetch (Tier 1). No live network (robots patched,
fetch faked; trafilatura runs on synthetic HTML like test_homepage_evidence).

Adversarial set: happy path profile_text set / too-short → no profile /
deterministic 404 → refused cached, no re-fetch / transient 500 retried once
then None, NOT cached / fetch_fn raise → never raises / robots deny → zero
fetch + log row / cache reuse + TTL expiry / throttle before live not on cache
hit / profile_max_chars truncation / min_body_chars boundary / fetch_roster
mixed roster stats + in-place mutation + no-url skipped / candidate field
default / config roundtrip / cold import.
"""
from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta

import pytest

from event_intel.events.extraction import ExhibitorCandidate
from event_intel.events.profile_fetch import (
    ProfileFetchConfig,
    ProfileFetcher,
)
from event_intel.runtime.failure_log import FailureLog

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
URL = "https://expo.example/exhibitor/acme"


@pytest.fixture(autouse=True)
def _robots_allow(monkeypatch):
    """Default: robots allows everything and makes NO network call."""
    monkeypatch.setattr(
        "event_intel.acquisition.robots.is_allowed",
        lambda url, *, user_agent="event-intel-mcp": True,
    )


def _page(paragraphs: int = 8) -> str:
    body = "".join(
        f"<p>Paragraph {i}: Acme Robotics builds on-device NPU compiler "
        "toolchains for edge AI workloads, targeting automotive customers.</p>"
        for i in range(paragraphs)
    )
    return f"<html><body><article><h1>Acme Robotics</h1>{body}</article></body></html>"


def _ok(html: str, url: str) -> dict:
    return {"status": 200, "text": html, "final_url": url}


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


def _fetcher(tmp_path, fetch, *, cfg=None, now=NOW, failure_log=None, sleep=None):
    return ProfileFetcher(
        cfg=cfg or ProfileFetchConfig(),
        cache_dir=tmp_path / "profile",
        now=now,
        fetch_fn=fetch,
        failure_log=failure_log,
        sleep=sleep or (lambda s: None),
    )


# ---------- happy path ----------


def test_ok_page_yields_profile_text(tmp_path):
    f = ProfileFetcher(
        cfg=ProfileFetchConfig(), cache_dir=tmp_path / "p", now=NOW,
        fetch_fn=FakeFetch({URL: _ok(_page(), URL)}), sleep=lambda s: None,
    )
    text = f.fetch_one(URL)
    assert text and "Acme Robotics" in text
    assert f.pages_fetched == 1


def test_profile_text_truncated_to_max_chars(tmp_path):
    cfg = ProfileFetchConfig(profile_max_chars=50)
    f = _fetcher(tmp_path, FakeFetch({URL: _ok(_page(20), URL)}), cfg=cfg)
    text = f.fetch_one(URL)
    assert text is not None and len(text) == 50


def test_min_body_chars_boundary(tmp_path):
    # Body just under the floor → too_short → no profile.
    short_html = "<html><body><article><p>Tiny stub.</p></article></body></html>"
    cfg = ProfileFetchConfig(min_body_chars=500)
    f = _fetcher(tmp_path, FakeFetch({URL: _ok(short_html, URL)}), cfg=cfg)
    assert f.fetch_one(URL) is None


def test_empty_extract_is_no_profile(tmp_path):
    # trafilatura finds no article body → too_short → None.
    bare = "<html><head><title>x</title></head><body></body></html>"
    f = _fetcher(tmp_path, FakeFetch({URL: _ok(bare, URL)}))
    assert f.fetch_one(URL) is None


# ---------- robustness / retry ----------


def test_fetch_fn_raising_never_raises(tmp_path):
    sleeps: list[float] = []
    fetch = FakeFetch({URL: RuntimeError("boom")})
    f = _fetcher(tmp_path, fetch, sleep=sleeps.append)
    assert f.fetch_one(URL) is None and f.pages_fetched == 0
    # throttle (0.5) once before the loop + ONE retry pause (2.0) on the
    # transient shape; two fetch attempts.
    assert len(fetch.calls) == 2 and sleeps == [0.5, 2.0]
    assert not list((tmp_path / "profile").glob("*.json"))  # transient not cached


def test_transient_500_retried_once_then_success(tmp_path):
    state = {"n": 0}

    def flaky(url):
        state["n"] += 1
        if state["n"] == 1:
            return {"status": 503, "text": None, "error": "HTTP 503"}
        return _ok(_page(), url)

    sleeps: list[float] = []
    f = _fetcher(tmp_path, flaky, sleep=sleeps.append)
    assert f.fetch_one(URL)
    assert state["n"] == 2 and sleeps == [0.5, 2.0]


def test_deterministic_404_refused_cached_no_refetch(tmp_path):
    sleeps: list[float] = []
    fetch = FakeFetch({})  # everything 404s
    f = _fetcher(tmp_path, fetch, sleep=sleeps.append)
    assert f.fetch_one(URL) is None
    assert len(fetch.calls) == 1  # 4xx not retried
    assert sleeps == [0.5]        # throttled once, no retry pause
    # Refusal cached → rerun fetches nothing.
    fetch2 = FakeFetch({})
    f2 = _fetcher(tmp_path, fetch2)
    assert f2.fetch_one(URL) is None and fetch2.calls == []


def test_robots_denied_zero_fetch_and_logged(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "event_intel.acquisition.robots.is_allowed",
        lambda url, *, user_agent="event-intel-mcp": False,
    )
    flog = tmp_path / "fail.jsonl"
    fetch = FakeFetch({URL: _ok(_page(), URL)})
    f = _fetcher(tmp_path, fetch, failure_log=FailureLog(flog))
    assert f.fetch_one(URL) is None
    assert fetch.calls == []  # robots gate sits BEFORE the fetch
    rows = [__import__("json").loads(line) for line in flog.read_text().splitlines()]
    assert rows and rows[0]["lane"] == "profile" and rows[0]["outcome"] == "robots_denied"


def test_throttle_before_live_not_on_cache_hit(tmp_path):
    sleeps: list[float] = []
    f = _fetcher(tmp_path, FakeFetch({URL: _ok(_page(), URL)}), sleep=sleeps.append)
    assert f.fetch_one(URL)
    assert sleeps == [0.5]  # one throttle before the live fetch
    # Served from cache → no throttle, no live fetch.
    sleeps.clear()
    f2 = _fetcher(tmp_path, FakeFetch({}), sleep=sleeps.append)
    assert f2.fetch_one(URL) and sleeps == [] and f2.pages_fetched == 0


def test_cache_reuse_and_ttl_expiry(tmp_path):
    f1 = _fetcher(tmp_path, FakeFetch({URL: _ok(_page(), URL)}))
    assert f1.fetch_one(URL) and f1.pages_fetched == 1

    def _boom(url):
        raise AssertionError("must be served from cache")

    f2 = _fetcher(tmp_path, _boom)
    assert f2.fetch_one(URL) and f2.pages_fetched == 0

    # 15 days later with ttl 14 → stale → live refetch.
    fetch3 = FakeFetch({URL: _ok(_page(), URL)})
    f3 = _fetcher(tmp_path, fetch3, now=NOW + timedelta(days=15))
    assert f3.fetch_one(URL) and f3.pages_fetched == 1 and len(fetch3.calls) == 1


# ---------- fetch_roster ----------


def test_fetch_roster_mixed_in_place_mutation_and_stats(tmp_path):
    cands = [
        ExhibitorCandidate(name="Acme", source_snippet="CSV row 0", url=URL),
        ExhibitorCandidate(name="NoUrl", source_snippet="CSV row 1", url=None),
        ExhibitorCandidate(
            name="Gone", source_snippet="CSV row 2",
            url="https://expo.example/exhibitor/gone",
        ),
    ]
    fetch = FakeFetch({URL: _ok(_page(), URL)})  # 'gone' → 404
    res = _fetcher(tmp_path, fetch).fetch_roster(cands)
    assert res.n_total == 3 and res.n_with_url == 2
    assert res.n_profiled == 1 and res.n_empty == 1
    # In-place mutation: only the reachable one got profile_text.
    assert cands[0].profile_text and "Acme Robotics" in cands[0].profile_text
    assert cands[1].profile_text is None and cands[2].profile_text is None
    assert any("1/2 exhibitors profiled" in w for w in res.warnings)


def test_fetch_roster_never_raises_on_fetch_error(tmp_path):
    cands = [ExhibitorCandidate(name="Acme", source_snippet="s", url=URL)]
    res = _fetcher(tmp_path, FakeFetch({URL: RuntimeError("boom")})).fetch_roster(cands)
    assert res.n_profiled == 0 and res.n_empty == 1
    assert cands[0].profile_text is None


def test_fetch_roster_empty_when_no_urls(tmp_path):
    cands = [ExhibitorCandidate(name="A", source_snippet="s", url=None)]
    res = _fetcher(tmp_path, FakeFetch({})).fetch_roster(cands)
    assert res.n_total == 1 and res.n_with_url == 0
    assert res.warnings == []  # nothing to say when no roster carried a URL


# ---------- field / config / cold import ----------


def test_candidate_profile_text_defaults_none():
    c = ExhibitorCandidate(name="A", source_snippet="s")
    assert c.profile_text is None


def test_config_from_dict_roundtrip_and_null_ttl():
    cfg = ProfileFetchConfig.from_dict(
        {"enabled": False, "profile_max_chars": 300, "cache_ttl_days": None,
         "min_body_chars": 50, "throttle_s": 0.0}
    )
    assert cfg.enabled is False and cfg.profile_max_chars == 300
    assert cfg.cache_ttl_days is None and cfg.min_body_chars == 50
    assert cfg.throttle_s == 0.0
    assert ProfileFetchConfig.from_dict({}).cache_ttl_days == 14


def test_profile_fetch_module_import_stays_cold():
    saved = {
        m: sys.modules[m]
        for m in list(sys.modules)
        if m in ("httpx", "trafilatura")
        or m.startswith(("httpx.", "trafilatura."))
    }
    saved_self = sys.modules.pop("event_intel.events.profile_fetch", None)
    for m in saved:
        del sys.modules[m]
    try:
        importlib.import_module("event_intel.events.profile_fetch")
        assert "httpx" not in sys.modules and "trafilatura" not in sys.modules
    finally:
        sys.modules.update(saved)
        if saved_self is not None:
            sys.modules["event_intel.events.profile_fetch"] = saved_self
