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

from event_intel._env import load_project_env  # noqa: E402

# Load the repo's .env so API keys are available (same loader as the MCP server).
# Non-empty shell/form env wins; blank values fall back to .env.
load_project_env()

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
    warm_up: bool = typer.Option(
        False, "--warm-up", help="Also load the bge-m3 model into memory (waits inline) after checks pass."
    ),
) -> None:
    """Run the 5-check runtime preflight for a workspace."""
    from event_intel.tools.check_runtime import check_runtime

    # Terminal: load inline (warm_up_block) so the user waits and sees load_seconds.
    # The MCP server path stays non-blocking to respect the client timeout.
    result = check_runtime(workspace_id=workspace, warm_up=warm_up, warm_up_block=warm_up)
    _print_json(result)
    raise typer.Exit(code=0 if result.get("ok") else 1)


@app.command("login-chatgpt")
def login_chatgpt_cmd(
    force: bool = typer.Option(
        False, "--force", help="Re-authenticate even if a valid token is cached."
    ),
) -> None:
    """Authenticate the ChatGPT OAuth LLM provider (opens a browser, one-time).

    Run this once in a terminal when using the ChatGPT subscription path so the
    PKCE browser flow does not block lazily mid-tool-call. Token is cached at
    ~/.event-intel/chatgpt_auth.json and auto-refreshed thereafter.
    """
    from event_intel.errors import Stage, envelope_from_exception
    from event_intel.providers import llm as _llm
    from event_intel.runtime.preflight import load_config

    try:
        config = load_config()
        provider = _llm.make_llm_provider(config)
        if not isinstance(provider, _llm.ChatGPTOAuthProvider):
            typer.echo(
                "Note: llm.provider is not 'chatgpt_oauth'. Logging in anyway; set "
                "EVENT_INTEL_USE_CHATGPT_OAUTH=true (or check the box in the .mcpb form) "
                "to actually use ChatGPT OAuth at runtime.",
                err=True,
            )
            provider = _llm.ChatGPTOAuthProvider()
        result = provider.login(force=force)
    except Exception as exc:
        _print_json(envelope_from_exception(exc, stage=Stage.PREFLIGHT))
        raise typer.Exit(code=1) from exc
    _print_json(result)


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


@app.command("build-event")
def build_event_cmd(
    workspace: str = typer.Option("default", "--workspace", "-w", help="Workspace ID."),
    event_name: str = typer.Option(..., "--event-name", help="Human-readable event title."),
    event_slug: str = typer.Option(..., "--event-slug", help="Slug for outputs/Chroma keys."),
    html_file: str | None = typer.Option(None, "--html-file", help="Path to a saved exhibitor-list HTML."),
    csv_file: str | None = typer.Option(None, "--csv-file", help="Path to a CSV with at least a name column."),
    text_file: str | None = typer.Option(None, "--text-file", help="Path to a plain-text exhibitor list."),
    lang: str = typer.Option("en", "--lang", help="Output language (en or ko)."),
    max_companies: int | None = typer.Option(None, "--max-companies", help="Override enrichment cap."),
    no_enrich: bool = typer.Option(False, "--no-enrich", help="Skip Brave enrichment (snippet-only scoring)."),
    no_rationale: bool = typer.Option(False, "--no-rationale", help="Skip Sonnet rationale calls."),
    resume_from: str | None = typer.Option(None, "--resume-from", help="Path to a per-row JSONL resume artifact."),
    refresh: bool = typer.Option(False, "--refresh", help="Bypass resume + search cache — re-fetch every company (real refresh)."),
    target_mode: str | None = typer.Option(None, "--target-mode", help="customer | partner | ecosystem (default: config/card → customer)."),
) -> None:
    """Build a tiered exhibitor list for an event from a saved source file."""
    from event_intel.tools.build_event_tier_list import build_event_tier_list

    provided = [(k, v) for k, v in [("--html-file", html_file), ("--csv-file", csv_file), ("--text-file", text_file)] if v]
    if len(provided) != 1:
        typer.echo("Pick exactly one of --html-file / --csv-file / --text-file.", err=True)
        raise typer.Exit(code=2)
    flag, path = provided[0]
    if flag == "--html-file":
        source_kind, source_ref = "html_file", path
    elif flag == "--csv-file":
        source_kind, source_ref = "csv_file", path
    else:
        source_kind, source_ref = "text_file", path  # Phase 18T: file-path contract

    result = build_event_tier_list(
        workspace_id=workspace,
        event_name=event_name,
        event_slug=event_slug,
        source_kind=source_kind,
        source_ref=source_ref,
        lang=lang,
        max_companies=max_companies,
        enrichment_enabled=not no_enrich,
        run_rationale=not no_rationale,
        resume_from=resume_from,
        refresh=refresh,
        target_mode=target_mode,
    )
    _print_json(result)
    raise typer.Exit(code=0 if result.get("ok") else 1)


