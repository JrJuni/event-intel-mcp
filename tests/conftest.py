"""Shared pytest fixtures.

Heavy-weight fixtures (Chroma, bge-m3) live in dedicated test files and are
opt-in — keep this conftest cheap so the test collection phase stays fast.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_api_keys_in_env(monkeypatch):
    """Default each test to a clean env. Tests that need keys can re-set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory, monkeypatch):
    """Redirect Path.home() to a per-test temp dir so NO test can write into
    the real user home (caught 2026-06-11: enrichment's default failure-log
    path `~/.event-intel/diagnostics/{ws}/` was silently populated by test
    fakes — the 'dur'/'t6' workspaces polluted real R1 diagnostics that
    `benchmark retry-stats` then aggregated). Tests that genuinely need a
    home dir get the isolated one; none may touch the real one.
    """
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
    return fake_home


@pytest.fixture
def tmp_chroma_dir(tmp_path, monkeypatch) -> Path:
    """Isolated Chroma persist dir per test."""
    target = tmp_path / "chroma"
    target.mkdir()
    monkeypatch.setenv("EVENT_INTEL_CHROMA_DIR", str(target))
    return target


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent
