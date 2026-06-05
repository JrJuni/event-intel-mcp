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
    capability_fit: float                                  # avg(top_k cosine), 0..1
    top_hits: list[dict]                                   # raw vectorstore hits
    capability_fit_breakdown: dict[str, int] = field(default_factory=dict)
    competitor_hits: int = 0
    bad_fit_hits: int = 0


def _exhibitor_query_text(row: "EnrichedExhibitor") -> str:
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
    to [0, 1]."""
    if distance is None:
        return 0.0
    try:
        sim = 1.0 - float(distance) / 2.0
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, sim))


def retrieve_fit_event_to_product(
    *,
    exhibitors: list["EnrichedExhibitor"],
    workspace_id: str,
    embedding_provider: "EmbeddingProvider",
    vectorstore_provider: "VectorStoreProvider",
    top_k: int = 5,
) -> list[FitResult]:
    """For each exhibitor, embed its evidence and query the product collection.

    Returns a `FitResult` per input exhibitor, in the same order. Empty input
    returns an empty list.
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

    hits_batch = vectorstore_provider.query(
        collection=collection,
        query_embeddings=embeddings,
        top_k=top_k,
    )

    results: list[FitResult] = []
    for exh, hits in zip(exhibitors, hits_batch, strict=True):
        breakdown: dict[str, int] = {}
        competitor_hits = 0
        bad_fit_hits = 0
        cap_sims: list[float] = []
        for h in hits:
            md = h.get("metadata") or {}
            kind = md.get("kind", "")
            if kind == "capability":
                cap_name = md.get("capability_name", "?")
                breakdown[cap_name] = breakdown.get(cap_name, 0) + 1
                cap_sims.append(_similarity_from_distance(h.get("distance")))
            elif kind == "competitor":
                competitor_hits += 1
            elif kind == "bad_fit":
                bad_fit_hits += 1
        # capability_fit averages ONLY capability-kind hits. Averaging all kinds
        # let a company sitting next to its own `competitor:<name>` chunk inflate
        # its fit (e.g. Snowflake 0.62 > LlamaIndex 0.56) — exactly backwards for
        # BD. A row whose top-k is crowded by competitor/bad_fit chunks now gets
        # a LOW capability_fit, and the hit counts drive the penalties.
        # NOTE: a 1-capability-hit average and a 3-hit average are treated with
        # equal confidence here — count-weighting is deferred (see backlog).
        avg = sum(cap_sims) / len(cap_sims) if cap_sims else 0.0
        results.append(
            FitResult(
                name=exh.name,
                capability_fit=avg,
                top_hits=hits,
                capability_fit_breakdown=breakdown,
                competitor_hits=competitor_hits,
                bad_fit_hits=bad_fit_hits,
            )
        )
    return results
