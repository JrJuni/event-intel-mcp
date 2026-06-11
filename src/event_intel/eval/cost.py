"""Y1D D0 — aggregate per-run LLM usage (run_summary.llm_usage) into a cost table.

Input: any directory tree containing ``run_summary.json`` files (benchmark run
dirs, production output dirs). Each summary may carry the ``llm_usage`` block
written by ``runtime.llm_ledger`` — runs predating D0 (or with an emitter
failure) are counted as ``runs_without_usage`` instead of being silently
dropped (no-silent-caps rule).

Output: per-run rows + grand totals, with the reference-model USD conversion
re-summed from the per-run blocks. stdlib-only (cold-import safe).
"""
from __future__ import annotations

import json
from pathlib import Path

COST_SCHEMA = "llm-cost/v1"


def collect_run_summaries(root: Path) -> list[Path]:
    """All run_summary.json files under ``root`` (recursive), sorted for
    deterministic aggregation order. A single file path is accepted too.
    """
    if root.is_file():
        return [root] if root.name == "run_summary.json" else []
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("run_summary.json") if p.is_file())


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def aggregate_costs(paths: list[Path]) -> dict:
    """Fold run summaries into {runs: [...], totals: {...}} cost evidence."""
    runs: list[dict] = []
    skipped_unreadable = 0
    runs_without_usage = 0
    total_tokens = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
    total_costs: dict[str, float] = {}
    for path in paths:
        data = _load(path)
        if data is None:
            skipped_unreadable += 1
            continue
        usage = data.get("llm_usage")
        if not isinstance(usage, dict):
            runs_without_usage += 1
            continue
        totals = usage.get("totals") or {}
        costs = usage.get("reference_costs_usd") or {}
        runs.append({
            "run_id": data.get("run_id"),
            "pair": data.get("pair"),
            "provider": data.get("provider"),
            "path": str(path),
            "calls": int(totals.get("calls") or 0),
            "input_tokens": int(totals.get("input_tokens") or 0),
            "output_tokens": int(totals.get("output_tokens") or 0),
            "stages": {
                s: {"calls": e.get("calls", 0),
                    "input_tokens": e.get("input_tokens", 0),
                    "output_tokens": e.get("output_tokens", 0)}
                for s, e in (usage.get("stages") or {}).items()
            },
            "reference_costs_usd": {
                m: c.get("total_usd", 0.0) for m, c in costs.items()
            },
        })
        for key in total_tokens:
            total_tokens[key] += int(totals.get(key) or 0)
        for model, c in costs.items():
            total_costs[model] = round(
                total_costs.get(model, 0.0) + float(c.get("total_usd") or 0.0), 6
            )
    return {
        "schema": COST_SCHEMA,
        "runs": runs,
        "totals": {**total_tokens, "reference_costs_usd": total_costs},
        "files_scanned": len(paths),
        "runs_without_usage": runs_without_usage,
        "skipped_unreadable": skipped_unreadable,
    }


def render_cost_table(agg: dict) -> str:
    """Human-readable per-run cost table (markdown). Stable column order."""
    models = sorted({
        m for run in agg.get("runs", []) for m in run.get("reference_costs_usd", {})
    })
    header = ["run_id", "pair", "calls", "in_tok", "out_tok", *models]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for run in agg.get("runs", []):
        cells = [
            str(run.get("run_id") or "?"),
            str(run.get("pair") or "-"),
            str(run.get("calls")),
            f"{run.get('input_tokens'):,}",
            f"{run.get('output_tokens'):,}",
            *(f"${run['reference_costs_usd'].get(m, 0.0):.4f}" for m in models),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    totals = agg.get("totals", {})
    total_costs = totals.get("reference_costs_usd", {})
    lines.append(
        "| **total** | - | "
        f"{totals.get('calls', 0)} | {totals.get('input_tokens', 0):,} | "
        f"{totals.get('output_tokens', 0):,} | "
        + " | ".join(f"**${total_costs.get(m, 0.0):.4f}**" for m in models)
        + " |"
    )
    if agg.get("runs_without_usage"):
        lines.append(
            f"\n{agg['runs_without_usage']} run(s) had no llm_usage block "
            "(pre-D0 or emitter failure) — excluded from totals."
        )
    return "\n".join(lines)
