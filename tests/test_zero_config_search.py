"""Zero-config search provider — S1 (factory + config + check_runtime surface).

Covers make_search_provider selection/validation, cache_signature, the
config-fingerprint provider isolation, and the check_runtime search-status block.
ddgs/searxng land in later slices — here they must be recognized-but-unavailable.
"""
from __future__ import annotations

import importlib

import pytest

from event_intel.errors import ErrorCode, MCPError
from event_intel.providers import search as S

# ---------- make_search_provider ----------


def test_factory_default_is_ddgs_when_no_search_section():
    """S2 flips the zero-config default to ddgs (keyless); N3 wraps it with the
    keyless news fallback by default."""
    p = S.make_search_provider({})
    assert isinstance(p, S.FallbackSearchProvider)
    assert isinstance(p.primary, S.DdgsSearchProvider)


def test_factory_explicit_brave():
    p = S.make_search_provider({"search": {"provider": "brave"}})
    assert isinstance(p, S.BraveSearchProvider)


def test_auto_default_selects_brave_when_key_present(monkeypatch):
    """User decision 2026-06-11 (supersedes ZCS R1#1, pre-quality-evidence):
    default `auto` resolves to brave when the FREE key is set (control run:
    big-co met 7/10 vs keyless 1–3/10), keyless pool otherwise. An EXPLICIT
    provider value still always wins."""
    monkeypatch.setenv("BRAVE_API_KEY", "sk-brave")
    assert isinstance(S.make_search_provider({}), S.BraveSearchProvider)
    # explicit ddgs wins over the key
    p = S.make_search_provider({"search": {"provider": "ddgs"}})
    assert isinstance(p, S.FallbackSearchProvider)
    assert isinstance(p.primary, S.DdgsSearchProvider)
    # no key → auto falls back to the keyless pool
    monkeypatch.delenv("BRAVE_API_KEY")
    p2 = S.make_search_provider({})
    assert isinstance(p2, S.FallbackSearchProvider)


def test_factory_explicit_ddgs_threads_throttle_config():
    p = S.make_search_provider(
        {"search": {"provider": "ddgs", "min_interval_ms": 500, "max_retries": 7}}
    )
    assert isinstance(p, S.FallbackSearchProvider)
    ddgs = p.primary
    assert isinstance(ddgs, S.DdgsSearchProvider)
    assert ddgs.min_interval_ms == 500 and ddgs.max_retries == 7


def test_factory_searxng_requires_url():
    with pytest.raises(MCPError) as exc:
        S.make_search_provider({"search": {"provider": "searxng"}})  # no url
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR
    assert "searxng_url" in exc.value.message


def test_factory_searxng_with_url():
    p = S.make_search_provider(
        {"search": {"provider": "searxng", "searxng_url": "http://localhost:8888"}}
    )
    assert isinstance(p, S.SearxngSearchProvider)
    assert p.base_url == "http://localhost:8888"


def test_factory_invalid_provider_raises_with_allowed_list():
    with pytest.raises(MCPError) as exc:
        S.make_search_provider({"search": {"provider": "google"}})
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR
    assert "invalid" in exc.value.message
    assert set(exc.value.hint["allowed"]) == set(S._VALID_SEARCH_PROVIDERS)


# ---------- cache_signature ----------


def test_brave_cache_signature_is_stable():
    assert S.BraveSearchProvider().cache_signature == "brave/v1"


def test_abc_default_cache_signature_is_class_name():
    class _Dummy(S.SearchProvider):
        def search(self, query, *, kind="web", count=10, days=None, lang="en"):
            return []

        def ping(self):
            return {"status": "ok"}

    assert _Dummy().cache_signature == "_Dummy"


# ---------- config fingerprint provider isolation (R1#1) ----------


def test_config_fingerprint_changes_with_provider():
    enr = importlib.import_module("event_intel.events.enrichment")
    cfg = {"max_companies": 30, "count_web": 5, "count_news": 5,
           "news_days_back": 180, "official_url_levenshtein_threshold": 0.7}
    fp_brave = enr._config_fingerprint(cfg, provider_sig="brave/v1")
    fp_ddgs = enr._config_fingerprint(cfg, provider_sig="ddgs/9.9.0")
    assert fp_brave != fp_ddgs


# ---------- check_runtime search-status block ----------


def _block(monkeypatch, *, provider, searxng_url="", brave_key=False):
    cr = importlib.import_module("event_intel.tools.check_runtime")
    cfg = {"search": {"provider": provider, "searxng_url": searxng_url}}
    monkeypatch.setattr("event_intel.runtime.preflight.load_config", lambda *a, **k: cfg)
    if brave_key:
        monkeypatch.setenv("BRAVE_API_KEY", "x")
    else:
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    return cr._search_status_block()


def test_search_block_brave_with_key(monkeypatch):
    b = _block(monkeypatch, provider="brave", brave_key=True)
    assert b == {"provider": "brave", "status": "ok", "warnings": []}


def test_search_block_brave_without_key_warns(monkeypatch):
    b = _block(monkeypatch, provider="brave", brave_key=False)
    assert b["status"] == "missing_key"
    assert any("BRAVE_API_KEY" in w for w in b["warnings"])


def test_search_block_ddgs_is_best_effort(monkeypatch):
    b = _block(monkeypatch, provider="ddgs")
    assert b["status"] == "best_effort"
    assert any("best-effort" in w for w in b["warnings"])


def test_search_block_searxng_requires_url(monkeypatch):
    no_url = _block(monkeypatch, provider="searxng", searxng_url="")
    assert no_url["status"] == "missing_config"
    with_url = _block(monkeypatch, provider="searxng", searxng_url="http://localhost:8888")
    assert with_url["status"] == "ok"


def test_search_block_invalid_provider(monkeypatch):
    b = _block(monkeypatch, provider="google")
    assert b["status"] == "invalid"


def test_check_runtime_envelope_carries_search_block_on_success(monkeypatch):
    cr = importlib.import_module("event_intel.tools.check_runtime")
    monkeypatch.setattr(
        "event_intel.runtime.preflight.run_preflight",
        lambda *a, **k: {"ok": True, "checks": {}},
    )
    monkeypatch.setattr(
        "event_intel.runtime.preflight.load_config",
        lambda *a, **k: {"search": {"provider": "ddgs"}},
    )
    res = cr.check_runtime(workspace_id="default")
    assert res["ok"] is True
    assert res["search"]["provider"] == "ddgs"


def test_check_runtime_envelope_carries_search_block_on_failure(monkeypatch):
    cr = importlib.import_module("event_intel.tools.check_runtime")
    from event_intel.errors import MCPError as _ME
    from event_intel.errors import Stage as _St

    def _boom(*a, **k):
        raise _ME(error_code=ErrorCode.MODEL_NOT_READY, stage=_St.PREFLIGHT, message="x")

    monkeypatch.setattr("event_intel.runtime.preflight.run_preflight", _boom)
    monkeypatch.setattr(
        "event_intel.runtime.preflight.load_config",
        lambda *a, **k: {"search": {"provider": "brave"}},
    )
    res = cr.check_runtime(workspace_id="default")
    assert res["ok"] is False
    assert "search" in res and res["search"]["provider"] == "brave"
