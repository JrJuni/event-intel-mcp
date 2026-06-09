"""S1 — runtime preflight tests.

Strategy: inject fake providers so we exercise the orchestrator without touching
disk caches, the network, or ML libraries. Real provider behavior is covered by
its own provider tests (later streams).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from event_intel.errors import ErrorCode, MCPError, Stage
from event_intel.runtime import preflight as _preflight
from event_intel.runtime.preflight import (
    load_config,
    run_preflight,
)
from event_intel.tools.check_runtime import check_runtime as check_runtime_tool

# ---------- Fakes ----------


class FakeEmbedding:
    def __init__(self, *, ready: bool = True):
        self._ready = ready
        self.warmed = False

    def is_ready(self) -> dict:
        if self._ready:
            return {"status": "ready", "path": "/fake/bge-m3", "size_mb": 1320}
        return {"status": "missing", "path": "/fake/bge-m3"}

    def warm_up(self) -> dict:
        self.warmed = True
        return {"status": "ready", "already_cached": False, "load_seconds": 0.0}

    def embed(self, texts):  # pragma: no cover - not used by preflight
        raise NotImplementedError


class FakeVectorStore:
    def __init__(self, *, writable: bool = True, product_chunks: int = 12):
        self._writable = writable
        self._product_chunks = product_chunks

    def ensure_writable(self) -> dict:
        if self._writable:
            return {"status": "writable", "path": "/fake/chroma"}
        return {"status": "denied", "path": "/fake/chroma", "error": "EACCES"}

    def collection_info(self, name: str) -> dict:
        if name.startswith("product_") and self._product_chunks > 0:
            return {"exists": True, "count": self._product_chunks}
        return {"exists": False, "count": 0}

    def upsert(self, **kwargs):  # pragma: no cover - not used by preflight
        raise NotImplementedError

    def query(self, **kwargs):  # pragma: no cover - not used by preflight
        raise NotImplementedError


class FakeLLM:
    def __init__(self, *, has_key: bool = True):
        self._has_key = has_key

    def ping(self) -> dict:
        if self._has_key:
            return {"status": "ok", "model": "claude-sonnet-4-6"}
        return {
            "status": "missing_key",
            "message": "ANTHROPIC_API_KEY missing or invalid",
            "fix": "Set ANTHROPIC_API_KEY in .env (see .env.example)",
        }

    def chat_cached(self, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def chat_once(self, **kwargs):  # pragma: no cover
        raise NotImplementedError


class FakeSearch:
    def __init__(self, *, has_key: bool = True, quota: int | None = 1500, error: str | None = None):
        self._has_key = has_key
        self._quota = quota
        self._error = error

    def ping(self) -> dict:
        if not self._has_key:
            return {"status": "missing_key", "remaining_quota": None}
        if self._error:
            return {"status": "error", "remaining_quota": None, "error": self._error}
        return {"status": "ok", "remaining_quota": self._quota}

    def search(self, **kwargs):  # pragma: no cover
        raise NotImplementedError


@pytest.fixture
def all_ready():
    """Bundle of fakes where every check passes."""
    return dict(
        embedding_provider=FakeEmbedding(ready=True),
        vectorstore_provider=FakeVectorStore(writable=True, product_chunks=12),
        llm_provider=FakeLLM(has_key=True),
        search_provider=FakeSearch(has_key=True, quota=1500),
    )


@pytest.fixture
def minimal_config():
    """Inline config dict that satisfies all required keys."""
    return {
        "schema_version": 1,
        "llm": {"draft_cards_model": "claude-sonnet-4-6"},
        "extraction": {"max_chunks_per_event": 12, "source_snippet_min_chars": 20},
        "scoring": {
            "weights": {"capability_fit": 0.3},
            "tier_rules": {"S": {"min_final_score": 7.5}},
        },
        "paths": {"chroma_dir": "~/.event-intel/chroma"},
    }


@pytest.fixture(autouse=True)
def _reset_warmup():
    """Warm-up state is process-global — reset around every test for isolation."""
    from event_intel.runtime import warmup

    warmup.reset()
    yield
    warmup.reset()


# ---------- Success path ----------


def test_preflight_success_with_all_ready(all_ready, minimal_config):
    result = run_preflight("default", config=minimal_config, **all_ready)
    assert result["ok"] is True
    checks = result["checks"]
    assert checks["embedding_model"]["status"] == "ready"
    assert checks["vectorstore"]["status"] == "writable"
    assert checks["llm_api"]["status"] == "ok"
    assert checks["brave_api"]["status"] == "ok"
    assert checks["brave_api"]["remaining_quota"] == 1500
    assert checks["product_context"]["status"] == "ready"
    assert checks["product_context"]["collection"] == "product_default"
    assert checks["product_context"]["chunks"] == 12
    assert isinstance(result["elapsed_ms"], int)


def test_preflight_no_warm_up_reports_not_started(all_ready, minimal_config):
    """Without warm_up, the model isn't loaded; checks.warm_up reports not_started."""
    result = run_preflight("default", config=minimal_config, **all_ready)
    assert result["ok"] is True
    assert all_ready["embedding_provider"].warmed is False
    assert result["checks"]["warm_up"]["status"] == "not_started"


