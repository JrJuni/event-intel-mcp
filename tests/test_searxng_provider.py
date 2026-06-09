"""SearxngSearchProvider — ZCS S3 (self-hosted JSON metasearch, keyless).

Covers request params (format=json/categories/time_range/language), result
mapping, count slicing, tolerant parsing, the 403/non-JSON config failures
(blind review R1#5), and ping status (missing_config / 403 / ok / error). httpx
is faked — no network.
"""
from __future__ import annotations

import pytest

from event_intel.providers.search import SearxngSearchProvider

# ---------- fake httpx ----------


class _FakeResp:
    def __init__(self, *, status=200, json_data=None, bad_json=False):
        self.status_code = status
        self._json = json_data if json_data is not None else {"results": []}
        self._bad_json = bad_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._json


def _patch_httpx(monkeypatch, resp):
    capture: dict = {}

    class _FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            capture["url"] = url
            capture["params"] = params
            return resp

    monkeypatch.setattr("httpx.Client", _FakeClient)
    return capture


def _provider():
    return SearxngSearchProvider(base_url="http://localhost:8888/")


_NEWS_PAYLOAD = {
    "results": [
        {
            "title": "Acme raises",
            "url": "https://news.example.com/acme",
            "content": "funding round",
            "publishedDate": "2026-06-01T00:00:00+00:00",
            "engine": "bing news",
        }
    ]
}
_WEB_PAYLOAD = {
    "results": [
        {"title": "Acme", "url": "https://acme.example.com", "content": "home", "engine": "google"}
    ]
}


# ---------- request params ----------


def test_web_request_params(monkeypatch):
    cap = _patch_httpx(monkeypatch, _FakeResp(json_data=_WEB_PAYLOAD))
    _provider().search("acme", kind="web", count=5, lang="en")
    assert cap["url"] == "http://localhost:8888/search"
    p = cap["params"]
    assert p["format"] == "json" and p["categories"] == "general"
    assert p["language"] == "en" and "time_range" not in p  # web → no days


def test_news_request_params_with_time_range(monkeypatch):
    cap = _patch_httpx(monkeypatch, _FakeResp(json_data=_NEWS_PAYLOAD))
    _provider().search("acme", kind="news", count=8, days=30, lang="ko")
    p = cap["params"]
    assert p["categories"] == "news"
    assert p["time_range"] == "month"
    assert p["language"] == "ko"


@pytest.mark.parametrize("days,rng", [(1, "day"), (7, "week"), (30, "month"), (180, "year")])
def test_time_range_buckets(monkeypatch, days, rng):
    cap = _patch_httpx(monkeypatch, _FakeResp(json_data=_NEWS_PAYLOAD))
    _provider().search("q", kind="news", days=days)
    assert cap["params"]["time_range"] == rng


# ---------- result mapping ----------


def test_news_result_mapping(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(json_data=_NEWS_PAYLOAD))
    r = _provider().search("acme", kind="news")
    assert r[0].url == "https://news.example.com/acme"
    assert r[0].snippet == "funding round"
    assert r[0].source == "bing news"
    assert r[0].published_at is not None


def test_tolerant_parse_of_missing_fields(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(json_data={"results": [{"url": "https://x.com"}]}))
    r = _provider().search("q", kind="web")
    assert r[0].url == "https://x.com"
    assert r[0].title == "" and r[0].snippet == "" and r[0].published_at is None


def test_count_slices_results(monkeypatch):
    many = {"results": [{"url": f"https://x.com/{i}", "title": str(i)} for i in range(20)]}
    _patch_httpx(monkeypatch, _FakeResp(json_data=many))
    r = _provider().search("q", kind="web", count=5)
    assert len(r) == 5


# ---------- 403 / non-JSON config failures (R1#5) ----------


def test_search_403_raises(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(status=403))
    with pytest.raises(RuntimeError) as exc:
        _provider().search("q", kind="web")
    assert "403" in str(exc.value) or "json" in str(exc.value).lower()


def test_search_non_json_raises(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(status=200, bad_json=True))
    with pytest.raises(RuntimeError) as exc:
        _provider().search("q", kind="web")
    assert "json" in str(exc.value).lower()


# ---------- ping ----------


def test_ping_missing_url():
    p = SearxngSearchProvider(base_url="")
    assert p.ping()["status"] == "missing_config"


def test_ping_403_is_missing_config(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(status=403))
    s = _provider().ping()
    assert s["status"] == "missing_config"
    assert "json" in s["fix"].lower()


def test_ping_ok_when_json_returned(monkeypatch):
    _patch_httpx(monkeypatch, _FakeResp(json_data={"results": []}))
    assert _provider().ping()["status"] == "ok"


def test_ping_network_error_is_error(monkeypatch):
    class _BoomClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            raise OSError("connection refused")

    monkeypatch.setattr("httpx.Client", _BoomClient)
    s = _provider().ping()
    assert s["status"] == "error" and "error" in s


# ---------- misc ----------


def test_cache_signature():
    assert _provider().cache_signature == "searxng/v1"
