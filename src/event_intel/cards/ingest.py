"""Ingest validated CapabilityCards into the Product Context mini-RAG.

Strategy: flatten cards into stable, addressable chunks (one per capability /
ideal_customer dimension / trigger / bad_fit / competitor), embed via bge-m3,
upsert into the per-workspace product collection. Same input → same chunk ids,
so re-ingest is an in-place update (no duplicates).

The collection name is `product_{workspace_id}` — the same convention used by
the runtime preflight `product_context` check (`runtime/preflight._product_collection_name`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from event_intel.cards.schema import CapabilityCards

if TYPE_CHECKING:
    from event_intel.providers.embedding import EmbeddingProvider
    from event_intel.providers.vectorstore import VectorStoreProvider


@dataclass
class _Chunk:
    id: str
    text: str
    metadata: dict


def product_collection_name(workspace_id: str) -> str:
    return f"product_{workspace_id}"


def flatten_cards_to_chunks(cards: CapabilityCards) -> list[_Chunk]:
    """Stable id scheme — re-flattening identical cards yields identical chunks."""
    chunks: list[_Chunk] = []
    product = cards.product_name

    # Product summary (one entry, gives any cross-cutting query something to land on)
    chunks.append(
        _Chunk(
            id="product:summary",
            text=f"{product}: {cards.one_liner}",
            metadata={
                "kind": "product_summary",
                "product_name": product,
                "schema_version": cards.schema_version,
            },
        )
    )

    # Capabilities — one chunk each. Concatenate name / pains / queries / keywords
    # so a single retrieval surface covers both buyer-intent and seller-language.
    for i, cap in enumerate(cards.capabilities):
        text = (
            f"Capability: {cap.name}\n"
            f"Keywords: {', '.join(cap.keywords)}\n"
            f"Buyer pains: {'; '.join(cap.buyer_pains)}\n"
            f"Evidence queries: {'; '.join(cap.evidence_queries)}"
        )
        chunks.append(
            _Chunk(
                id=f"cap:{i}:{cap.name}",
                text=text,
                metadata={
                    "kind": "capability",
                    "capability_name": cap.name,
                    "capability_index": i,
                },
            )
        )

    # Ideal customer — split into industries / signals / geo so different queries
    # can find them independently.
    ic = cards.ideal_customer
    chunks.append(
        _Chunk(
            id="ideal_customer:industries",
            text=f"Ideal customer industries: {', '.join(ic.industries)}",
            metadata={"kind": "ideal_customer", "facet": "industries"},
        )
    )
    chunks.append(
        _Chunk(
            id="ideal_customer:signals",
            text=f"Ideal customer signals: {', '.join(ic.company_signals)}",
            metadata={"kind": "ideal_customer", "facet": "signals"},
        )
    )
    if ic.geo:
        chunks.append(
            _Chunk(
                id="ideal_customer:geo",
                text=f"Ideal customer geo: {', '.join(ic.geo)}",
                metadata={"kind": "ideal_customer", "facet": "geo"},
            )
        )

    # Buying triggers
    for i, trig in enumerate(cards.buying_triggers):
        chunks.append(
            _Chunk(
                id=f"trigger:{i}",
                text=f"Buying trigger: {trig.signal} (weight={trig.weight:.2f})",
                metadata={
                    "kind": "buying_trigger",
                    "weight": trig.weight,
                    "trigger_index": i,
                },
            )
        )

    # Bad fits — used by scoring penalty + retrieval-time filtering
    for i, bf in enumerate(cards.bad_fit):
        kw = f" Keywords: {', '.join(bf.keywords)}." if bf.keywords else ""
        chunks.append(
            _Chunk(
                id=f"bad_fit:{i}",
                text=f"Bad fit: {bf.reason}.{kw}",
                metadata={"kind": "bad_fit", "bad_fit_index": i},
            )
        )

    # Competitors
    for i, comp in enumerate(cards.competitors):
        kw = f" Keywords: {', '.join(comp.keywords)}." if comp.keywords else ""
        chunks.append(
            _Chunk(
                id=f"competitor:{i}:{comp.name}",
                text=f"Competitor: {comp.name}.{kw}",
                metadata={
                    "kind": "competitor",
                    "competitor_name": comp.name,
                    "competitor_index": i,
                },
            )
        )

    return chunks


def ingest_cards(
    *,
    cards: CapabilityCards,
    workspace_id: str,
    embedding_provider: "EmbeddingProvider",
    vectorstore_provider: "VectorStoreProvider",
) -> dict:
    """Embed + upsert cards into product_{workspace_id}. Returns a summary dict.

    Idempotent: re-ingesting the same cards updates rows in place because every
    chunk id is content-derived and stable across runs.
    """
    chunks = flatten_cards_to_chunks(cards)
    if not chunks:  # pragma: no cover — flatten always emits product:summary
        return {
            "ok": True,
            "collection": product_collection_name(workspace_id),
            "chunks": 0,
            "ids": [],
        }

    texts = [c.text for c in chunks]
    embeddings = embedding_provider.embed(texts)
    if len(embeddings) != len(texts):
        raise RuntimeError(
            f"embedding count mismatch: got {len(embeddings)} for {len(texts)} inputs"
        )

    collection = product_collection_name(workspace_id)
    vectorstore_provider.upsert(
        collection=collection,
        ids=[c.id for c in chunks],
        embeddings=embeddings,
        metadatas=[c.metadata for c in chunks],
        documents=texts,
    )

    return {
        "ok": True,
        "collection": collection,
        "chunks": len(chunks),
        "ids": [c.id for c in chunks],
        "product_name": cards.product_name,
        "schema_version": cards.schema_version,
    }