def test_preflight_warm_up_block_loads_embedding_model(all_ready, minimal_config):
    """warm_up + warm_up_block (terminal CLI) loads inline and reports ready."""
    result = run_preflight(
        "default", warm_up=True, warm_up_block=True, config=minimal_config, **all_ready
    )
    assert result["ok"] is True
    assert all_ready["embedding_provider"].warmed is True
    assert result["checks"]["warm_up"]["status"] == "ready"


def test_preflight_warm_up_async_never_blocks(all_ready, minimal_config):
    """warm_up without block (MCP path) starts a background load and returns at once.

    The fake warm_up is instant, so status is 'warming' or already 'ready' — both
    are valid; the point is run_preflight returns a status dict without raising.
    """
    result = run_preflight("default", warm_up=True, config=minimal_config, **all_ready)
    assert result["ok"] is True
    assert result["checks"]["warm_up"]["status"] in ("warming", "ready")


# ---------- Per-check failure paths ----------


def test_preflight_invalid_workspace_id_returns_invalid_input(all_ready, minimal_config):
    with pytest.raises(MCPError) as exc:
        run_preflight("bad slug!", config=minimal_config, **all_ready)
    assert exc.value.error_code == ErrorCode.INVALID_INPUT
    assert exc.value.stage == Stage.PREFLIGHT


def test_preflight_missing_embedding_model_returns_model_not_ready(all_ready, minimal_config):
    all_ready["embedding_provider"] = FakeEmbedding(ready=False)
    with pytest.raises(MCPError) as exc:
        run_preflight("default", config=minimal_config, **all_ready)
    assert exc.value.error_code == ErrorCode.MODEL_NOT_READY
    assert "models prepare" in str(exc.value.hint)


def test_preflight_vectorstore_not_writable_returns_io_error(all_ready, minimal_config):
    all_ready["vectorstore_provider"] = FakeVectorStore(writable=False)
    with pytest.raises(MCPError) as exc:
        run_preflight("default", config=minimal_config, **all_ready)
    assert exc.value.error_code == ErrorCode.IO_ERROR


def test_preflight_missing_anthropic_key_returns_config_error(all_ready, minimal_config):
    all_ready["llm_provider"] = FakeLLM(has_key=False)
    with pytest.raises(MCPError) as exc:
        run_preflight("default", config=minimal_config, **all_ready)
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR
    assert "ANTHROPIC_API_KEY" in exc.value.message


def test_preflight_missing_brave_key_returns_config_error(all_ready, minimal_config):
    all_ready["search_provider"] = FakeSearch(has_key=False)
    with pytest.raises(MCPError) as exc:
        run_preflight("default", config=minimal_config, **all_ready)
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR
    # message is now provider-neutral; the brave key hint lives in the fix.
    assert "BRAVE_API_KEY" in str(exc.value.hint)