@app.command("analyze-page")
def analyze_page_cmd(
    url: str = typer.Option(..., "--url", help="Exhibition site URL to analyze."),
    workspace: str = typer.Option("default", "--workspace", "-w"),
    lang: str = typer.Option("en", "--lang"),
) -> None:
    """Classify an exhibition site URL and return acquisition hints (Phase 18T)."""
    from event_intel.tools.analyze_event_page import analyze_event_page

    result = analyze_event_page(url=url, lang=lang, workspace_id=workspace)
    _print_json(result)
    raise typer.Exit(code=0 if result.get("ok") else 1)


@app.command("acquire-source")
def acquire_source_cmd(
    url: str = typer.Option(..., "--url", help="Exhibition site URL to acquire from."),
    workspace: str = typer.Option("default", "--workspace", "-w"),
    event_slug: str = typer.Option(..., "--event-slug", help="Slug for the event (cache key)."),
    lang: str = typer.Option("en", "--lang"),
    refetch: bool = typer.Option(False, "--refetch", help="Ignore cached artifact and re-acquire."),
) -> None:
    """Analyze → probe → fetch → artifact; returns (source_kind, source_ref) (Phase 18T)."""
    from event_intel.tools.acquire_exhibitor_source import acquire_exhibitor_source

    result = acquire_exhibitor_source(
        url=url,
        workspace_id=workspace,
        event_slug=event_slug,
        lang=lang,
        refetch=refetch,
    )
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


@app.command("eval-matrix")
def eval_matrix_cmd(
    cell_dir: str = typer.Option(
        "tests/fixtures/eval",
        "--cell-dir",
        help="Directory of labeled eval cells (*.yaml).",
    ),
) -> None:
    """Run the scoring-matrix (1A) over labeled cells and print metrics (Phase 18V)."""
    from event_intel.eval.harness import run_matrix
    from event_intel.runtime.preflight import load_config

    config = load_config()
    cells = run_matrix(cell_dir, config=config)
    _print_json({"ok": True, "cells": [c.as_dict() for c in cells]})


benchmark_app = typer.Typer(
    no_args_is_help=True,
    help="Y1 real-data benchmark: gold-blind run → blind labels → measure (dev surface).",
)
app.add_typer(benchmark_app, name="benchmark")


@benchmark_app.command("threshold-freeze")
def benchmark_threshold_freeze_cmd(
    out: str = typer.Option(..., "--out", "-o", help="Manifest path (refuses overwrite)."),
    universe_file: str | None = typer.Option(
        None, "--universe-file", help="JSON of per-pair universe (caps/subset). Optional."
    ),
) -> None:
    """Freeze D6 gate thresholds + universe into an immutable manifest (step 1)."""
    from datetime import UTC, datetime
    from pathlib import Path

    from event_intel.eval import benchmark as _bm

    universe = json.loads(Path(universe_file).read_text(encoding="utf-8")) if universe_file else None
    manifest = _bm.freeze_thresholds(
        universe=universe, now_iso=datetime.now(UTC).isoformat(), path=out
    )
    _print_json({"ok": True, "manifest_path": out, "sha": manifest["sha"]})


