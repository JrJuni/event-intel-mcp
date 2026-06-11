"""Append-only JSONL diagnostics sink for failure-pattern events (news plan R1).

Records one event per live search (later: per body fetch, B1) so the R2 smoke
campaign can measure "which exception/backend/site shape recovers at which
attempt" — the evidence base for the R3 retry policy. Strictly best-effort:
diagnostics I/O must never break a build. stdlib-only (cold-import safe).

Events live under the data root (gitignored user dir), e.g.
``~/.event-intel/diagnostics/{workspace}/search_failures.jsonl``. Query strings
contain company names — fine for a local diagnostics file, but the aggregate
report (``benchmark retry-stats``) only emits anonymous counts.
"""
from __future__ import annotations

import json
from pathlib import Path


class FailureLog:
    """Best-effort JSONL appender. ``base_fields`` are merged into every event
    (e.g. workspace) so writers don't have to thread context through call sites.
    """

    def __init__(self, path: Path | str, *, base_fields: dict | None = None) -> None:
        self.path = Path(path)
        self.base_fields = dict(base_fields or {})
        self._mkdir_failed = False

    def append(self, event: dict) -> None:
        if self._mkdir_failed:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._mkdir_failed = True  # don't retry mkdir on every event
            return
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({**self.base_fields, **event}, ensure_ascii=False) + "\n")
        except OSError:
            pass  # diagnostics are best-effort

    def append_all(self, events: list[dict]) -> None:
        for ev in events:
            self.append(ev)