# ---------- R3-#1: product context lifecycle ----------


def test_preflight_missing_product_context_returns_product_context_missing(
    all_ready, minimal_config
):
    all_ready["vectorstore_provider"] = FakeVectorStore(writable=True, product_chunks=0)
    with pytest.raises(MCPError) as exc:
        run_preflight("default", config=minimal_config, **all_ready)
    assert exc.value.error_code == ErrorCode.PRODUCT_CONTEXT_MISSING
    assert "ingest" in str(exc.value.hint)


def test_preflight_skip_product_context_for_ingest(all_ready, minimal_config):
    """ingest_product_context's own preflight must NOT require product context — the
    whole point of that call is to create the collection."""
    all_ready["vectorstore_provider"] = FakeVectorStore(writable=True, product_chunks=0)
    result = run_preflight(
        "default",
        config=minimal_config,
        require_product_context=False,
        **all_ready,
    )
    assert result["ok"] is True
    assert result["checks"]["product_context"]["status"] == "missing"


# ---------- R3-#4: brave quota optional ----------


def test_preflight_brave_quota_null_still_ok(all_ready, minimal_config):
    all_ready["search_provider"] = FakeSearch(has_key=True, quota=None)
    result = run_preflight("default", config=minimal_config, **all_ready)
    assert result["ok"] is True
    assert result["checks"]["brave_api"]["status"] == "ok"
    assert result["checks"]["brave_api"]["remaining_quota"] is None


# ---------- R3-#2: config error ----------


def test_config_defaults_missing_key_returns_config_error(tmp_path):
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump({"schema_version": 1}), encoding="utf-8")
    with pytest.raises(MCPError) as exc:
        load_config(broken)
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR
    assert "missing required config key" in exc.value.message
    hint = exc.value.hint
    assert isinstance(hint, dict)
    assert str(broken) in hint["expected_path"]


def test_config_defaults_file_not_found_returns_config_error(tmp_path):
    missing = tmp_path / "nope.yaml"
    with pytest.raises(MCPError) as exc:
        load_config(missing)
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR
    assert "not found" in exc.value.message


def test_default_defaults_yaml_loads(repo_root: Path):
    """The shipped config/defaults.yaml must satisfy every required key."""
    data = load_config(repo_root / "config" / "defaults.yaml")
    assert data["schema_version"] == 1
    assert data["llm"]["draft_cards_model"]
    assert data["extraction"]["max_chunks_per_event"] == 12


# ---------- User config override (deep merge) ----------


def test_user_config_overrides_single_key(tmp_path, monkeypatch):
    """~/.event-intel/config.yaml (or EVENT_INTEL_CONFIG) overrides defaults
    via deep merge — user file may be partial.
    """
    user_cfg = tmp_path / "user.yaml"
    user_cfg.write_text(
        yaml.safe_dump({"llm": {"provider": "chatgpt_oauth"}}), encoding="utf-8"
    )
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(user_cfg))

    data = load_config()  # no path arg → uses defaults + user merge
    # User override wins
    assert data["llm"]["provider"] == "chatgpt_oauth"
    # Sibling keys preserved from defaults
    assert data["llm"]["draft_cards_model"] == "claude-sonnet-4-6"
    assert data["extraction"]["max_chunks_per_event"] == 12


