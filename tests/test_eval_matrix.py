"""Phase 18V eval matrix — two layers.

1A scoring matrix: fast regression over labeled cells (fake FitResult), asserting
   mode-aware metric thresholds + a committed baseline snapshot for DB×AI.
1B pipeline-contract matrix: fake Search provider injected, REAL enrichment run,
   asserting the pipeline produces the evidence the scoring layer assumes. Grows
   as 18V-2 lands dedupe / pool-split / timestamp behavior.

Baseline numbers live here as committed constants (NOT read from gitignored
outputs/) — they are the floor that 18V-2/18V-3 must not regress.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from event_intel.eval import metrics as M
from event_intel.eval.harness import load_cell, run_scoring_cell
from event_intel.runtime.preflight import load_config

_FIXTURES = Path(__file__).parent / "fixtures" / "eval"

# --- committed baseline snapshot (Phase 18U DB×AI gold set) ---
BASELINE_CELL = "db_x_ai_gtc"
BASELINE_AUC = 1.0
BASELINE_LEAKAGE = 0.0
BASELINE_PRECISION_AT_10 = 0.8
BASELINE_TIER_COUNTS = {"S": 0, "A": 8, "B": 5, "C": 3}


def _config():
    return load_config()


def _cells():
    return sorted(_FIXTURES.glob("*.yaml"))


# ---------- 1A scoring matrix ----------


@pytest.mark.parametrize("cell_path", _cells(), ids=lambda p: p.stem)
def test_scoring_cell_metrics_hold(cell_path):
    """Every labeled cell: competitors stay out of S/A (customer mode), targets
    outrank bad_fit, and the engine credits no phantom evidence types."""
    cell = load_cell(cell_path)
    cm = run_scoring_cell(cell, config=_config())

    # AUC is defined only when both target and bad_fit are present.
    if cm.auc is not None:
        assert cm.auc >= 0.9, f"{cm.cell}: AUC {cm.auc} below 0.9"
    if cm.competitor_leakage_rate is not None and cell.get("target_mode", "customer") == "customer":
        assert cm.competitor_leakage_rate == 0.0, f"{cm.cell}: competitor leaked into S/A"
    assert cm.evidence_false_positive_rate <= 0.1, f"{cm.cell}: evidence FP too high"


def test_auc_fixtures_have_both_classes():
    """AUC is meaningless without at least one target AND one bad_fit. Guard the
    fixtures so a cell silently missing a class doesn't pass as 'AUC N/A'."""
    for cell_path in _cells():
        cell = load_cell(cell_path)
        labels = [r.get("label") for r in cell["rows"]]
        assert "target" in labels, f"{cell_path.stem}: no target rows"
        assert "bad_fit" in labels, f"{cell_path.stem}: no bad_fit rows"


def test_baseline_db_x_ai_snapshot():
    """Pin the Phase-18U baseline. 18V-2/18V-3 must reproduce same-or-better."""
    cell = load_cell(_FIXTURES / f"{BASELINE_CELL}.yaml")
    cm = run_scoring_cell(cell, config=_config())
    assert cm.auc == BASELINE_AUC
    assert cm.competitor_leakage_rate == BASELINE_LEAKAGE
    assert cm.precision_at_10 == BASELINE_PRECISION_AT_10
    assert cm.tier_counts == BASELINE_TIER_COUNTS


# ---------- metric unit coverage ----------


def test_ranking_auc_none_when_class_missing():
    assert M.ranking_accuracy_auc([("a", 1.0)], {"a": "target"}) is None
    assert M.ranking_accuracy_auc([("a", 1.0)], {"a": "bad_fit"}) is None


def test_ranking_auc_counts_ties_half():
    scored = [("t", 1.0), ("b", 1.0)]
    labels = {"t": "target", "b": "bad_fit"}
    assert M.ranking_accuracy_auc(scored, labels) == 0.5


def test_evidence_fp_rate_counts_unexpected_types():
    present = {"x": {"official_url", "press_release"}}
    expected = {"x": {"official_url"}}
    # 1 of 2 credited types (press_release) is unexpected.
    assert M.evidence_false_positive_rate(present, expected) == 0.5


def test_precision_at_10_is_mode_aware():
    scored = [(f"t{i}", 10 - i) for i in range(5)]
    labels = {"t0": "target", "t1": "competitor", "t2": "target", "t3": "neutral", "t4": "bad_fit"}
    # customer: only target counts → 2/5
    assert M.precision_at_10(scored, labels, {"target"}) == pytest.approx(2 / 5)
    # partner: target+competitor → 3/5
    assert M.precision_at_10(scored, labels, {"target", "competitor"}) == pytest.approx(3 / 5)


# ---------- 1B pipeline-contract matrix ----------


@dataclass
class _SR:
    title: str
    url: str
    snippet: str
    source: str | None = None
    published_at: object = None
    extra: dict | None = None


class _FakeSearch:
    """Canned web+news results keyed by name fragment — real enrichment runs."""

    def __init__(self):
        self.calls: list[dict] = []
        self.web: dict[str, list[_SR]] = {}
        self.news: dict[str, list[_SR]] = {}

    def search(self, query, *, kind, count, days=None, lang="en"):
        self.calls.append({"query": query, "kind": kind})
        bucket = self.web if kind == "web" else self.news
        for frag, results in bucket.items():
            if frag in query:
                return list(results)
        return []

    def ping(self):  # pragma: no cover
        return {"status": "ok", "remaining_quota": None}


def test_pipeline_contract_enrichment_produces_evidence(tmp_path):
    """1B: inject a fake search provider, run the REAL enrichment pipeline, and
    assert it resolves official_url + news_signals the scoring layer depends on."""
    from event_intel.events.enrichment import enrich_exhibitors
    from event_intel.events.extraction import ExhibitorCandidate

    search = _FakeSearch()
    search.web["Acme Data"] = [
        _SR(title="Acme Data — official", url="https://acmedata.example", snippet=""),
        _SR(title="LinkedIn", url="https://www.linkedin.com/company/acme/", snippet=""),
    ]
    search.news["Acme Data"] = [
        _SR(title="Acme Data raises Series B", url="https://techpress.example/acme-series-b", snippet="funding"),
    ]
    cands = [
        ExhibitorCandidate(
            name="Acme Data",
            source_snippet="Realtime feature store for ML pipelines",
            extraction_confidence=0.9,
        )
    ]
    cfg = {
        "enrichment": {
            "max_companies": 30,
            "brave_count_web": 5,
            "brave_count_news": 5,
            "news_days_back": 180,
            "cache_enabled": True,
            "official_url_levenshtein_threshold": 0.4,
        }
    }
    result = enrich_exhibitors(
        candidates=cands, workspace_id="evalc", lang="en", config=cfg,
        search_provider=search,
        cache_dir=tmp_path / "cache", resume_path=tmp_path / "r.jsonl",
    )
    row = result.rows[0]
    assert row.official_url == "https://acmedata.example"
    assert len(row.news_signals) == 1
    assert row.news_signals[0].url.startswith("https://techpress.example")
