"""Unidirectional fit retrieval — event evidence → product capability chunks.

Per plan v0.5 §Mini-RAG (단방향 정정):
    For each exhibitor, embed its evidence text (source_snippet + description
    + news titles) and query the per-workspace product collection. Average
    top-k cosine similarity → `capability_fit` raw score (0..1). Per-capability
    hit count → `capability_fit_breakdown`.

    The legacy v0.3 "bidirectional" path (product → event query too) is
    explicitly NOT exposed here. Adding it is a v0.4+ decision.

Heavy deps stay lazy — embedding + vectorstore both arrive as injected
providers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from event_intel.events.enrichment import EnrichedExhibitor
    from event_intel.providers.embedding import EmbeddingProvider
    from event_intel.providers.vectorstore import VectorStoreProvider


@dataclass
class FitResult:
    name: str
    capability_fit: float                                  # avg capability-pool cosine, 0..1
    top_hits: list[dict]                                   # capability-pool hits (explainability)
    capability_fit_breakdown: dict[str, int] = field(default_factory=dict)
    competitor_hits: int = 0                               # explanatory only (NOT penalty driver)
    bad_fit_hits: int = 0                                  # explanatory only (NOT penalty driver)
    competitor_similarity: float = 0.0                    # max sim over competitor chunks → penalty
    bad_fit_similarity: float = 0.0                       # max sim over bad_fit chunks → penalty


def _exhibitor_query_text(row: EnrichedExhibitor) -> str:
    parts: list[str] = [row.name]
    if row.source_snippet:
        parts.append(row.source_snippet)
    if row.description:
        parts.append(row.description)
    for n in row.news_signals[:3]:
        if n.title:
            parts.append(n.title)
    return "\n".join(parts)


def _product_collection_name(workspace_id: str) -> str:
    return f"product_{workspace_id}"


def _similarity_from_distance(distance: float | None) -> float:
    """Chroma returns squared L2 by default with normalized embeddings. For
    bge-m3 we normalize at ingest, so cosine similarity ≈ 1 - dist/2. Clamp
    to [0, 1].
    """
    if distance is None:
        return 0.0
    try:
        sim = 1.0 - float(distance) / 2.0
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, sim))


def retrieve_fit_event_to_product(
    *,
    exhibitors: list[EnrichedExhibitor],
    workspace_id: str,
    embedding_provider: EmbeddingProvider,
    vectorstore_provider: VectorStoreProvider,
    top_k: int = 5,
    capability_top_k: int | None = None,
    capability_aggregate_top_n: int = 3,
) -> list[FitResult]:
    """For each exhibitor, embed its evidence and query the product collection.

    **Two pools** (Phase 18V 4b):
      - capability pool — `where kind=capability`, larger `capability_top_k`, so
        capability_fit averages over a fuller view of positive matches.
      - negative pool — `where kind in {competitor, bad_fit}`, `top_k`, used for
        the penalty SIMILARITY (max cosine per kind), NOT a raw count. A
        negative-only query would saturate any count to ~top_k for everyone;
        the max-similarity is what tells a true competitor from a coincidental
        neighbor (review round-2 #1).

    `where` is honored by Chroma; fakes that ignore it still produce correct
    results because we re-filter by `metadata.kind` here. Returns one FitResult
    per exhibitor, same order. Empty input → empty list.
    """
    if not exhibitors:
        return []

    collection = _product_collection_name(workspace_id)
    queries = [_exhibitor_query_text(e) for e in exhibitors]
    embeddings = embedding_provider.embed(queries)
    if len(embeddings) != len(exhibitors):
        raise RuntimeError(
            f"embedding count mismatch: {len(embeddings)} for {len(exhibitors)} exhibitors"
        )

    cap_k = capability_top_k if capability_top_k and capability_top_k > 0 else top_k
    cap_batch = vectorstore_provider.query(
        collection=collection,
        query_embeddings=embeddings,
        top_k=cap_k,
        where={"kind": "capability"},
    )
    neg_batch = vectorstore_provider.query(
        collection=collection,
        query_embeddings=embeddings,
        top_k=top_k,
        where={"kind": {"$in": ["competitor", "bad_fit"]}},
    )

    results: list[FitResult] = []
    for exh, cap_hits, neg_hits in zip(exhibitors, cap_batch, neg_batch, strict=True):
        breakdown: dict[str, int] = {}
        cap_sims: list[float] = []
        for h in cap_hits:
            md = h.get("metadata") or {}
            if md.get("kind") != "capability":
                continue
            cap_name = md.get("capability_name", "?")
            breakdown[cap_name] = breakdown.get(cap_name, 0) + 1
            cap_sims.append(_similarity_from_distance(h.get("distance")))

        competitor_hits = 0
        bad_fit_hits = 0
        comp_sims: list[float] = []
        bad_sims: list[float] = []
        for h in neg_hits:
            md = h.get("metadata") or {}
            kind = md.get("kind", "")
            sim = _similarity_from_distance(h.get("distance"))
            if kind == "competitor":
                competitor_hits += 1
                comp_sims.append(sim)
            elif kind == "bad_fit":
                bad_fit_hits += 1
                bad_sims.append(sim)

        # capability_fit = mean of the TOP-N strongest capability-kind sims, not
        # the mean of ALL of them (review #5). With <=10 capability chunks and a
        # capability pool of 20, averaging everything let weak capabilities dilute
        # a strong specific match and flatten scores. Top-N keeps a sharp,
        # genuine fit sharp. Still capability-only (Phase-18U contamination fix).
        top_n = capability_aggregate_top_n if capability_aggregate_top_n > 0 else len(cap_sims)
        best = sorted(cap_sims, reverse=True)[:top_n]
        avg = sum(best) / len(best) if best else 0.0
        results.append(
            FitResult(
                name=exh.name,
                capability_fit=avg,
                top_hits=cap_hits,
                capability_fit_breakdown=breakdown,
                competitor_hits=competitor_hits,
                bad_fit_hits=bad_fit_hits,
                competitor_similarity=max(comp_sims, default=0.0),
                bad_fit_similarity=max(bad_sims, default=0.0),
            )
        )
    return results