def test_user_config_overrides_nested_key_without_clobbering_siblings(tmp_path, monkeypatch):
    """Deep merge: override a single nested key, all sibling nested keys survive."""
    user_cfg = tmp_path / "user.yaml"
    user_cfg.write_text(
        yaml.safe_dump({"scoring": {"weights": {"capability_fit": 0.99}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(user_cfg))

    data = load_config()
    assert data["scoring"]["weights"]["capability_fit"] == 0.99
    # Sibling weight untouched
    assert data["scoring"]["weights"]["source_confidence"] == 0.15
    # Sibling section (tier_rules) untouched
    assert data["scoring"]["tier_rules"]["S"]["min_final_score"] == 7.5


def test_missing_user_config_is_silently_ignored(tmp_path, monkeypatch):
    """Pointing EVENT_INTEL_CONFIG at a non-existent file is OK — defaults stand."""
    missing = tmp_path / "no_such_file.yaml"
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(missing))

    data = load_config()  # must not raise
    assert data["llm"]["draft_cards_model"] == "claude-sonnet-4-6"


def test_user_config_partial_file_does_not_trigger_required_key_check(tmp_path, monkeypatch):
    """A partial user file should NOT fail required-key validation — only the
    final merged dict is checked.
    """
    user_cfg = tmp_path / "user.yaml"
    user_cfg.write_text(yaml.safe_dump({"llm": {"provider": "chatgpt_oauth"}}), encoding="utf-8")
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(user_cfg))

    data = load_config()
    # No CONFIG_ERROR even though user file alone lacks schema_version etc.
    assert data["schema_version"] == 1


# ---------- LLM provider env override (.mcpb checkbox / power-user) ----------


def test_env_use_chatgpt_oauth_truthy_overrides_config(tmp_path, monkeypatch):
    """EVENT_INTEL_USE_CHATGPT_OAUTH=true forces chatgpt_oauth over a config that says anthropic."""
    user_cfg = tmp_path / "user.yaml"
    user_cfg.write_text(yaml.safe_dump({"llm": {"provider": "anthropic"}}), encoding="utf-8")
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(user_cfg))
    monkeypatch.setenv("EVENT_INTEL_USE_CHATGPT_OAUTH", "true")
    monkeypatch.delenv("EVENT_INTEL_LLM_PROVIDER", raising=False)

    data = load_config()
    assert data["llm"]["provider"] == "chatgpt_oauth"
    # Sibling llm keys from defaults survive the override
    assert data["llm"]["draft_cards_model"] == "claude-sonnet-4-6"


def test_env_use_chatgpt_oauth_falsey_is_noop(tmp_path, monkeypatch):
    """Opt-in semantics: =false does NOT clobber an existing chatgpt_oauth config."""
    user_cfg = tmp_path / "user.yaml"
    user_cfg.write_text(yaml.safe_dump({"llm": {"provider": "chatgpt_oauth"}}), encoding="utf-8")
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(user_cfg))
    monkeypatch.setenv("EVENT_INTEL_USE_CHATGPT_OAUTH", "false")
    monkeypatch.delenv("EVENT_INTEL_LLM_PROVIDER", raising=False)

    data = load_config()
    assert data["llm"]["provider"] == "chatgpt_oauth"


def test_env_use_chatgpt_oauth_empty_string_is_noop(tmp_path, monkeypatch):
    """Unchecked .mcpb box injects an empty string — must be a no-op (default stands)."""
    missing = tmp_path / "no_such.yaml"  # isolate from the real ~/.event-intel/config.yaml
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(missing))
    monkeypatch.setenv("EVENT_INTEL_USE_CHATGPT_OAUTH", "")
    monkeypatch.delenv("EVENT_INTEL_LLM_PROVIDER", raising=False)

    data = load_config()
    assert data["llm"]["provider"] == "anthropic"  # defaults.yaml value, untouched


def test_env_llm_provider_explicit_wins_over_boolean(tmp_path, monkeypatch):
    """EVENT_INTEL_LLM_PROVIDER (explicit) takes precedence over the boolean sugar."""
    missing = tmp_path / "no_such.yaml"
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(missing))
    monkeypatch.setenv("EVENT_INTEL_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("EVENT_INTEL_USE_CHATGPT_OAUTH", "true")

    data = load_config()
    assert data["llm"]["provider"] == "anthropic"


def test_env_llm_provider_openai_is_accepted(tmp_path, monkeypatch):
    """Y2.2a: 'openai' is a valid explicit provider (official key-based lane)."""
    missing = tmp_path / "no_such.yaml"
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(missing))
    monkeypatch.setenv("EVENT_INTEL_LLM_PROVIDER", "openai")
    monkeypatch.delenv("EVENT_INTEL_USE_CHATGPT_OAUTH", raising=False)

    data = load_config()
    assert data["llm"]["provider"] == "openai"
    # Sibling llm keys from defaults survive the override
    assert data["llm"]["openai_model"] == "gpt-4.1"


