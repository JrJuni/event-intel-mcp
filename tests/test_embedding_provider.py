"""BgeM3Provider — process-level model cache + warm_up (Phase 18T.1 perf).

The real bge-m3 load is ~1.3 GB; these tests inject a fake `sentence_transformers`
module so no model/torch loads. They lock two contracts:
- `_get_model` caches the SentenceTransformer at process level (keyed by cache_dir),
  so repeated BgeM3Provider instances reuse one in-memory model.
- `warm_up()` loads the model and reports `already_cached` so callers can tell an
  instant warm-up from a cold load.
"""
from __future__ import annotations

import sys
import types

import numpy as np

from event_intel.providers.embedding import BgeM3Provider


def _install_fake_sentence_transformers(monkeypatch) -> dict:
    """Replace sentence_transformers with a counter-backed fake. Returns the counter."""
    calls = {"count": 0}

    class _FakeST:
        def __init__(self, name, cache_folder=None):
            calls["count"] += 1
            self.name = name

        def encode(self, texts, **kwargs):
            return np.array([[0.0, 1.0, 0.0] for _ in texts], dtype=float)

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = _FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    BgeM3Provider._MODEL_CACHE.clear()  # deterministic start
    return calls


def test_model_cached_across_instances(monkeypatch, tmp_path):
    calls = _install_fake_sentence_transformers(monkeypatch)

    p1 = BgeM3Provider(cache_dir=tmp_path)
    p1.embed(["a"])
    p2 = BgeM3Provider(cache_dir=tmp_path)
    p2.embed(["b"])

    # Two providers, same cache_dir → the model is constructed exactly once.
    assert calls["count"] == 1


def test_different_cache_dirs_load_separately(monkeypatch, tmp_path):
    calls = _install_fake_sentence_transformers(monkeypatch)

    BgeM3Provider(cache_dir=tmp_path / "a").embed(["x"])
    BgeM3Provider(cache_dir=tmp_path / "b").embed(["y"])

    assert calls["count"] == 2  # distinct cache keys → distinct models


def test_warm_up_reports_already_cached(monkeypatch, tmp_path):
    _install_fake_sentence_transformers(monkeypatch)

    p1 = BgeM3Provider(cache_dir=tmp_path)
    first = p1.warm_up()
    assert first["status"] == "ready"
    assert first["already_cached"] is False
    assert "load_seconds" in first

    # A fresh instance on the same cache_dir sees the warmed model.
    p2 = BgeM3Provider(cache_dir=tmp_path)
    second = p2.warm_up()
    assert second["already_cached"] is True