@benchmark_app.command("run")
def benchmark_run_cmd(
    pair: str = typer.Option(..., "--pair", help="Pair id (e.g. p4_hyodol_hcr)."),
    runs_root: str = typer.Option("benchmarks/runs", "--runs-root", help="Root for run dirs."),
    workspace: str = typer.Option("default", "--workspace", "-w"),
    event_name: str = typer.Option(..., "--event-name"),
    event_slug: str = typer.Option(..., "--event-slug"),
    html_file: str | None = typer.Option(None, "--html-file"),
    csv_file: str | None = typer.Option(None, "--csv-file"),
    text_file: str | None = typer.Option(None, "--text-file"),
    lang: str = typer.Option("en", "--lang"),
    max_companies: int | None = typer.Option(None, "--max-companies"),
    no_enrich: bool = typer.Option(False, "--no-enrich"),
    no_rationale: bool = typer.Option(False, "--no-rationale"),
    target_mode: str | None = typer.Option(None, "--target-mode"),
) -> None:
    """Hidden run (step 3): build the tier list, persist an immutable gold-blind
    run-result. NO gold is read here — measure joins gold separately.
    """
    import json as _json
    from pathlib import Path

    from event_intel.eval import benchmark as _bm
    from event_intel.tools.build_event_tier_list import build_event_tier_list

    provided = [(k, v) for k, v in [("--html-file", html_file), ("--csv-file", csv_file), ("--text-file", text_file)] if v]
    if len(provided) != 1:
        typer.echo("Pick exactly one of --html-file / --csv-file / --text-file.", err=True)
        raise typer.Exit(code=2)
    flag, path = provided[0]
    source_kind = {"--html-file": "html_file", "--csv-file": "csv_file", "--text-file": "text_file"}[flag]

    result = build_event_tier_list(
        workspace_id=workspace, event_name=event_name, event_slug=event_slug,
        source_kind=source_kind, source_ref=path, lang=lang,
        max_companies=max_companies, enrichment_enabled=not no_enrich,
        run_rationale=not no_rationale, target_mode=target_mode,
    )
    if not result.get("ok") or not result.get("run_summary_path"):
        _print_json(result)
        raise typer.Exit(code=1)

    payload = _json.loads(Path(result["run_summary_path"]).read_text(encoding="utf-8"))
    run_dir = _bm.run(pair=pair, build_fn=lambda: payload, runs_root=runs_root)
    _print_json({"ok": True, "run_dir": str(run_dir), "run_id": payload["run_id"]})