def test_env_llm_provider_invalid_raises_config_error(tmp_path, monkeypatch):
    """An unknown provider in the explicit env var fails loud as CONFIG_ERROR."""
    missing = tmp_path / "no_such.yaml"
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(missing))
    monkeypatch.setenv("EVENT_INTEL_LLM_PROVIDER", "gemini")

    with pytest.raises(MCPError) as exc:
        load_config()
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR
    hint = exc.value.hint
    assert isinstance(hint, dict)
    assert hint["env_var"] == "EVENT_INTEL_LLM_PROVIDER"
    assert "chatgpt_oauth" in hint["allowed"]


def test_env_override_not_applied_in_explicit_path_branch(repo_root: Path, monkeypatch):
    """load_config(explicit_path) is the test-compat branch — env override must NOT apply."""
    monkeypatch.setenv("EVENT_INTEL_USE_CHATGPT_OAUTH", "true")
    monkeypatch.setenv("EVENT_INTEL_LLM_PROVIDER", "chatgpt_oauth")

    data = load_config(repo_root / "config" / "defaults.yaml")
    # defaults.yaml says anthropic; the explicit-path branch ignores env entirely
    assert data["llm"]["provider"] == "anthropic"


# ---------- Y2.2b deploy-mode provider gating ----------


def _remote_env(monkeypatch):
    monkeypatch.setenv("EVENT_INTEL_DEPLOY_MODE", "remote")
    monkeypatch.delenv("EVENT_INTEL_USE_CHATGPT_OAUTH", raising=False)
    monkeypatch.delenv("EVENT_INTEL_LLM_PROVIDER", raising=False)


def test_remote_mode_blocks_chatgpt_oauth_from_config(tmp_path, monkeypatch):
    user_cfg = tmp_path / "user.yaml"
    user_cfg.write_text(yaml.safe_dump({"llm": {"provider": "chatgpt_oauth"}}), encoding="utf-8")
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(user_cfg))
    _remote_env(monkeypatch)

    with pytest.raises(MCPError) as exc:
        load_config()
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR
    assert exc.value.hint["deploy_mode"] == "remote"
    assert "anthropic" in exc.value.hint["fix"] and "openai" in exc.value.hint["fix"]


def test_remote_mode_blocks_chatgpt_oauth_from_env_boolean(tmp_path, monkeypatch):
    """The boolean sugar still routes through the gate (override runs first)."""
    missing = tmp_path / "no_such.yaml"
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(missing))
    monkeypatch.setenv("EVENT_INTEL_DEPLOY_MODE", "remote")
    monkeypatch.setenv("EVENT_INTEL_USE_CHATGPT_OAUTH", "true")
    monkeypatch.delenv("EVENT_INTEL_LLM_PROVIDER", raising=False)

    with pytest.raises(MCPError) as exc:
        load_config()
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR


def test_remote_mode_allows_anthropic(tmp_path, monkeypatch):
    missing = tmp_path / "no_such.yaml"  # defaults.yaml → anthropic
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(missing))
    _remote_env(monkeypatch)

    data = load_config()
    assert data["llm"]["provider"] == "anthropic"


def test_remote_mode_allows_openai(tmp_path, monkeypatch):
    missing = tmp_path / "no_such.yaml"
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(missing))
    _remote_env(monkeypatch)
    monkeypatch.setenv("EVENT_INTEL_LLM_PROVIDER", "openai")

    data = load_config()
    assert data["llm"]["provider"] == "openai"


