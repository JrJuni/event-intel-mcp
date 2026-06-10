"""Batch smoke runner for the BD critique harness (silver DEV diagnostics).

Runs many (product workspace × event source) pairs through the engine and
collects each tier list + run summary into one batch dir for downstream
critique. Partial failures are ISOLATED — one pair's failure never aborts the
batch. This is an orthogonal diagnostic lane: it contains NO scoring logic and
never touches final_score / tier.

stdlib + an INJECTED ``build_fn`` at import (cold-start safe; the engine + yaml
are imported lazily by the caller / inside functions).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from event_intel.errors import ErrorCode, MCPError, Stage

_VALID_SOURCE_KINDS = (
    "html_file", "csv_file", "text_file", "html_text", "text", "url",
)


@dataclass
class PairSpec:
    pair: str  # stable slug id for this product×event pair
    workspace: str = "default"
    event_name: str = ""
    event_slug: str = ""
    source_kind: str = "html_file"
    source_ref: str = ""
    lang: str = "en"
    target_mode: str | None = None


@dataclass
class PairResult:
    pair: str
    ok: bool
    tier_list_path: str | None = None
    run_summary_path: str | None = None
    tier_counts: dict[str, int] = field(default_factory=dict)
    error: str | None = None


def load_pair_specs(path: str | Path) -> list[PairSpec]:
    """Load a batch spec YAML: a list under ``pairs:`` (or a bare list) of
    ``{pair, workspace, event_name, event_slug, source_kind, source_ref, lang,
    target_mode}``. Raises INVALID_INPUT on an empty/invalid spec.
    """
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = data.get("pairs", []) if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise MCPError(
            error_code=ErrorCode.INVALID_INPUT,
            stage=Stage.PREFLIGHT,
            message="batch spec must list one or more pairs",
            hint={"fix": "Provide a YAML with `pairs: [{pair, workspace, source_kind, source_ref, ...}]`"},
        )
    specs: list[PairSpec] = []
    for item in raw:
        pair = str(item.get("pair") or item.get("event_slug") or "").strip()
        if not pair:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.PREFLIGHT,
                message="each pair needs a `pair` (or `event_slug`) id",
                hint={"item": item},
            )
        kind = str(item.get("source_kind", "html_file"))
        if kind not in _VALID_SOURCE_KINDS:
            raise MCPError(
                error_code=ErrorCode.INVALID_INPUT,
                stage=Stage.PREFLIGHT,
                message=f"invalid source_kind {kind!r} for pair {pair!r}",
                hint={"allowed": list(_VALID_SOURCE_KINDS)},
            )
        specs.append(
            PairSpec(
                pair=pair,
                workspace=str(item.get("workspace", "default")),
                event_name=str(item.get("event_name", "")),
                event_slug=str(item.get("event_slug", pair)),
                source_kind=kind,
                source_ref=str(item.get("source_ref", "")),
                lang=str(item.get("lang", "en")),
                target_mode=item.get("target_mode"),
            )
        )
    return specs


def _collect_one(
    spec: PairSpec,
    res: Any,
    pair_dir: Path,
    copy_fn: Callable[[str, Path], None],
) -> PairResult:
    if not isinstance(res, dict) or not res.get("ok"):
        msg = res.get("message") if isinstance(res, dict) else "build returned non-dict"
        return PairResult(pair=spec.pair, ok=False, error=msg or "build failed")
    pair_dir.mkdir(parents=True, exist_ok=True)
    tl_src = res.get("tier_list_yaml_path")
    rs_src = res.get("run_summary_path")
    tl_dst = rs_dst = None
    if tl_src and Path(tl_src).is_file():
        tl_dst = pair_dir / "tier_list.yaml"
        copy_fn(tl_src, tl_dst)
    if rs_src and Path(rs_src).is_file():
        rs_dst = pair_dir / "run_summary.json"
        copy_fn(rs_src, rs_dst)
    return PairResult(
        pair=spec.pair,
        ok=tl_dst is not None,
        tier_list_path=str(tl_dst) if tl_dst else None,
        run_summary_path=str(rs_dst) if rs_dst else None,
        tier_counts=dict(res.get("tier_counts", {})),
        error=None if tl_dst else "build ok but produced no tier_list.yaml",
    )


def run_smoke_batch(
    specs: list[PairSpec],
    *,
    build_fn: Callable[[PairSpec], dict[str, Any]],
    out_root: str | Path,
    batch_id: str,
    copy_fn: Callable[[str, Path], None] | None = None,
) -> dict[str, Any]:
    """Run each pair through ``build_fn`` and collect outputs under
    ``out_root/batch_id/<pair>/``. One pair's failure (exception OR ok=False
    envelope) is recorded and skipped — the batch always completes. Writes a
    ``batch.json`` manifest and returns the summary dict.
    """
    import shutil

    if copy_fn is None:
        copy_fn = lambda src, dst: shutil.copyfile(src, dst)  # noqa: E731
    batch_dir = Path(out_root) / batch_id
    results: list[PairResult] = []
    for spec in specs:
        try:
            res = build_fn(spec)
        except Exception as exc:  # noqa: BLE001 — isolate per-pair failure
            results.append(
                PairResult(pair=spec.pair, ok=False, error=f"{type(exc).__name__}: {exc}")
            )
            continue
        results.append(_collect_one(spec, res, batch_dir / spec.pair, copy_fn))

    summary = _summarize(batch_id, str(batch_dir), results)
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "batch.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _summarize(batch_id: str, batch_dir: str, results: list[PairResult]) -> dict[str, Any]:
    tier_totals: dict[str, int] = {}
    for r in results:
        for tier, n in (r.tier_counts or {}).items():
            tier_totals[tier] = tier_totals.get(tier, 0) + int(n)
    return {
        "batch_id": batch_id,
        "batch_dir": batch_dir,
        "n": len(results),
        "n_ok": sum(1 for r in results if r.ok),
        "n_failed": sum(1 for r in results if not r.ok),
        "tier_totals": tier_totals,
        "results": [
            {
                "pair": r.pair,
                "ok": r.ok,
                "tier_list_path": r.tier_list_path,
                "run_summary_path": r.run_summary_path,
                "tier_counts": r.tier_counts,
                "error": r.error,
            }
            for r in results
        ],
    }
