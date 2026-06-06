"""Scoring-matrix runner (eval layer 1A).

Loads a labeled cell (one product×event YAML), builds `EnrichedExhibitor` +
fake `FitResult` from it, runs the real `score_exhibitors`, and returns the
metrics. No network / no embeddings — `capability_fit`, `competitor_hits`, etc.
come straight from the fixture so this stays a fast regression gate.

Cell YAML shape:

    cell: db_x_ai_gtc
    product: db
    event: ai
    target_mode: customer            # mode-aware positive labels
    reference_date: "2026-06-01T00:00:00+00:00"   # forward-compat (recency, 4a)
    rows:
      - name: LlamaIndex
        label: target                # target | competitor | bad_fit | neutral
        expected_evidence_types: [official_url, news]
        official_url: https://www.llamaindex.ai
        news: 3                       # number of news signals (fake)
        capability_fit: 0.78
        competitor_hits: 0
        bad_fit_hits: 0
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from event_intel.eval import metrics as _metrics

# Mode-aware positive label sets (review round-2 #6). customer hunts customers;
# partner also welcomes partners; ecosystem treats the whole field as fair game.
_POSITIVE_BY_MODE: dict[str, set[str]] = {
    "customer": {"target"},
    "partner": {"target", "partner"},
    "ecosystem": {"target", "partner", "competitor"},
}


def positive_labels_for_mode(target_mode: str) -> set[str]:
    return _POSITIVE_BY_MODE.get(target_mode, {"target"})


@dataclass
class CellMetrics:
    cell: str
    target_mode: str
    n_rows: int
    auc: float | None
    precision_at_10: float | None
    competitor_leakage_rate: float | None
    evidence_false_positive_rate: float
    tier_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "cell": self.cell,
            "target_mode": self.target_mode,
            "n_rows": self.n_rows,
            "auc": self.auc,
            "precision_at_10": self.precision_at_10,
            "competitor_leakage_rate": self.competitor_leakage_rate,
            "evidence_false_positive_rate": self.evidence_false_positive_rate,
            "tier_counts": self.tier_counts,
        }


def load_cell(path: str | Path) -> dict:
    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "rows" not in data:
        raise ValueError(f"eval cell {path} missing top-level 'rows'")
    return data


def present_evidence_types(row) -> set[str]:
    """Evidence types the engine credits for a row. Forward-compatible: reads the
    typed `evidence` container (item 1, 18V-2) when present, else falls back to
    the official_url + news_signals representation that ships today."""
    typed = getattr(row, "evidence", None)
    if typed:
        return {getattr(e, "type", None) for e in typed if getattr(e, "type", None)}
    present: set[str] = set()
    if getattr(row, "official_url", None):
        present.add("official_url")
    if getattr(row, "news_signals", None):
        present.add("news")
    return present


def _build_scoring_inputs(cell: dict):
    from event_intel.events.enrichment import EnrichedExhibitor, NewsSignal
    from event_intel.rag.retriever import FitResult

    enriched: list = []
    fits: list = []
    for r in cell["rows"]:
        name = r["name"]
        n_news = int(r.get("news", 0))
        news = [
            NewsSignal(title=f"{name} news {i}", url=f"https://news.example/{name}/{i}", snippet="")
            for i in range(n_news)
        ]
        enriched.append(
            EnrichedExhibitor(
                name=name,
                source_snippet=r.get("snippet", f"evidence snippet for {name}"),
                url=r.get("url"),
                official_url=r.get("official_url"),
                description=r.get("description"),
                news_signals=news,
                extraction_confidence=float(r.get("extraction_confidence", 1.0)),
            )
        )
        fits.append(
            FitResult(
                name=name,
                capability_fit=float(r.get("capability_fit", 0.0)),
                top_hits=[],
                capability_fit_breakdown=dict(r.get("breakdown", {})),
                competitor_hits=int(r.get("competitor_hits", 0)),
                bad_fit_hits=int(r.get("bad_fit_hits", 0)),
                # 4b penalty is similarity-gated — the fixture MUST supply the
                # similarities (not just hit counts) or the competitor/bad_fit
                # penalty never fires and the matrix wouldn't test it (review #2).
                competitor_similarity=float(r.get("competitor_similarity", 0.0)),
                bad_fit_similarity=float(r.get("bad_fit_similarity", 0.0)),
            )
        )
    return enriched, fits


def _parse_reference_date(cell: dict) -> datetime:
    raw = cell.get("reference_date")
    if isinstance(raw, str) and raw:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def run_scoring_cell(cell: dict, *, config: dict) -> CellMetrics:
    """Run the scoring matrix (1A) over one labeled cell and return its metrics."""
    from event_intel.scoring.compute import score_exhibitors

    enriched, fits = _build_scoring_inputs(cell)
    labels = {r["name"]: r.get("label", "neutral") for r in cell["rows"]}
    target_mode = cell.get("target_mode", "customer")

    # The whole point of the matrix is to exercise the REAL scorer paths: the
    # resolved target_mode (penalty factors) and a FIXED reference_date (recency)
    # must reach score_exhibitors, else the metrics don't test 18V (review #2).
    summary = score_exhibitors(
        enriched=enriched,
        fit_results=fits,
        cards=None,
        config=config,
        top_k=int(config.get("scoring", {}).get("retrieval", {}).get("top_k", 5)),
        target_mode=target_mode,
        reference_date=_parse_reference_date(cell),
    )

    scored = [(s.name, s.final_score) for s in summary.rows]
    tiers = {s.name: s.tier for s in summary.rows}
    present_ev = {row.name: present_evidence_types(row) for row in enriched}
    expected_ev = {
        r["name"]: set(r.get("expected_evidence_types", [])) for r in cell["rows"]
    }
    positive = positive_labels_for_mode(target_mode)

    return CellMetrics(
        cell=cell.get("cell", "?"),
        target_mode=target_mode,
        n_rows=len(enriched),
        auc=_metrics.ranking_accuracy_auc(scored, labels),
        precision_at_10=_metrics.precision_at_10(scored, labels, positive),
        competitor_leakage_rate=_metrics.competitor_leakage_rate(tiers, labels),
        evidence_false_positive_rate=_metrics.evidence_false_positive_rate(
            present_ev, expected_ev
        ),
        tier_counts=dict(summary.tier_counts),
    )


def run_matrix(cell_dir: str | Path, *, config: dict) -> list[CellMetrics]:
    """Run every `*.yaml` cell under a directory (scoring matrix)."""
    cell_dir = Path(cell_dir)
    out: list[CellMetrics] = []
    for path in sorted(cell_dir.glob("*.yaml")):
        out.append(run_scoring_cell(load_cell(path), config=config))
    return out
