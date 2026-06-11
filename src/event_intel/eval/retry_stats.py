"""R1 — aggregate failure-pattern events into retry-policy evidence.

Input: ``search_failures.jsonl`` (and later ``fetch_failures.jsonl``, B1) files
written by the enrichment layer — one event per live search with
``outcome ∈ {ok, recovered, degraded, no_results, error}`` and ``attempts``.

Output: anonymous counts only (no queries/company names) — "which exception
class / backend / kind recovers at which attempt" — the evidence base the R3
retry policy is codified from after the R2 smoke campaign (>= 10 runs).

Silver diagnostics, NOT an accuracy gate. stdlib-only (cold-import safe).
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

RETRY_STATS_SCHEMA = "retry-stats/v1"

_OUTCOMES = ("ok", "recovered", "degraded", "no_results", "error")


def collect_event_files(root: Path) -> list[Path]:
    """All diagnostics JSONL files under ``root`` (recursive), sorted for
    deterministic aggregation order.
    """
    if not root.is_dir():
        return []
    return sorted(p for p in root.rglob("*.jsonl") if p.is_file())


def load_events(paths: Iterable[Path]) -> list[dict]:
    """Tolerant JSONL reader: bad lines / non-dict rows are skipped (a half
    written diagnostics line must never break the report).
    """
    events: list[dict] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                events.append(row)
    return events


def aggregate(events: list[dict], *, files_scanned: int = 0) -> dict:
    """Fold events into the retry-policy evidence table.

    - ``by_outcome`` — overall health split.
    - ``recovered_attempts_hist`` — for recovered queries, how many tries it
      took ("does attempt 4 ever pay off?" → the R3 ceiling question).
    - ``degraded_attempts_hist`` — what ceiling the degraded ones hit.
    - ``exc_class_outcomes`` — per exception class, recovered vs degraded
      (which failure shapes are worth retrying at all).
    - ``by_backend`` / ``by_kind`` — lane health.
    """
    by_outcome: Counter[str] = Counter()
    recovered_attempts: Counter[int] = Counter()
    degraded_attempts: Counter[int] = Counter()
    exc_outcomes: dict[str, Counter[str]] = {}
    by_backend: dict[str, Counter[str]] = {}
    by_kind: dict[str, Counter[str]] = {}

    for ev in events:
        outcome = str(ev.get("outcome", "")) or "unknown"
        by_outcome[outcome] += 1
        attempts = ev.get("attempts")
        if outcome == "recovered" and isinstance(attempts, int):
            recovered_attempts[attempts] += 1
        if outcome == "degraded" and isinstance(attempts, int):
            degraded_attempts[attempts] += 1
        # Count each exception CLASS once per event — the unit is "a query that
        # saw this failure shape", not raw occurrence count.
        for exc in {str(x) for x in ev.get("exc_classes") or []}:
            exc_outcomes.setdefault(exc, Counter())[outcome] += 1
        backend = ev.get("backend")
        if backend:
            by_backend.setdefault(str(backend), Counter())[outcome] += 1
        kind = ev.get("kind")
        if kind:
            by_kind.setdefault(str(kind), Counter())[outcome] += 1

    def _hist(c: Counter[int]) -> dict[str, int]:
        return {str(k): c[k] for k in sorted(c)}

    def _nested(d: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
        return {k: dict(sorted(v.items())) for k, v in sorted(d.items())}

    total = len(events)
    retried = by_outcome["recovered"] + by_outcome["degraded"]
    return {
        "schema": RETRY_STATS_SCHEMA,
        "grade": "silver",  # diagnostics, not a gate
        "files_scanned": files_scanned,
        "total_events": total,
        "by_outcome": {k: by_outcome[k] for k in _OUTCOMES if by_outcome[k]}
        | ({"unknown": by_outcome["unknown"]} if by_outcome["unknown"] else {}),
        "retry_recovery_rate": (
            round(by_outcome["recovered"] / retried, 3) if retried else None
        ),
        "recovered_attempts_hist": _hist(recovered_attempts),
        "degraded_attempts_hist": _hist(degraded_attempts),
        "exc_class_outcomes": _nested(exc_outcomes),
        "by_backend": _nested(by_backend),
        "by_kind": _nested(by_kind),
    }
