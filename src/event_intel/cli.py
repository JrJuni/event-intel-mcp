"""event-intel CLI — typer thin wrapper. See plan v0.5 §CLI Surface.

All heavy work is delegated to MCP tool handler functions, so the same code runs
from Claude Desktop (via FastMCP) and from the terminal.

UTF-8 stdio reconfigure runs at module top — required for Windows. See
docs/playbook.md UTF-8 stdio entry.
"""
from __future__ import annotations

import sys

# UTF-8 stdio reconfigure — before any framework I/O (typer/rich).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except (ValueError, AttributeError):
            pass

import json  # noqa: E402

import typer  # noqa: E402

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="event-intel — turn exhibitor lists into BD target tier lists.",
)

models_app = typer.Typer(no_args_is_help=True, help="Manage local model weights.")
app.add_typer(models_app, name="models")


def _print_json(payload: dict) -> None:
    typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


@app.command("check-runtime")
def check_runtime_cmd(
    workspace: str = typer.Option("default", "--workspace", "-w", help="Workspace ID."),
) -> None:
    """Run the 5-check runtime preflight for a workspace."""
    from event_intel.tools.check_runtime import check_runtime

    result = check_runtime(workspace_id=workspace)
    _print_json(result)
    raise typer.Exit(code=0 if result.get("ok") else 1)


@models_app.command("prepare")
def models_prepare_cmd(
    cache_dir: str | None = typer.Option(
        None,
        "--cache-dir",
        help="Override HF cache directory (defaults to $HF_HOME or ~/.cache/huggingface).",
    ),
) -> None:
    """Download bge-m3 weights (~1.3 GB) and verify with a smoke encode."""
    from event_intel.runtime.models import prepare_bge_m3

    try:
        result = prepare_bge_m3(cache_dir=cache_dir)
    except Exception as exc:
        from event_intel.errors import Stage, envelope_from_exception

        result = envelope_from_exception(exc, stage=Stage.PREFLIGHT)
        _print_json(result)
        raise typer.Exit(code=1) from exc
    _print_json(result)


@models_app.command("verify")
def models_verify_cmd(
    cache_dir: str | None = typer.Option(
        None,
        "--cache-dir",
        help="Override HF cache directory (defaults to $HF_HOME or ~/.cache/huggingface).",
    ),
) -> None:
    """Report whether bge-m3 weights are present in cache. Does NOT download."""
    from event_intel.runtime.models import verify_bge_m3

    result = verify_bge_m3(cache_dir=cache_dir)
    _print_json(result)
    raise typer.Exit(code=0 if result.get("status") == "ready" else 1)


@app.command("draft-cards")
def draft_cards_cmd(
    workspace: str = typer.Option("default", "--workspace", "-w", help="Workspace ID."),
    source: list[str] = typer.Option(
        None,
        "--source",
        "-s",
        help="Source file(s) (.md / .txt / .pdf). Repeatable.",
    ),
    text: str = typer.Option("", "--text", help="Inline source text (alternative to --source)."),
    lang: str = typer.Option("en", "--lang", help="Output language (en or ko)."),
    out: str | None = typer.Option(
        None, "--out", "-o", help="Output yaml path (default: outputs/{ws}/capability_cards.draft.yaml)."
    ),
) -> None:
    """Draft capability_cards.yaml from product source material."""
    from event_intel.tools.draft_capability_cards import draft_capability_cards

    if source and text:
        typer.echo("Pick either --source or --text, not both.", err=True)
        raise typer.Exit(code=2)
    if not source and not text:
        typer.echo("Provide --source <path> or --text <inline>.", err=True)
        raise typer.Exit(code=2)

    result = draft_capability_cards(
        workspace_id=workspace,
        source_kind="file" if source else "text",
        source_content=text,
        source_paths=list(source) if source else None,
        lang=lang,
        out_path=out,
    )
    _print_json(result)
    raise typer.Exit(code=0 if result.get("ok") else 1)


@app.command("validate")
def validate_cmd(
    cards: str = typer.Option(..., "--cards", "-c", help="Path to capability_cards.yaml."),
) -> None:
    """Validate a capability_cards.yaml against schema v1."""
    from event_intel.tools.validate_capability_cards import validate_capability_cards

    result = validate_capability_cards(cards_path=cards)
    _print_json(result)
    raise typer.Exit(code=0 if result.get("ok") else 1)


@app.command("ingest")
def ingest_cmd(
    cards: str = typer.Option(..., "--cards", "-c", help="Path to capability_cards.yaml."),
    workspace: str = typer.Option("default", "--workspace", "-w", help="Workspace ID."),
) -> None:
    """Embed + upsert cards into the product_{workspace} Chroma collection."""
    from event_intel.tools.ingest_capability_cards import ingest_product_context

    result = ingest_product_context(workspace_id=workspace, cards_path=cards)
    _print_json(result)
    raise typer.Exit(code=0 if result.get("ok") else 1)


@app.command("export-schema")
def export_schema_cmd(
    out: str | None = typer.Option(
        None,
        "--out",
        "-o",
        help="Output path (default: outputs/_schema/capability_cards.v{N}.json).",
    ),
    fmt: str = typer.Option("json", "--format", help="json (only json supported in v0)."),
) -> None:
    """Export the capability_cards JSON Schema generated from the pydantic SSOT."""
    import json as _json
    from pathlib import Path

    from event_intel.cards.schema import SCHEMA_VERSION, CapabilityCards

    if fmt != "json":
        typer.echo(f"unsupported --format {fmt!r}; only 'json' supported in v0", err=True)
        raise typer.Exit(code=2)

    schema = CapabilityCards.model_json_schema()
    out_path = Path(out) if out else Path("outputs") / "_schema" / f"capability_cards.v{SCHEMA_VERSION}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )
    _print_json({"ok": True, "path": str(out_path), "schema_version": SCHEMA_VERSION})


def main() -> None:
    """Module entrypoint for `python -m event_intel.cli`."""
    app()


if __name__ == "__main__":
    main()