@benchmark_app.command("company-packet")
def benchmark_company_packet_cmd(
    pair: str = typer.Option(..., "--pair"),
    roster_file: str = typer.Option(..., "--roster", help="Roster JSON (eval.roster.dump_roster shape)."),
    cohort: str = typer.Option("full", "--cohort", help="full | top10_decoy."),
    out: str = typer.Option(..., "--out", "-o", help="Company packet JSON path."),
    run_dir: str | None = typer.Option(None, "--run-dir", help="Run dir (required for top10_decoy)."),
    decoy_count: int = typer.Option(10, "--decoy-count"),
    seed: int = typer.Option(0, "--seed"),
) -> None:
    """Build the blind company packet (step 4) — names only, engine output hidden."""
    from pathlib import Path

    from event_intel.eval import benchmark as _bm
    from event_intel.eval import blind as _blind
    from event_intel.eval import roster as _roster

    roster = _roster.load_roster(roster_file)
    run_top10 = None
    if cohort == _blind.TOP10_DECOY:
        if not run_dir:
            typer.echo("--run-dir is required for cohort top10_decoy.", err=True)
            raise typer.Exit(code=2)
        rr = _bm.load_run_result(run_dir)
        ranked = sorted(rr.scored, key=lambda x: -x[1])[:10]
        run_top10 = [n for n, _ in ranked]

    packet = _blind.build_company_packet(
        pair=pair, cohort=cohort, roster=roster,
        run_top10_names=run_top10, decoy_count=decoy_count, seed=seed,
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(
        json.dumps(_blind.packet_to_dict(packet), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print_json({"ok": True, "packet_path": out, "cohort": cohort, "entries": len(packet.entries)})


@benchmark_app.command("evidence-packet")
def benchmark_evidence_packet_cmd(
    pair: str = typer.Option(..., "--pair"),
    run_dir: str = typer.Option(..., "--run-dir", help="Run dir (provides top-10 evidence)."),
    sealed_labels: str = typer.Option(..., "--sealed-labels", help="Sealed company labels JSON."),
    out: str = typer.Option(..., "--out", "-o", help="Evidence packet JSON path."),
) -> None:
    """Build the evidence packet (step 7) — ONLY after company labels are sealed.

    Refuses if --sealed-labels is missing/empty (R2-2: top-10 membership would
    otherwise bias the company labels).
    """
    from pathlib import Path

    from event_intel.eval import benchmark as _bm
    from event_intel.eval import blind as _blind

    sl_path = Path(sealed_labels)
    sealed = (
        _blind.sealed_labels_from_dict(json.loads(sl_path.read_text(encoding="utf-8")))
        if sl_path.is_file()
        else None
    )
    rr = _bm.load_run_result(run_dir)
    # build_evidence_packet raises ValueError if sealed is None (order enforcement).
    ev = _blind.build_evidence_packet(
        pair=pair, top10_evidence=rr.top10_evidence, sealed_company_labels=sealed
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(
        json.dumps(_blind.evidence_packet_to_dict(ev), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print_json({"ok": True, "evidence_packet_path": out, "items": len(ev.items)})


@benchmark_app.command("measure")
def benchmark_measure_cmd(
    run_dir: str = typer.Option(..., "--run-dir"),
    roster_file: str = typer.Option(..., "--roster"),
    sealed_labels: str = typer.Option(..., "--sealed-labels"),
    sealed_verdicts: str | None = typer.Option(None, "--sealed-verdicts"),
    threshold_manifest: str | None = typer.Option(None, "--thresholds", help="Frozen manifest."),
    target_mode: str = typer.Option("customer", "--target-mode"),
    out: str | None = typer.Option(None, "--out", "-o", help="Write the report JSON here too."),
) -> None:
    """Reveal + join (step 9): run-result + sealed gold + match → metrics + gates."""
    from pathlib import Path

    from event_intel.eval import benchmark as _bm
    from event_intel.eval import blind as _blind
    from event_intel.eval import roster as _roster

    rr = _bm.load_run_result(run_dir)
    roster = _roster.load_roster(roster_file)
    match = _roster.match_roster([n for n, _ in rr.scored], roster)
    sl = _blind.sealed_labels_from_dict(
        json.loads(Path(sealed_labels).read_text(encoding="utf-8"))
    )
    sv = None
    if sealed_verdicts:
        sv = _blind.sealed_verdicts_from_dict(
            json.loads(Path(sealed_verdicts).read_text(encoding="utf-8"))
        )
    gates = None
    if threshold_manifest:
        gates, _ = _bm.load_threshold_manifest(threshold_manifest)

    report = _bm.measure(
        run_result=rr, roster=roster, match=match, sealed_labels=sl,
        sealed_verdicts=sv, target_mode=target_mode, thresholds=gates,
    )
    payload = report.to_dict()
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_json({"ok": True, **payload})
    raise typer.Exit(code=0 if report.passed() else 1)


@benchmark_app.command("labeling-sheet")
def benchmark_labeling_sheet_cmd(
    pair: str = typer.Option(..., "--pair"),
    packet: str = typer.Option(..., "--packet", help="Company packet JSON (names only)."),
    source: str = typer.Option(..., "--source", help="Raw event source (csv or json)."),
    source_format: str = typer.Option("csv", "--source-format", help="csv | json."),
    records_path: str = typer.Option(
        "", "--records-path", help="For json: dotted key to the records list (e.g. company_data). Empty = top-level list."
    ),
    name_key: str = typer.Option("name", "--name-key"),
    overview_keys: str = typer.Option("description", "--overview-keys", help="Comma-separated, tried in order."),
    url_key: str | None = typer.Option(None, "--url-key"),
    card: str | None = typer.Option(None, "--card", help="Capability card yaml for the rubric header."),
    lang: str = typer.Option("ko", "--lang"),
    out_json: str = typer.Option(..., "--out-json", help="Fillable sheet JSON (edit the `label` field)."),
    out_md: str | None = typer.Option(None, "--out-md", help="Human-readable worksheet markdown."),
) -> None:
    """Build a fillable labeling sheet — packet names + NEUTRAL source overviews +
    product rubric. No engine score/tier/rank is included (blindness preserved).
    """
    import csv as _csv
    from pathlib import Path

    import yaml as _yaml

    from event_intel.eval import blind as _blind
    from event_intel.eval import labeling as _labeling

    # load raw source records
    if source_format == "csv":
        with open(source, encoding="utf-8") as f:
            records = list(_csv.DictReader(f))
    else:
        data = json.loads(Path(source).read_text(encoding="utf-8"))
        for key in (k for k in records_path.split(".") if k):
            data = data[key]
        records = data
    ctx = _labeling.build_context_from_records(
        records, name_key=name_key,
        overview_keys=tuple(k.strip() for k in overview_keys.split(",") if k.strip()),
        url_key=url_key,
    )

    pkt = _blind.packet_from_dict(json.loads(Path(packet).read_text(encoding="utf-8")))
    sheet = _labeling.build_labeling_sheet(pkt.entries, ctx)

    Path(out_json).parent.mkdir(parents=True, exist_ok=True)
    Path(out_json).write_text(json.dumps(sheet, ensure_ascii=False, indent=2), encoding="utf-8")

    covered = sum(1 for r in sheet if r["overview"])
    if out_md:
        header = ""
        if card:
            card_dict = _yaml.safe_load(Path(card).read_text(encoding="utf-8"))
            header = _labeling.product_header_from_card(card_dict, lang=lang)
        md = _labeling.render_worksheet_md(
            pair=pair, product_header=header, sheet=sheet, lang=lang
        )
        Path(out_md).write_text(md, encoding="utf-8")

    _print_json({
        "ok": True, "sheet_path": out_json, "worksheet_path": out_md,
        "companies": len(sheet), "with_overview": covered,
    })


@benchmark_app.command("seal-labels")
def benchmark_seal_labels_cmd(
    sheet: str = typer.Option(..., "--sheet", help="Filled labeling sheet JSON."),
    packet: str = typer.Option(..., "--packet", help="The company packet the sheet was built from."),
    out: str = typer.Option(..., "--out", "-o", help="Sealed labels JSON path."),
    allow_partial: bool = typer.Option(False, "--allow-partial", help="Permit unlabeled rows."),
) -> None:
    """Seal a filled labeling sheet into sealed company labels (state machine step 5)."""
    from pathlib import Path

    from event_intel.eval import blind as _blind
    from event_intel.eval import labeling as _labeling

    rows = json.loads(Path(sheet).read_text(encoding="utf-8"))
    labels, grades, provenance = _labeling.extract_sealed_inputs(
        rows, require_all=not allow_partial
    )
    pkt = _blind.packet_from_dict(json.loads(Path(packet).read_text(encoding="utf-8")))
    sealed = _blind.seal_company_labels(pkt, labels, grades=grades, provenance=provenance)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(
        json.dumps(_blind.sealed_labels_to_dict(sealed), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    gold_n = sum(1 for g in sealed.grades.values() if g == "gold")
    _print_json({
        "ok": True, "sealed_labels_path": out, "labeled": len(labels),
        "gold": gold_n, "sha": sealed.sha,
    })


@benchmark_app.command("independent-view")
def benchmark_independent_view_cmd(
    sheet: str = typer.Option(..., "--sheet", help="GPT-drafted sheet JSON."),
    out: str = typer.Option(..., "--out", "-o", help="GPT-blind view JSON for the 2nd vendor."),
) -> None:
    """Emit the GPT-blind view (name+overview+url only) + its SHA, for an
    independent 2nd-vendor labeling pass (cross-vendor gold, review R2#5).
    """
    from pathlib import Path

    from event_intel.eval import label_refine as _refine

    rows = json.loads(Path(sheet).read_text(encoding="utf-8"))
    view = _refine.independent_input_view(rows)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_json({"ok": True, "view_path": out, "input_sha": _refine.input_sha(view), "rows": len(view)})


@benchmark_app.command("cross-vendor")
def benchmark_cross_vendor_cmd(
    sheet: str = typer.Option(..., "--sheet", help="GPT-drafted sheet JSON."),
    claude_labels: str = typer.Option(..., "--claude-labels", help="Independent {name: label} JSON."),
    input_sha: str = typer.Option(..., "--input-sha", help="SHA of the GPT-blind view the 2nd vendor saw."),
    prompt_sha: str = typer.Option("", "--prompt-sha"),
    model_id: str = typer.Option("claude", "--model-id"),
    out: str = typer.Option(..., "--out", "-o"),
) -> None:
    """Promote rows to gold where the GPT draft and the independent Claude label
    agree (refuses if the input SHA proves the 2nd vendor saw GPT fields).
    """
    from pathlib import Path

    from event_intel.eval import label_refine as _refine

    rows = json.loads(Path(sheet).read_text(encoding="utf-8"))
    labels = json.loads(Path(claude_labels).read_text(encoding="utf-8"))
    merged = _refine.merge_cross_vendor(
        rows, labels, independent_input_sha=input_sha, prompt_sha=prompt_sha, model_id=model_id
    )
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    gold = sum(1 for r in merged if r.get("grade") == "gold")
    _print_json({"ok": True, "sheet_path": out, "gold": gold, "needs_review": len(merged) - gold})


@benchmark_app.command("apply-refinements")
def benchmark_apply_refinements_cmd(
    sheet: str = typer.Option(..., "--sheet", help="Flagged sheet JSON."),
    refinements: str = typer.Option(..., "--refinements", help="Host search output {name:{final_label,evidence_urls,note}}."),
    out: str = typer.Option(..., "--out", "-o"),
) -> None:
    """Merge host (Claude+web search) refinements into flagged rows as gold."""
    from pathlib import Path

    from event_intel.eval import label_refine as _refine

    rows = json.loads(Path(sheet).read_text(encoding="utf-8"))
    refs = json.loads(Path(refinements).read_text(encoding="utf-8"))
    merged = _refine.apply_refinements(rows, refs)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    refined = sum(1 for r in merged if r.get("source") == "search_refine")
    _print_json({"ok": True, "sheet_path": out, "refined": refined})


@benchmark_app.command("label-stats")
def benchmark_label_stats_cmd(
    sheet: str = typer.Option(..., "--sheet", help="A drafted/refined sheet JSON."),
    out: str | None = typer.Option(None, "--out", "-o", help="Write the stats JSON here too."),
) -> None:
    """Labeling-process meta-metrics — gold/flag/flip rates ('how trustworthy is
    this gold?').
    """
    from pathlib import Path

    from event_intel.eval import label_refine as _refine

    rows = json.loads(Path(sheet).read_text(encoding="utf-8"))
    stats = _refine.label_stats(rows)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_json({"ok": True, **stats})


def main() -> None:
    """Module entrypoint for `python -m event_intel.cli`."""
    app()


if __name__ == "__main__":
    main()
