"""DdgsSearchProvider — ZCS S2 (keyless zero-config search).

Covers web/news mapping, region + days→timelimit mapping, count, the
process-wide throttle (fake clock/sleep), exception taxonomy (N2: genuine
no-results vs retry-then-degrade), per-call degraded flag (N1), backend
forwarding, ping best_effort, cache_signature, and lazy ddgs import. No live
network.
"""
from __future__ import annotations

import importlib
import sys

import pytest

from event_intel.providers import search as S
from event_intel.providers.search import DdgsSearchProvider, SearchResult

# ---------- fake ddgs.DDGS ----------


class _FakeDDGS:
    calls: list = []
    backends: list = []  # backend kwarg per call (N2)
    text_raises = 0  # number of leading RatelimitException to raise before success
    news_raises = 0
    error = None  # an exception instance to raise instead (non-ratelimit)

    def __init__(self):
        pass

    def _maybe_raise(self, which):
        from ddgs.exceptions import RatelimitException

        if _FakeDDGS.error is not None:
            raise _FakeDDGS.error
        n = getattr(_FakeDDGS, f"{which}_raises")
        if n > 0:
            setattr(_FakeDDGS, f"{which}_raises", n - 1)
            raise RatelimitException("429")

    def text(self, query, *, region, timelimit, max_results, backend="auto"):
        _FakeDDGS.calls.append(("text", query, region, timelimit, max_results))
        _FakeDDGS.backends.append(backend)
        self._maybe_raise("text")
        return [{"title": "Acme site", "href": "https://acme.example.com", "body": "home"}]

    def news(self, query, *, region, timelimit, max_results, backend="auto"):
        _FakeDDGS.calls.append(("news", query, region, timelimit, max_results))
        _FakeDDGS.backends.append(backend)
        self._maybe_raise("news")
        return [{
            "title": "Acme raises", "url": "https://news.example.com/acme",
            "body": "funding", "date": "2026-06-01T00:00:00+00:00", "source": "TechCrunch",
        }]


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    _FakeDDGS.calls = []
    _FakeDDGS.backends = []
    _FakeDDGS.text_raises = 0
    _FakeDDGS.news_raises = 0
    _FakeDDGS.error = None
    monkeypatch.setattr("ddgs.DDGS", _FakeDDGS)


def _provider(**kw):
    # min_interval_ms=0 disables the throttle delay; noop sleep for backoff.
    kw.setdefault("min_interval_ms", 0)
    kw.setdefault("sleep", lambda s: None)
    return DdgsSearchProvider(**kw)


# ---------- mapping ----------


def test_web_search_maps_href_and_body():
    r = _provider().search("acme", kind="web", count=5, lang="en")
    assert len(r) == 1 and isinstance(r[0], SearchResult)
    assert r[0].url == "https://acme.example.com"
    assert r[0].snippet == "home"
    assert _FakeDDGS.calls[0] == ("text", "acme", "us-en", None, 5)


def test_news_search_maps_url_date_source():
    r = _provider().search("acme", kind="news", count=8, days=30, lang="en")
    assert r[0].url == "https://news.example.com/acme"
    assert r[0].source == "TechCrunch"
    assert r[0].published_at is not None  # parsed from date
    # news with days=30 → timelimit "m", count→max_results
    assert _FakeDDGS.calls[0] == ("news", "acme", "us-en", "m", 8)


@pytest.mark.parametrize("days,expected", [(1, "d"), (7, "w"), (30, "m"), (180, "y")])
def test_days_to_timelimit_buckets(days, expected):
    _provider().search("q", kind="news", days=days)
    assert _FakeDDGS.calls[0][3] == expected


@pytest.mark.parametrize("lang,region", [("en", "us-en"), ("ko", "kr-kr"), ("ja", "jp-jp"), ("xx", "wt-wt")])
def test_lang_to_region_mapping(lang, region):
    _provider().search("q", kind="web", lang=lang)
    assert _FakeDDGS.calls[0][2] == region


# ---------- throttle (fake clock/sleep) ----------


def test_rate_limiter_sleeps_remaining_gap():
    rl = S._RateLimiter()
    clock_t = [1000.0]
    slept: list[float] = []

    def clock():
        return clock_t[0]

    def sleep(s):
        slept.append(s)
        clock_t[0] += s

    rl.wait(1.1, clock=clock, sleep=sleep)  # _last=0, clock huge → no sleep
    assert slept == []
    clock_t[0] = 1000.5
    rl.wait(1.1, clock=clock, sleep=sleep)  # 0.5s elapsed → sleep remaining ~0.6
    assert len(slept) == 1 and abs(slept[0] - 0.6) < 1e-9


