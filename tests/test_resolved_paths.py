"""W0 — runtime.paths.ResolvedPaths resolution + the two path bugs it fixes.

The resolver is injectable (env / home / repo_root) so these tests never touch
the real home dir or the real repo. Covers: per-leaf precedence matrix, the
workspace-root back-compat fallback, legacy env honoring, awkward Windows-style
paths (spaces / non-ASCII / foreign drive), and the bug-(a)/bug-(b) regressions.
"""
from __future__ import annotations

import pytest

from event_intel.runtime import paths as P


def _env(**kw) -> dict[str, str]:
    return dict(kw)


# --------------------------------------------------------------------------- #
# data_root + leaf defaults
# --------------------------------------------------------------------------- #
def test_defaults_use_home_data_root(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    rp = P.resolve_paths(env=_env(), home=home, repo_root=repo)
    assert rp.data_root == home / ".event-intel"
    assert rp.chroma_dir == home / ".event-intel" / "chroma"
    assert rp.artifacts_root == home / ".event-intel" / "artifacts"


def test_data_dir_env_moves_chroma_and_artifacts(tmp_path):
    home = tmp_path / "home"
    data = tmp_path / "elsewhere"
    rp = P.resolve_paths(
        env=_env(EVENT_INTEL_DATA_DIR=str(data)), home=home, repo_root=tmp_path / "repo"
    )
    assert rp.data_root == data
    assert rp.chroma_dir == data / "chroma"
    assert rp.artifacts_root == data / "artifacts"


# --------------------------------------------------------------------------- #
# chroma_dir precedence:  env  >  config.paths.chroma_dir  >  default
# --------------------------------------------------------------------------- #
def test_chroma_env_beats_config(tmp_path):
    rp = P.resolve_paths(
        {"paths": {"chroma_dir": str(tmp_path / "from_cfg")}},
        env=_env(EVENT_INTEL_CHROMA_DIR=str(tmp_path / "from_env")),
        home=tmp_path / "home",
        repo_root=tmp_path / "repo",
    )
    assert rp.chroma_dir == tmp_path / "from_env"


def test_chroma_config_beats_default(tmp_path):
    home = tmp_path / "home"
    rp = P.resolve_paths(
        {"paths": {"chroma_dir": str(tmp_path / "from_cfg")}},
        env=_env(),
        home=home,
        repo_root=tmp_path / "repo",
    )
    assert rp.chroma_dir == tmp_path / "from_cfg"


def test_artifacts_config_key_honored(tmp_path):
    rp = P.resolve_paths(
        {"paths": {"artifacts_dir": str(tmp_path / "arts")}},
        env=_env(),
        home=tmp_path / "home",
        repo_root=tmp_path / "repo",
    )
    assert rp.artifacts_root == tmp_path / "arts"


# --------------------------------------------------------------------------- #
# workspace_root precedence + back-compat fallback
# --------------------------------------------------------------------------- #
def test_fresh_install_uses_new_eventintel_layout(tmp_path):
    """Neither ~/EventIntel nor <repo>/outputs exists → new layout."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"  # no outputs/ dir
    rp = P.resolve_paths(env=_env(), home=home, repo_root=repo)
    assert rp.workspace_root == home / "EventIntel"
    assert rp.workspace_root_is_legacy is False


def test_existing_checkout_falls_back_to_repo_outputs(tmp_path):
    """<repo>/outputs exists (the tracked .gitkeep guarantees this on a checkout)
    and ~/EventIntel has no payload → use legacy outputs (back-compat)."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / "outputs").mkdir(parents=True)
    (repo / "outputs" / ".gitkeep").write_text("", encoding="utf-8")
    rp = P.resolve_paths(env=_env(), home=home, repo_root=repo)
    assert rp.workspace_root == repo / "outputs"
    assert rp.workspace_root_is_legacy is True


def test_migrated_install_prefers_new_layout_once_it_has_payload(tmp_path):
    """Both exist, ~/EventIntel has real data → new layout wins (post-migration)."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / "outputs").mkdir(parents=True)
    (home / "EventIntel" / "default").mkdir(parents=True)  # payload
    rp = P.resolve_paths(env=_env(), home=home, repo_root=repo)
    assert rp.workspace_root == home / "EventIntel"
    assert rp.workspace_root_is_legacy is False


def test_legacy_output_env_wins_and_is_not_legacy_flagged(tmp_path):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / "outputs").mkdir(parents=True)  # would otherwise trigger fallback
    custom = tmp_path / "my custom out"
    rp = P.resolve_paths(
        env=_env(EVENT_INTEL_OUTPUT_DIR=str(custom)), home=home, repo_root=repo
    )
    assert rp.workspace_root == custom
    assert rp.workspace_root_is_legacy is False


def test_workspace_dir_env_beats_config_and_default(tmp_path):
    rp = P.resolve_paths(
        {"paths": {"workspace_dir": str(tmp_path / "cfg_ws")}},
        env=_env(EVENT_INTEL_WORKSPACE_DIR=str(tmp_path / "env_ws")),
        home=tmp_path / "home",
        repo_root=tmp_path / "repo",
    )
    assert rp.workspace_root == tmp_path / "env_ws"


def test_workspace_dir_config_beats_default(tmp_path):
    repo = tmp_path / "repo"
    (repo / "outputs").mkdir(parents=True)  # fallback candidate, must lose to config
    rp = P.resolve_paths(
        {"paths": {"workspace_dir": str(tmp_path / "cfg_ws")}},
        env=_env(),
        home=tmp_path / "home",
        repo_root=repo,
    )
    assert rp.workspace_root == tmp_path / "cfg_ws"


# --------------------------------------------------------------------------- #
# Windows-style awkward paths (spaces / non-ASCII / foreign drive shape)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "leaf",
    [
        "Program Files/Event Intel",  # spaces
        "사용자/이벤트",  # Korean
        "OneDrive - Acme/Event Intel",  # space + dash (OneDrive)
    ],
)
def test_awkward_workspace_paths_round_trip(tmp_path, leaf):
    target = tmp_path / leaf
    rp = P.resolve_paths(
        env=_env(EVENT_INTEL_WORKSPACE_DIR=str(target)),
        home=tmp_path / "home",
        repo_root=tmp_path / "repo",
    )
    assert rp.workspace_root == target
    assert rp.workspace_dir("ws1") == target / "ws1"


def test_tilde_in_config_expands(tmp_path):
    rp = P.resolve_paths(
        {"paths": {"chroma_dir": "~/custom-chroma"}},
        env=_env(),
        home=tmp_path / "home",
        repo_root=tmp_path / "repo",
    )
    # ~ expands against the *real* home (expanduser), not the injected one — we
    # only assert it is no longer a literal tilde path.
    assert "~" not in str(rp.chroma_dir)
    assert rp.chroma_dir.is_absolute()


# --------------------------------------------------------------------------- #
# accessor shapes
# --------------------------------------------------------------------------- #
def test_accessor_layout(tmp_path):
    home = tmp_path / "home"
    rp = P.resolve_paths(env=_env(), home=home, repo_root=tmp_path / "repo")
    ws = rp.workspace_root
    assert rp.workspace_dir("acme") == ws / "acme"
    assert rp.sources_dir("acme", "product") == ws / "acme" / "sources" / "product"
    assert rp.sources_dir("acme", "company") == ws / "acme" / "sources" / "company"
    assert rp.cards_dir("acme") == ws / "acme" / "cards"
    assert rp.events_dir("acme") == ws / "acme" / "events"
    assert rp.artifact_dir("acme", "expo_2026") == rp.artifacts_root / "acme" / "expo_2026"
    assert rp.cache_dir == rp.data_root / "cache"
    assert rp.resume_dir == rp.data_root / "resume"
    assert (
        rp.source_index_manifest("acme")
        == rp.data_root / "source-index" / "acme" / "manifest.json"
    )


def test_resolver_is_side_effect_free(tmp_path):
    """Resolution must not create any directories."""
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    rp = P.resolve_paths(env=_env(), home=home, repo_root=repo)
    _ = (rp.chroma_dir, rp.artifacts_root, rp.workspace_dir("x"), rp.cache_dir)
    assert not home.exists()
    assert not repo.exists()


# --------------------------------------------------------------------------- #
# bug-(b): ChromaProvider honors config.paths.chroma_dir (was env-only)
# --------------------------------------------------------------------------- #
def test_chroma_provider_honors_config_chroma_dir(tmp_path, monkeypatch):
    from event_intel.providers import vectorstore as vs

    monkeypatch.delenv(P.ENV_CHROMA_DIR, raising=False)
    cfg = {"paths": {"chroma_dir": str(tmp_path / "cfg_chroma")}}
    provider = vs.ChromaProvider(config=cfg)
    assert provider.persist_dir == tmp_path / "cfg_chroma"


def test_chroma_provider_env_still_wins_over_config(tmp_path, monkeypatch):
    from event_intel.providers import vectorstore as vs

    monkeypatch.setenv(P.ENV_CHROMA_DIR, str(tmp_path / "env_chroma"))
    cfg = {"paths": {"chroma_dir": str(tmp_path / "cfg_chroma")}}
    provider = vs.ChromaProvider(config=cfg)
    assert provider.persist_dir == tmp_path / "env_chroma"


def test_chroma_provider_explicit_persist_dir_wins(tmp_path):
    from event_intel.providers import vectorstore as vs

    provider = vs.ChromaProvider(persist_dir=str(tmp_path / "explicit"))
    assert provider.persist_dir == tmp_path / "explicit"


# --------------------------------------------------------------------------- #
# bug-(a): draft_capability_cards writes under the workspace root, not cwd
# --------------------------------------------------------------------------- #
def test_draft_output_path_is_workspace_relative_not_cwd(tmp_path, monkeypatch):
    from event_intel.tools import draft_capability_cards as draft

    monkeypatch.chdir(tmp_path)  # foreign cwd, as the MCP server has
    monkeypatch.setenv(P.ENV_OUTPUT_DIR, str(tmp_path / "ws_root"))
    out = draft._resolve_output_path("acme", None)
    assert out == tmp_path / "ws_root" / "acme" / "capability_cards.draft.yaml"
    # the old bug produced a cwd-relative "outputs/..." path:
    assert out.is_absolute()


def test_draft_output_path_explicit_out_path_wins(tmp_path):
    from event_intel.tools import draft_capability_cards as draft

    explicit = tmp_path / "somewhere" / "cards.yaml"
    out = draft._resolve_output_path("acme", str(explicit))
    assert out == explicit
