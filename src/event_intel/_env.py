"""Project .env loading for the CLI and MCP server entry points.

The `.mcpb` install form injects API keys as environment variables, but reinstalls
make re-entering them tedious. Because `event_intel` is editable-installed, the
server can derive the repo root from this file's location and load `<repo>/.env`
regardless of the working directory Claude Desktop spawns it in — so keys live in
`.env` and the form fields become optional.

Cold-start safe: imports only os / pathlib / python-dotenv at call time.
"""
from __future__ import annotations

# Keys the .mcpb form maps; blank form fields must not shadow .env values.
_FORM_KEYS = ("ANTHROPIC_API_KEY", "BRAVE_API_KEY")


def load_project_env(*, keys: tuple[str, ...] = _FORM_KEYS, repo_root=None) -> None:
    """Load the project's `.env`, letting `.env` fill keys the form left blank.

    Semantics:
      - A **non-empty** value already in the environment (e.g. a key typed into the
        .mcpb form) is preserved — `load_dotenv` runs with `override=False`.
      - A **blank** value (unchecked / empty form field injects ``""``) is popped
        first so `.env` can supply it. Without this, the empty string would shadow
        the `.env` value under `override=False` and providers would see "no key".
    """
    import os
    from pathlib import Path

    from dotenv import load_dotenv

    # Only the API keys are popped/filled. Boolean form fields
    # (EVENT_INTEL_USE_CHATGPT_OAUTH / EVENT_INTEL_WARM_ON_START) are intentionally
    # NOT in `keys`: the .mcpb checkbox is authoritative for them, so a form-injected
    # value survives (load_dotenv override=False) and .env cannot flip it.
    for k in keys:
        if os.environ.get(k, "").strip() == "":
            os.environ.pop(k, None)

    # Repo root is derived from the package location (editable install:
    # <repo>/src/event_intel/_env.py). Deterministic and cwd-independent. For a
    # non-editable install this points into site-packages, where there is no .env
    # and load_dotenv is a harmless no-op (form / shell env supply the keys then).
    root = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env")
