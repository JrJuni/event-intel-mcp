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
# A8/B2/C6: 8 targets (A); 2 competitors below the sim threshold stay B; 3
# competitors above it + 3 bad_fit are penalized to C. This distribution is only
# reproducible because the harness now feeds competitor_similarity + target_mode
# + reference_date into the real scorer (review #2).
BASELINE_TIER_COUNTS = {"S": 0, "A": 8, "B": 2, "C": 6}


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


# ---------- the matrix genuinely exercises 4b penalty + target_mode (review #2) ----------


def _score_cell_rows(cell, *, config, target_mode=None):
    """Score a cell through the real scorer and return {name: ScoredExhibitor}.
    Mirrors harness.run_scoring_cell but exposes per-row results for assertions."""
    from event_intel.eval.harness import _build_scoring_inputs, _parse_reference_date
    from event_intel.scoring.compute import score_exhibitors

    enriched, fits = _build_scoring_inputs(cell)
    summary = score_exhibitors(
        enriched=enriched, fit_results=fits, cards=None, config=config,
        top_k=int(config.get("scoring", {}).get("retrieval", {}).get("top_k", 5)),
        target_mode=target_mode or cell.get("target_mode", "customer"),
        reference_date=_parse_reference_date(cell),
    )
    return {s.name: s for s in summary.rows}


def test_similarity_gated_penalty_is_live_in_matrix():
    """A high-similarity competitor (Snowflake, 0.70) must be penalized BELOW a
    below-threshold one (Vespa, 0.45) — proving the 4b penalty actually flows
    through the harness, not just the unit test."""
    cell = load_cell(_FIXTURES / f"{BASELINE_CELL}.yaml")
    rows = _score_cell_rows(cell, config=_config())
    assert rows["Snowflake"].final_score < rows["Vespa.ai"].final_score
    assert rows["Snowflake"].tier == "C"      # penalty fired
    assert rows["Vespa.ai"].tier == "B"       # gated out (sim < threshold)
    # And no competitor of any similarity leaked into S/A.
    for name in ("Vespa.ai", "Activeloop", "PlanetScale", "Snowflake", "ClickHouse"):
        assert rows[name].tier not in ("S", "A")


def test_partner_mode_neutralizes_penalty_through_harness():
    """Re-scoring the SAME cell under target_mode=partner zeroes the competitor
    penalty — a previously-C competitor recovers. Proves target_mode reaches the
    scorer via the harness (review #2)."""
    cell = load_cell(_FIXTURES / f"{BASELINE_CELL}.yaml")
    customer = _score_cell_rows(cell, config=_config(), target_mode="customer")
    partner = _score_cell_rows(cell, config=_config(), target_mode="partner")
    assert partner["Snowflake"].final_score > customer["Snowflake"].final_score
    order = ["C", "B", "A", "S"]
    assert order.index(partner["Snowflake"].tier) > order.index(customer["Snowflake"].tier)


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


