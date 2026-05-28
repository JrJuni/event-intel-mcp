"""S6 — typer CLI smoke tests via CliRunner.

These exercise the CLI surface in isolation from real model/API state by
either (a) using `--help` so no provider is constructed, or (b) monkeypatching
the underlying tool-handler module that the CLI imports lazily inside the
command body.

The CLI is a *thin* wrapper — same code paths as MCP — so these tests are
deliberately light. Full e2e is in test_mcp_tools.py.
"""
from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from event_intel.cli import app


runner = CliRunner()


def test_root_help_lists_all_subcommands():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    out = res.stdout
    for sub in (
        "check-runtime",
        "draft-cards",
        "validate",
        "ingest",
        "build-event",
        "export-schema",
        "models",
    ):
        assert sub in out, f"`{sub}` missing from `event-intel --help`:\n{out}"


def test_build_event_rejects_multiple_source_flags():
    res = runner.invoke(
        app,
        ["build-event", "--workspace", "default", "--event-name", "X",
         "--event-slug", "x", "--html-file", "a.html", "--csv-file", "b.csv"],
    )
    assert res.exit_code != 0
    assert "Pick exactly one" in res.output or "html" in res.output.lower()


def test_build_event_rejects_no_source_flag():
    res = runner.invoke(
        app,
        ["build-event", "--workspace", "default", "--event-name", "X", "--event-slug", "x"],
    )
    assert res.exit_code != 0


def test_models_help_lists_prepare_and_verify():
    res = runner.invoke(app, ["models", "--help"])
    assert res.exit_code == 0
    assert "prepare" in res.stdout
    assert "verify" in res.stdout


def test_export_schema_writes_json_file(tmp_path):
    out_path = tmp_path / "schema.json"
    res = runner.invoke(app, ["export-schema", "--out", str(out_path), "--format", "json"])
    assert res.exit_code == 0, res.stdout + res.stderr
    assert out_path.is_file()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    # JSON Schema basics — pydantic emits a `$defs` / `properties` shape.
    assert "properties" in data
    assert "schema_version" in data["properties"]


def test_validate_subcommand_against_sample_cards(repo_root):
    res = runner.invoke(
        app,
        ["validate", "--cards",
         str(repo_root / "tests" / "fixtures" / "cards" / "sample_cards.yaml")],
    )
    # Output is JSON envelope from the validate tool. Exit code mirrors `ok`.
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["ok"] is True
    assert payload["schema_version"] == 1


def test_draft_cards_requires_source_or_text():
    res = runner.invoke(app, ["draft-cards", "--workspace", "default"])
    assert res.exit_code != 0
    # Newer typer/Click merges streams in CliRunner by default; just check stdout.
    assert "--source" in res.output or "source" in res.output.lower()


def test_check_runtime_returns_envelope_even_with_no_keys(monkeypatch):
    """check-runtime should always print a JSON envelope (success or failure)
    and exit with code matching the envelope's `ok`. With no keys set,
    expect a failure envelope — but still JSON-parseable."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    res = runner.invoke(app, ["check-runtime", "--workspace", "default"])
    # Regardless of pass/fail, output must be a JSON envelope.
    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"check-runtime did not emit JSON: {exc}\n{res.stdout}")
    assert "ok" in payload
    if payload["ok"] is False:
        for key in ("error_code", "stage", "message"):
            assert key in payload, f"failure envelope missing `{key}`"
        assert res.exit_code == 1
    else:
        assert res.exit_code == 0


# ---------- Phase 18T — new subcommands + --text-file mapping ----------

def test_root_help_now_lists_eight_tools():
    """Phase 18T adds analyze-page + acquire-source. Root help must grow."""
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    for sub in ("analyze-page", "acquire-source"):
        assert sub in res.output, (
            f"'{sub}' missing from --help after Phase 18T T0 wiring:\n{res.output}"
        )


def test_build_event_text_file_uses_text_file_source_kind(tmp_path, monkeypatch):
    """Phase 18T R2-4: --text-file must map to source_kind='text_file' (file-path
    contract), NOT 'text' (inline-string contract)."""
    from event_intel.tools import build_event_tier_list as _bt_mod
    from event_intel.runtime import preflight as _preflight

    # Capture the source_kind that the tool receives.
    captured: dict = {}

    def _fake_build(**kwargs):
        captured.update(kwargs)
        return {"ok": False, "error_code": "INTERNAL", "stage": "preflight",
                "message": "captured", "hint": None, "retryable": False}

    monkeypatch.setattr(_bt_mod, "build_event_tier_list", _fake_build)

    txt = tmp_path / "exhibitors.txt"
    txt.write_text("Exhibitor A — AI company", encoding="utf-8")

    runner.invoke(
        app,
        ["build-event", "--event-name", "X", "--event-slug", "x",
         "--text-file", str(txt)],
    )
    assert captured.get("source_kind") == "text_file", (
        f"Expected source_kind='text_file', got {captured.get('source_kind')!r}. "
        "The CLI must map --text-file to the file-path contract, not 'text' (inline)."
    )
