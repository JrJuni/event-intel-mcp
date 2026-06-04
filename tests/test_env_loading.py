"""_env.load_project_env — .env fallback for blank .mcpb form keys (Phase 18T.2).

Semantics under test:
- A blank form value (injected as "") is popped so `.env` can fill it.
- A non-empty value (form/shell) is preserved (load_dotenv override=False).
- No `.env` file → no crash; missing keys stay missing.
"""
from __future__ import annotations

import os

import pytest

from event_intel._env import load_project_env


@pytest.fixture(autouse=True)
def _preserve_keys():
    """load_dotenv writes os.environ directly — snapshot/restore the test keys."""
    keys = ("ANTHROPIC_API_KEY", "BRAVE_API_KEY", "EVENT_INTEL_USE_CHATGPT_OAUTH")
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_blank_form_key_filled_from_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # blank form field
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=fromfile\n", encoding="utf-8")

    load_project_env(repo_root=tmp_path)

    assert os.environ["ANTHROPIC_API_KEY"] == "fromfile"


def test_nonempty_form_key_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "formval")  # user typed it into the form
    (tmp_path / ".env").write_text("BRAVE_API_KEY=fromfile\n", encoding="utf-8")

    load_project_env(repo_root=tmp_path)

    assert os.environ["BRAVE_API_KEY"] == "formval"  # form wins (override=False)


def test_form_boolean_is_authoritative_over_dotenv(tmp_path, monkeypatch):
    """Boolean form fields (the .mcpb checkboxes) are NOT popped, so a form-injected
    value wins and .env cannot flip it — only API keys fall back to .env.
    Locks the policy from blind-review #2 (Phase 18T.2)."""
    monkeypatch.setenv("EVENT_INTEL_USE_CHATGPT_OAUTH", "false")  # unchecked box
    (tmp_path / ".env").write_text("EVENT_INTEL_USE_CHATGPT_OAUTH=true\n", encoding="utf-8")

    load_project_env(repo_root=tmp_path)

    assert os.environ["EVENT_INTEL_USE_CHATGPT_OAUTH"] == "false"  # form wins, .env shadowed


def test_no_dotenv_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # tmp_path has no .env

    load_project_env(repo_root=tmp_path)  # must not raise

    assert os.environ.get("ANTHROPIC_API_KEY") is None