class _FakeEmbed:
    def embed(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class _FakeVS:
    """Honors the `where={"kind": ...}` filter so the two-pool split is actually
    exercised end-to-end (review #2: 1B must run the retriever, not just enrich)."""

    def __init__(self):
        self.calls = []
        self._cap = [{"id": "cap:0", "distance": 0.3,
                      "metadata": {"kind": "capability", "capability_name": "Vector search"}}]
        self._neg = [{"id": "competitor:0", "distance": 0.2,
                      "metadata": {"kind": "competitor", "competitor_name": "RivalDB"}}]

    def query(self, *, collection, query_embeddings, top_k, where=None):
        self.calls.append({"top_k": top_k, "where": where})
        kind = (where or {}).get("kind")
        hits = self._cap if kind == "capability" else self._neg
        return [list(hits) for _ in query_embeddings]


def test_pipeline_contract_retriever_pool_split_and_similarity():
    """1B: run the REAL retriever against a where-aware fake vectorstore — the
    capability pool feeds capability_fit, the negative pool feeds the gated
    competitor_similarity. Catches pool-split regressions the scoring matrix can't."""
    from event_intel.events.enrichment import EnrichedExhibitor
    from event_intel.rag.retriever import retrieve_fit_event_to_product

    rows = [EnrichedExhibitor(name="Acme Data", source_snippet="vector search platform")]
    vs = _FakeVS()
    fits = retrieve_fit_event_to_product(
        exhibitors=rows, workspace_id="evalc",
        embedding_provider=_FakeEmbed(), vectorstore_provider=vs,
        top_k=5, capability_top_k=20,
    )
    fit = fits[0]
    # capability_fit comes from the capability pool (dist 0.3 → sim 0.85).
    assert fit.capability_fit == pytest.approx(0.85, abs=1e-6)
    # competitor_similarity comes from the negative pool (dist 0.2 → sim 0.9).
    assert fit.competitor_similarity == pytest.approx(0.9, abs=1e-6)
    # Two pools queried with distinct kind filters and top_k.
    wheres = [c["where"] for c in vs.calls]
    assert {"kind": "capability"} in wheres
    assert {"kind": {"$in": ["competitor", "bad_fit"]}} in wheres
    assert any(c["top_k"] == 20 for c in vs.calls)  # capability pool larger


# ---------- #4: top-N + recency exercised end-to-end (review round-2 #4) ----------


def test_topn_aggregation_changes_tier_end_to_end():
    """retriever (top-N) → scorer with REAL config: top-3 aggregation must move the
    company across a TIER boundary (A) vs mean-of-all (B) — proving top-N affects
    real tiers, not just a higher number (review round-3 #4)."""
    from event_intel.events.enrichment import EnrichedExhibitor, NewsSignal
    from event_intel.rag.retriever import retrieve_fit_event_to_product
    from event_intel.scoring.compute import score_exhibitors

    sims = [0.95, 0.92, 0.90, 0.15, 0.10]  # 3 strong + 2 weak capabilities

    class _VS:
        def query(self, *, collection, query_embeddings, top_k, where=None):
            if (where or {}).get("kind") == "capability":
                hits = [
                    {"id": f"cap:{i}", "distance": 2 * (1 - s),
                     "metadata": {"kind": "capability", "capability_name": f"C{i}"}}
                    for i, s in enumerate(sims)
                ]
            else:
                hits = []
            return [list(hits) for _ in query_embeddings]

    # official_url + name-matched news → floor 2, buying signal fires (so the
    # capability_fit delta from top-N is what tips A vs B).
    rows = [EnrichedExhibitor(
        name="Acme", source_snippet="cap evidence", official_url="https://acme.example",
        news_signals=[NewsSignal(title=f"Acme news {i}", url=f"u{i}", snippet="") for i in range(3)],
    )]
    cfg = _config()

    def _run(top_n):
        fits = retrieve_fit_event_to_product(
            exhibitors=rows, workspace_id="w", embedding_provider=_FakeEmbed(),
            vectorstore_provider=_VS(), top_k=5, capability_top_k=20,
            capability_aggregate_top_n=top_n,
        )
        return score_exhibitors(
            enriched=rows, fit_results=fits, cards=None, config=cfg, top_k=5,
        ).rows[0]

    top3, all_mean = _run(3), _run(0)
    assert top3.final_score > all_mean.final_score
    assert top3.tier == "A" and all_mean.tier == "B"  # top-N tips the tier


def test_recency_changes_score_through_harness():
    """Same row, recent vs stale news date → recent scores higher when run through
    the harness with a fixed reference_date (recency is live in the matrix, #4)."""
    base = {"target_mode": "customer", "reference_date": "2026-06-01T00:00:00+00:00"}
    row = {"name": "R", "label": "target", "official_url": "https://r.example",
           "news": 1, "capability_fit": 0.7}
    recent = _score_cell_rows({**base, "rows": [{**row, "news_published_at": "2026-05-25"}]},
                              config=_config())["R"]
    stale = _score_cell_rows({**base, "rows": [{**row, "news_published_at": "2023-01-01"}]},
                             config=_config())["R"]
    assert recent.final_score > stale.final_score