def test_personal_local_default_allows_chatgpt_oauth(tmp_path, monkeypatch):
    """Default mode (no env) preserves the free-trial OAuth path."""
    user_cfg = tmp_path / "user.yaml"
    user_cfg.write_text(yaml.safe_dump({"llm": {"provider": "chatgpt_oauth"}}), encoding="utf-8")
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(user_cfg))
    monkeypatch.delenv("EVENT_INTEL_DEPLOY_MODE", raising=False)
    monkeypatch.delenv("EVENT_INTEL_USE_CHATGPT_OAUTH", raising=False)
    monkeypatch.delenv("EVENT_INTEL_LLM_PROVIDER", raising=False)

    data = load_config()
    assert data["llm"]["provider"] == "chatgpt_oauth"


def test_explicit_personal_local_allows_chatgpt_oauth(tmp_path, monkeypatch):
    user_cfg = tmp_path / "user.yaml"
    user_cfg.write_text(yaml.safe_dump({"llm": {"provider": "chatgpt_oauth"}}), encoding="utf-8")
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(user_cfg))
    monkeypatch.setenv("EVENT_INTEL_DEPLOY_MODE", "personal-local")
    monkeypatch.delenv("EVENT_INTEL_USE_CHATGPT_OAUTH", raising=False)
    monkeypatch.delenv("EVENT_INTEL_LLM_PROVIDER", raising=False)

    data = load_config()
    assert data["llm"]["provider"] == "chatgpt_oauth"


def test_invalid_deploy_mode_raises_config_error(tmp_path, monkeypatch):
    missing = tmp_path / "no_such.yaml"
    monkeypatch.setenv("EVENT_INTEL_CONFIG", str(missing))
    monkeypatch.setenv("EVENT_INTEL_DEPLOY_MODE", "production")  # not a valid mode

    with pytest.raises(MCPError) as exc:
        load_config()
    assert exc.value.error_code == ErrorCode.CONFIG_ERROR
    assert exc.value.hint["env_var"] == "EVENT_INTEL_DEPLOY_MODE"
    assert "personal-local" in exc.value.hint["allowed"]


def test_explicit_config_path_branch_is_not_gated(repo_root: Path, monkeypatch):
    """load_config(explicit_path) is the test-compat branch — deploy gate must NOT apply.

    Mirrors the env-override skip: a passed-in path is loaded verbatim.
    """
    monkeypatch.setenv("EVENT_INTEL_DEPLOY_MODE", "remote")
    cfg = repo_root / "config" / "defaults.yaml"
    data = load_config(cfg)  # defaults says anthropic anyway; just must not raise
    assert data["llm"]["provider"] == "anthropic"


def test_resolve_deploy_mode_helper(monkeypatch):
    from event_intel.runtime.preflight import resolve_deploy_mode

    monkeypatch.delenv("EVENT_INTEL_DEPLOY_MODE", raising=False)
    assert resolve_deploy_mode() == "personal-local"
    monkeypatch.setenv("EVENT_INTEL_DEPLOY_MODE", "")
    assert resolve_deploy_mode() == "personal-local"
    monkeypatch.setenv("EVENT_INTEL_DEPLOY_MODE", "remote")
    assert resolve_deploy_mode() == "remote"


# ---------- Tool boundary ----------


def test_check_runtime_tool_renders_envelope_on_failure(monkeypatch, minimal_config):
    """tools/check_runtime.py must catch MCPError and render to envelope."""

    def fake_run_preflight(workspace_id, **kwargs):
        raise MCPError(
            error_code=ErrorCode.MODEL_NOT_READY,
            stage=Stage.PREFLIGHT,
            message="bge-m3 not cached",
            hint={"fix": "Run `event-intel models prepare`"},
            retryable=False,
        )

    monkeypatch.setattr(_preflight, "run_preflight", fake_run_preflight)
    result = check_runtime_tool(workspace_id="default")
    assert result["ok"] is False
    assert result["error_code"] == "MODEL_NOT_READY"
    assert result["stage"] == "preflight"
    assert result["hint"]["fix"] == "Run `event-intel models prepare`"
