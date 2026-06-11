"""R1 — failure-pattern instrumentation (FailureLog + enrichment wiring) and
the `benchmark retry-stats` aggregation. Silver diagnostics, no live network.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from event_intel.cli import app
from event_intel.eval.retry_stats import aggregate, collect_event_files, load_events
from event_intel.events.enrichment import enrich_exhibitors
from event_intel.events.extraction import ExhibitorCandidate
from event_intel.runtime.failure_log import FailureLog

runner = CliRunner()


# ---------- FailureLog ----------


def test_failure_log_appends_jsonl_with_base_fields(tmp_path):
    log = FailureLog(tmp_path / "d" / "s.jsonl", base_fields={"workspace": "w1"})
    log.append({"outcome": "ok", "attempts": 1})
    log.append({"outcome": "degraded", "attempts": 6})
    rows = [
        json.loads(ln)
        for ln in (tmp_path / "d" / "s.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0] == {"workspace": "w1", "outcome": "ok", "attempts": 1}
    assert rows[1]["outcome"] == "degraded" and rows[1]["workspace"] == "w1"


def test_failure_log_is_best_effort_on_unwritable_path(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir", encoding="utf-8")
    log = FailureLog(blocker / "sub" / "s.jsonl")  # parent mkdir will fail
    log.append({"outcome": "ok"})  # must not raise
    log.append({"outcome": "ok"})  # mkdir not retried, still no raise


# ---------- aggregation ----------


def _ev(outcome, attempts=1, excs=(), backend="auto", kind="news"):
    return {
        "outcome": outcome, "attempts": attempts, "exc_classes": list(excs),
        "backend": backend, "kind": kind,
    }


def test_aggregate_histograms_and_recovery_rate():
    events = [
        _ev("ok"),
        _ev("recovered", attempts=3, excs=["DDGSException", "DDGSException"]),
        _ev("recovered", attempts=2, excs=["TimeoutException"]),
        _ev("degraded", attempts=6, excs=["DDGSException"] * 6),
        _ev("no_results"),
        _ev("error", excs=["RuntimeError"], kind="web"),
    ]
    stats = aggregate(events, files_scanned=2)
    assert stats["schema"] == "retry-stats/v1" and stats["grade"] == "silver"
    assert stats["total_events"] == 6 and stats["files_scanned"] == 2
    assert stats["by_outcome"] == {
        "ok": 1, "recovered": 2, "degraded": 1, "no_results": 1, "error": 1,
    }
    assert stats["retry_recovery_rate"] == round(2 / 3, 3)
    assert stats["recovered_attempts_hist"] == {"2": 1, "3": 1}
    assert stats["degraded_attempts_hist"] == {"6": 1}
    assert stats["exc_class_outcomes"]["DDGSException"] == {"degraded": 1, "recovered": 1}
    assert stats["by_kind"]["web"] == {"error": 1}


def test_aggregate_empty_is_graceful():
    stats = aggregate([])
    assert stats["total_events"] == 0
    assert stats["retry_recovery_rate"] is None
    assert stats["by_outcome"] == {}


def test_load_events_skips_bad_lines_and_collect_handles_missing_root(tmp_path):
    p = tmp_path / "diag" / "s.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(
        '{"outcome": "ok", "attempts": 1}\nnot-json\n[1,2]\n\n{"outcome": "degraded"}\n',
        encoding="utf-8",
    )
    files = collect_event_files(tmp_path / "diag")
    assert files == [p]
    events = load_events(files)
    assert [e["outcome"] for e in events] == ["ok", "degraded"]
    assert collect_event_files(tmp_path / "nope") == []


# ---------- enrichment wiring ----------


def _config():
    return {
        "enrichment": {
            "max_companies": 30, "count_web": 5, "count_news": 5,
            "news_days_back": 180, "cache_enabled": True,
            "official_url_levenshtein_threshold": 0.4,
        },
    }


class _EventedFake:
    """Fake provider that emits one R1 event per live search (ddgs contract)."""

    def __init__(self):
        self.last_call_degraded = False
        self._events: list[dict] = []
        self.calls = 0

    def search(self, query, *, kind, count, days=None, lang="en"):
        self.calls += 1
        self._events.append({
            "provider": "fake", "backend": "auto", "kind": kind,
            "outcome": "ok", "attempts": 1, "exc_classes": [],
        })
        return []

    def drain_events(self):
        events, self._events = self._events, []
        return events

    def ping(self):  # pragma: no cover
        return {"status": "ok"}


def test_enrichment_writes_provider_events_with_workspace(tmp_path):
    cands = [ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)]
    flog = tmp_path / "diag" / "search_failures.jsonl"
    provider = _EventedFake()
    enrich_exhibitors(
        candidates=cands, workspace_id="r1ws", lang="en", config=_config(),
        search_provider=provider,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r1.jsonl",
        failure_log_path=flog,
    )
    rows = [json.loads(ln) for ln in flog.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == provider.calls == 2  # web + news, both live
    assert all(r["workspace"] == "r1ws" and r["outcome"] == "ok" for r in rows)

    # Second run, same cache, fresh resume: all cache hits → no new events.
    p2 = _EventedFake()
    enrich_exhibitors(
        candidates=cands, workspace_id="r1ws", lang="en", config=_config(),
        search_provider=p2,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r2.jsonl",
        failure_log_path=flog,
    )
    assert p2.calls == 0
    rows_after = flog.read_text(encoding="utf-8").splitlines()
    assert len(rows_after) == 2  # unchanged — cache hits log nothing


def test_enrichment_logs_error_event_when_provider_raises(tmp_path):
    class _Boom(_EventedFake):
        def search(self, query, *, kind, count, days=None, lang="en"):
            raise RuntimeError("socket reset")

    cands = [ExhibitorCandidate(name="Mobius Labs", source_snippet="x" * 30)]
    flog = tmp_path / "search_failures.jsonl"
    enrich_exhibitors(
        candidates=cands, workspace_id="r1err", lang="en", config=_config(),
        search_provider=_Boom(),
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
        failure_log_path=flog,
    )
    rows = [json.loads(ln) for ln in flog.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2  # web + news both errored
    assert all(
        r["outcome"] == "error" and r["exc_classes"] == ["RuntimeError"]
        and r["workspace"] == "r1err"
        for r in rows
    )


# ---------- CLI ----------


def test_cli_retry_stats_smoke(tmp_path):
    diag = tmp_path / "diagnostics" / "w1"
    diag.mkdir(parents=True)
    (diag / "search_failures.jsonl").write_text(
        json.dumps(_ev("recovered", attempts=2, excs=["DDGSException"])) + "\n"
        + json.dumps(_ev("degraded", attempts=6, excs=["DDGSException"] * 6)) + "\n",
        encoding="utf-8",
    )
    res = runner.invoke(
        app,
        ["benchmark", "retry-stats", "--diagnostics-dir", str(tmp_path / "diagnostics"),
         "--out", str(tmp_path / "stats.json")],
    )
    assert res.exit_code == 0, res.output
    written = json.loads((tmp_path / "stats.json").read_text(encoding="utf-8"))
    assert written["total_events"] == 2
    assert written["retry_recovery_rate"] == 0.5
    assert written["files_scanned"] == 1