def test_rate_limiter_zero_interval_never_sleeps():
    rl = S._RateLimiter()
    slept: list[float] = []
    rl.wait(0, clock=lambda: 0.0, sleep=lambda s: slept.append(s))
    assert slept == []


# ---------- backoff + graceful ----------


def test_backoff_retries_then_succeeds():
    _FakeDDGS.text_raises = 2  # fail twice, succeed on the 3rd
    p = _provider(max_retries=3)
    r = p.search("acme", kind="web")
    assert len(r) == 1  # eventually succeeded
    assert p.degraded is False


def test_rate_limit_exhausted_degrades_to_empty():
    _FakeDDGS.text_raises = 99  # always rate-limited
    p = _provider(max_retries=2)
    r = p.search("acme", kind="web")
    assert r == []  # graceful empty, NOT a raise
    assert p.degraded is True and p.degraded_queries == 1


def test_non_ratelimit_error_retries_then_degrades():
    """N2: timeouts/transport errors no longer propagate — they retry with
    backoff and then degrade to empty (flag + last_error), never abort."""
    from ddgs.exceptions import TimeoutException

    _FakeDDGS.error = TimeoutException("boom")
    p = _provider(max_retries=2)
    r = p.search("acme", kind="web")
    assert r == []
    assert p.degraded is True and p.last_call_degraded is True
    assert "boom" in (p.last_error or "")
    assert len(_FakeDDGS.calls) == 3  # initial + 2 retries


def test_no_results_exception_is_genuine_empty_not_degraded():
    """N2: ddgs raises DDGSException("No results found.") for a zero-hit query —
    that's a real answer: [] immediately, no retries, NOT degraded."""
    from ddgs.exceptions import DDGSException

    _FakeDDGS.error = DDGSException("No results found.")
    p = _provider(max_retries=5)
    r = p.search("acme", kind="web")
    assert r == []
    assert p.degraded is False and p.last_call_degraded is False
    assert len(_FakeDDGS.calls) == 1  # answered on the first attempt — no retry


def test_ddgs_no_results_message_literal_pin():
    """_is_no_results string-matches a ddgs-internal message. Pin the literal in
    the installed package source so a ddgs upgrade that changes it fails loud
    (pyproject pins ddgs<10)."""
    import inspect

    import ddgs.ddgs as ddgs_mod

    assert '"No results found."' in inspect.getsource(ddgs_mod)


def test_backend_forwarded_and_defaults_to_auto():
    _provider().search("acme", kind="web")
    assert _FakeDDGS.backends == ["auto"]
    _FakeDDGS.backends = []
    _provider(backend="duckduckgo,bing").search("acme", kind="news")
    assert _FakeDDGS.backends == ["duckduckgo,bing"]


# ---------- ping / signature ----------


def test_ping_is_best_effort_no_network():
    assert _provider().ping() == {
        "status": "best_effort", "provider": "ddgs", "remaining_quota": None
    }


def test_cache_signature_includes_ddgs_version_and_backend():
    sig = _provider().cache_signature
    assert sig.startswith("ddgs/") and sig.endswith("/region1/b=auto")
    # A configured backend changes the result space → must change the key (N2).
    assert _provider(backend="bing").cache_signature.endswith("/b=bing")


# ---------- lazy import (cold-start) ----------


def test_ddgs_not_imported_at_search_module_load():
    for m in list(sys.modules):
        if m == "ddgs" or m.startswith("ddgs."):
            del sys.modules[m]
    sys.modules.pop("event_intel.providers.search", None)
    importlib.import_module("event_intel.providers.search")
    assert "ddgs" not in sys.modules


def test_last_call_degraded_set_on_degrade_and_reset_on_success():
    """N1: the per-call flag marks a degraded (rate-limit) empty and resets on
    the next successful call on the same instance."""
    _FakeDDGS.text_raises = 99  # always rate-limited
    p = _provider(max_retries=1)
    assert p.search("acme", kind="web") == []
    assert p.last_call_degraded is True

    _FakeDDGS.text_raises = 0  # healthy again
    r = p.search("acme", kind="web")
    assert len(r) == 1
    assert p.last_call_degraded is False


def test_last_call_degraded_false_by_default_and_after_genuine_results():
    p = _provider()
    assert p.last_call_degraded is False
    p.search("acme", kind="news")
    assert p.last_call_degraded is False
