"""Benchmark run/measure split — Y1 CS4.

The blind boundary (design v4 §2 steps 3 & 9, review R1#1) is enforced as TWO
functions that cannot see each other's inputs:

  run     — the *hidden run* (step 3). Invokes the engine, persists an immutable,
            gold-blind run-result. Its signature carries NO gold path, and it
            refuses to persist a payload that smells of gold (labels/verdicts) —
            so a wiring mistake can't smuggle gold into the run dir.
  measure — the *reveal + join* (step 9), a SEPARATE process. Takes the
            run-result + sealed company labels + sealed evidence verdicts +
            roster match, projects everything onto roster_id space, and computes
            the CS6 metric table. It refuses unsealed labels (TypeError) — you
            cannot measure against gold that was never frozen.

Pure stdlib + cold eval imports (metrics / roster / blind) — import-cold,
regression-guarded by tests/test_mcp_cold_start.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from event_intel.eval import blind as _blind
from event_intel.eval import metrics as _metrics
from event_intel.eval import roster as _roster

# Top-level keys that, if present in a run-result payload, mean gold leaked into
# the (supposedly blind) run. `run` refuses to persist such a payload.
_GOLD_KEYS = frozenset(
    {"labels", "sealed_labels", "verdicts", "sealed_verdicts", "gold", "gold_labels"}
)

# Which gold classes count as a "good pick" per target_mode (overridable). Grounded
# in the gold vocab target|competitor|bad_fit|neutral: ecosystem treats competitor
# as positive, partner keeps it neutral (see metrics mode policy).
_POSITIVE_BY_MODE = {
    "customer": frozenset({"target"}),
    "partner": frozenset({"target"}),
    "ecosystem": frozenset({"target", "competitor"}),
}

# D6 pre-frozen gate thresholds (plan §6). (metric_name, direction, threshold).
# CS8's threshold-freeze manifest overrides these; kept here as the documented default.
DEFAULT_GATES: tuple[tuple[str, str, float], ...] = (
    ("extraction_coverage", ">=", 0.80),
    ("precision_at_10", ">=", 0.60),
    ("conditional_competitor_leakage_rate", "<=", 0.10),
    ("conditional_bad_fit_leakage_rate", "<=", 0.05),
    ("evidence_precision", ">=", 0.85),
)


# ============================================================================
# run — the gold-blind hidden run (step 3)
# ============================================================================


@dataclass
class RunResult:
    """The immutable, gold-blind run-result persisted by `run` and reloaded by
    `measure`. `scored`/`tiers` are keyed by EXTRACTED name (roster_id projection
    happens in measure, via the CS2 match).
    """
    pair: str
    run_id: str
    run_fingerprint: str
    scored: list[tuple[str, float]]          # (extracted_name, final_score)
    tiers: dict[str, str]                    # extracted_name -> tier
    top10_evidence: list[dict[str, Any]] = field(default_factory=list)
    run_summary: dict[str, Any] = field(default_factory=dict)


def _assert_gold_blind(payload: dict[str, Any]) -> None:
    leaked = sorted(k for k in payload if k in _GOLD_KEYS)
    if leaked:
        raise ValueError(
            f"run-result payload contains gold fields {leaked}: the hidden run "
            "must be blind to gold (design v4 §2 step 3 / review R1#1)"
        )


def _run_result_from_payload(pair: str, payload: dict[str, Any]) -> RunResult:
    companies = payload.get("companies", [])
    scored = [(c["name"], float(c["final_score"])) for c in companies]
    tiers = {c["name"]: c["tier"] for c in companies}
    return RunResult(
        pair=pair,
        run_id=payload["run_id"],
        run_fingerprint=payload["run_fingerprint"],
        scored=scored,
        tiers=tiers,
        top10_evidence=list(payload.get("top10_evidence", [])),
        run_summary=payload,
    )


def _atomic_write(path: Path, text: str, *, allow_overwrite: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not allow_overwrite:
        raise FileExistsError(f"run-result already exists (immutable run): {path}")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".run_result.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        Path(tmp).replace(path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(
    *,
    pair: str,
    build_fn: Callable[[], dict[str, Any]],
    runs_root: str | Path,
    allow_overwrite: bool = False,
) -> Path:
    """Hidden run (step 3): invoke the engine via `build_fn`, persist an immutable
    gold-blind run-result under ``runs_root/<pair>/<run_id>/run_result.json``.

    `build_fn` returns the engine's run_summary dict (CS1 emits it). There is
    deliberately NO gold parameter — the blind boundary is structural (R1#1).
    Returns the run directory.
    """
    payload = build_fn()
    _assert_gold_blind(payload)
    result = _run_result_from_payload(pair, payload)

    run_dir = Path(runs_root) / pair / result.run_id
    record = {
        "pair": result.pair,
        "run_id": result.run_id,
        "run_fingerprint": result.run_fingerprint,
        "run_summary": result.run_summary,
        "top10_evidence": result.top10_evidence,
    }
    _atomic_write(
        run_dir / "run_result.json",
        json.dumps(record, ensure_ascii=False, indent=2),
        allow_overwrite=allow_overwrite,
    )
    return run_dir


def load_run_result(run_dir: str | Path) -> RunResult:
    """Reload a persisted run-result (the only run→measure handoff)."""
    record = json.loads((Path(run_dir) / "run_result.json").read_text(encoding="utf-8"))
    rr = _run_result_from_payload(record["pair"], record["run_summary"])
    rr.top10_evidence = list(record.get("top10_evidence", []))
    return rr


# ============================================================================
# threshold manifest — frozen BEFORE any labels are seen (step 1)
# ============================================================================


def freeze_thresholds(
    *,
    gates: tuple[tuple[str, str, float], ...] = DEFAULT_GATES,
    universe: dict[str, Any] | None = None,
    now_iso: str,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Freeze the D6 gate thresholds + per-pair universe into an immutable manifest
    (state machine step 1). The `sha` covers only the frozen content (gates +
    universe), NOT `frozen_at`, so the freeze is provable and re-derivable. Writing
    refuses to overwrite — a freeze happens once, before any label is seen.
    """
    content = {"gates": [list(g) for g in gates], "universe": universe or {}}
    sha = hashlib.sha256(
        json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    manifest = {**content, "sha": sha, "frozen_at": now_iso}
    if path is not None:
        _atomic_write(
            Path(path),
            json.dumps(manifest, ensure_ascii=False, indent=2),
            allow_overwrite=False,
        )
    return manifest


def load_threshold_manifest(
    path: str | Path,
) -> tuple[tuple[tuple[str, str, float], ...], dict[str, Any]]:
    """Load a frozen manifest → (gates tuple, full manifest dict)."""
    m = json.loads(Path(path).read_text(encoding="utf-8"))
    gates = tuple((g[0], g[1], float(g[2])) for g in m["gates"])
    return gates, m


# ============================================================================
# measure — reveal + join + metrics (step 9)
# ============================================================================


@dataclass
class GateOutcome:
    name: str
    result: _metrics.MetricResult
    threshold: float | None
    direction: str               # ">=" or "<="
    passed: bool | None          # None when status != OK (N/A / insufficient_n: not gated)


@dataclass
class MeasureReport:
    pair: str
    run_id: str
    run_fingerprint: str
    target_mode: str
    metrics: dict[str, _metrics.MetricResult]
    gates: list[GateOutcome]

    def gate_failures(self) -> list[GateOutcome]:
        return [g for g in self.gates if g.passed is False]

    def passed(self) -> bool:
        return not self.gate_failures()

    def to_dict(self) -> dict[str, Any]:
        def _m(r: _metrics.MetricResult) -> dict[str, Any]:
            return {"value": r.value, "status": r.status, "n": r.n}

        return {
            "pair": self.pair,
            "run_id": self.run_id,
            "run_fingerprint": self.run_fingerprint,
            "target_mode": self.target_mode,
            "metrics": {k: _m(v) for k, v in self.metrics.items()},
            "gates": [
                {
                    "name": g.name,
                    "threshold": g.threshold,
                    "direction": g.direction,
                    "passed": g.passed,
                    "value": g.result.value,
                    "status": g.result.status,
                    "n": g.result.n,
                }
                for g in self.gates
            ],
            "passed": self.passed(),
        }


def _project(
    run_result: RunResult,
    roster: list[_roster.RosterEntry],
    match: _roster.MatchResult,
    sealed_labels: _blind.SealedLabels | None,
) -> tuple[list[tuple[str, float]], dict[str, str], dict[str, str], set[str]]:
    """Project the (name-keyed) run-result onto roster_id space via the match,
    and resolve gold labels (sealed user labels first, roster intrinsic fill).
    """
    score_by_name = dict(run_result.scored)
    scored_by_rid = [
        (rid, score_by_name[name])
        for rid, name in match.matched.items()
        if name in score_by_name
    ]
    tiers_by_rid = {
        rid: run_result.tiers[name]
        for rid, name in match.matched.items()
        if name in run_result.tiers
    }
    scored_ids = set(match.matched)  # materialized roster_ids only (R3-4)

    canon_to_rid = {e.canonical_name: e.roster_id for e in roster}
    labels_by_rid: dict[str, str] = {}
    if sealed_labels is not None:
        for name, lab in sealed_labels.labels.items():
            rid = canon_to_rid.get(name)
            if rid:
                labels_by_rid[rid] = lab
    for e in roster:  # roster intrinsic labels fill any gaps (full-roster cohort)
        if e.label and e.roster_id not in labels_by_rid:
            labels_by_rid[e.roster_id] = e.label
    return scored_by_rid, tiers_by_rid, labels_by_rid, scored_ids


def _evaluate_gates(
    metrics: dict[str, _metrics.MetricResult],
    gates: tuple[tuple[str, str, float], ...],
) -> list[GateOutcome]:
    out: list[GateOutcome] = []
    for name, direction, threshold in gates:
        r = metrics.get(name)
        if r is None:
            continue
        if r.status != _metrics.OK or r.value is None:
            passed: bool | None = None  # N/A or insufficient_n → not gated, reported
        elif direction == ">=":
            passed = r.value >= threshold
        else:
            passed = r.value <= threshold
        out.append(GateOutcome(name, r, threshold, direction, passed))
    return out


def measure(
    *,
    run_result: RunResult,
    roster: list[_roster.RosterEntry],
    match: _roster.MatchResult,
    sealed_labels: _blind.SealedLabels,
    sealed_verdicts: _blind.SealedVerdicts | None = None,
    target_mode: str = "customer",
    positive: set[str] | None = None,
    evidence_present: list[bool] | None = None,
    thresholds: tuple[tuple[str, str, float], ...] | None = None,
) -> MeasureReport:
    """Reveal + join (step 9). Joins the run-result with sealed gold and the CS2
    match, then computes the CS6 metric table + gate outcomes.

    `sealed_labels` MUST be a frozen `SealedLabels` — measuring against unsealed
    labels would break the blind state machine, so a raw dict is rejected.
    Partner mode zeroes the competitor metrics to N/A (competitor is neutral).
    """
    if not isinstance(sealed_labels, _blind.SealedLabels):
        raise TypeError(
            "measure requires SEALED company labels (eval.blind.SealedLabels); "
            "unsealed gold breaks the blind state machine (design v4 §2 step 5)"
        )

    positive = set(positive) if positive is not None else set(
        _POSITIVE_BY_MODE.get(target_mode, _POSITIVE_BY_MODE["customer"])
    )
    scored_by_rid, tiers_by_rid, labels_by_rid, scored_ids = _project(
        run_result, roster, match, sealed_labels
    )

    metrics: dict[str, _metrics.MetricResult] = {
        "extraction_coverage": _roster.coverage(match, roster),
        "mention_coverage": _roster.mention_coverage(match, roster),
        "precision_at_10": _metrics.precision_at_k(
            scored_by_rid, labels_by_rid, positive, k=10
        ),
    }

    # competitor trio — N/A under partner mode (competitor is neutral there).
    na = _metrics.MetricResult(None, _metrics.NA)
    if target_mode == "partner":
        metrics["end_to_end_competitor_selection_rate"] = na
        metrics["conditional_competitor_leakage_rate"] = na
        metrics["competitor_extraction_coverage"] = na
    else:
        metrics["end_to_end_competitor_selection_rate"] = _metrics.end_to_end_selection_rate(
            tiers_by_rid, labels_by_rid, klass="competitor"
        )
        metrics["conditional_competitor_leakage_rate"] = _metrics.conditional_leakage_rate(
            tiers_by_rid, labels_by_rid, scored_ids, klass="competitor"
        )
        metrics["competitor_extraction_coverage"] = _metrics.class_extraction_coverage(
            labels_by_rid, scored_ids, klass="competitor"
        )

    # bad_fit trio — negative in every mode.
    metrics["end_to_end_bad_fit_selection_rate"] = _metrics.end_to_end_selection_rate(
        tiers_by_rid, labels_by_rid, klass="bad_fit"
    )
    metrics["conditional_bad_fit_leakage_rate"] = _metrics.conditional_leakage_rate(
        tiers_by_rid, labels_by_rid, scored_ids, klass="bad_fit"
    )
    metrics["bad_fit_extraction_coverage"] = _metrics.class_extraction_coverage(
        labels_by_rid, scored_ids, klass="bad_fit"
    )

    # evidence
    verdicts = list(sealed_verdicts.verdicts) if sealed_verdicts is not None else []
    metrics["evidence_precision"] = _metrics.evidence_precision(verdicts)
    metrics["evidence_yield"] = (
        _metrics.evidence_yield(evidence_present)
        if evidence_present is not None
        else _metrics.MetricResult(None, _metrics.NA)
    )

    # AUC (target vs bad_fit, full-label only)
    auc = _metrics.ranking_accuracy_auc(scored_by_rid, labels_by_rid)
    metrics["auc"] = (
        _metrics.MetricResult(auc, _metrics.OK)
        if auc is not None
        else _metrics.MetricResult(None, _metrics.NA)
    )

    gates = _evaluate_gates(metrics, thresholds or DEFAULT_GATES)
    return MeasureReport(
        pair=run_result.pair,
        run_id=run_result.run_id,
        run_fingerprint=run_result.run_fingerprint,
        target_mode=target_mode,
        metrics=metrics,
        gates=gates,
    )
