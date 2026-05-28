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


def main() -> None:
    """Module entrypoint for `python -m event_intel.cli`."""
    app()


if __name__ == "__main__":
    main()
