"""Shared pytest fixtures.

Heavy-weight fixtures (Chroma, bge-m3) live in dedicated test files and are
opt-in — keep this conftest cheap so the test collection phase stays fast.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_api_keys_in_env(monkeypatch):
    """Default each test to a clean env. Tests that need keys can re-set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)


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
