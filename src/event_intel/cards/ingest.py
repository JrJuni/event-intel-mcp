"""Ingest validated CapabilityCards into the Product Context mini-RAG.

Strategy: flatten cards into stable, addressable chunks (one per capability /
ideal_customer dimension / trigger / bad_fit / competitor), embed via bge-m3,
upsert into the per-workspace product collection. Same input → same chunk ids,
so re-ingest is an in-place update (no duplicates).

The collection name is `product_{workspace_id}` — the same convention used by
the runtime preflight `product_context` check (`runtime/preflight._product_collection_name`).
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from event_intel.cards.schema import CapabilityCards

if TYPE_CHECKING:
    from event_intel.providers.embedding import EmbeddingProvider
    from event_intel.providers.vectorstore import VectorStoreProvider

_DEFAULT_EMBEDDING_MODEL_ID = "bge-m3"
RECEIPT_FILENAME = "ingest_receipt.json"


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


# ============================================================================
# Ingest receipt + content fingerprint — Y1 CS7 (review R3-6).
#
# Two distinct artifacts, mirroring run_id vs run_fingerprint (CS1):
#   content_fingerprint — DETERMINISTIC sha256 of (sorted chunk_id+doc_hash) +
#                         embedding model + collection. NO timestamp, so an
#                         identical re-ingest yields the identical fingerprint
#                         (that determinism is what folds into run_fingerprint).
#   ingest_receipt      — an INSTANCE record: the fingerprint + cards SHA + ts.
#                         ts is for audit only and is NOT part of the fingerprint.
# The fingerprint is also written to the Chroma collection's metadata so measure
# can detect a live collection that drifted from the receipt (a file receipt
# alone can't prove the live store is unchanged).
# ============================================================================


def _doc_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def compute_content_fingerprint(
    chunks: list[_Chunk], *, embedding_model_id: str, collection: str
) -> str:
    """Deterministic, timestamp-free fingerprint of an ingest's content."""
    payload = {
        "chunks": sorted((c.id, _doc_hash(c.text)) for c in chunks),
        "embedding_model_id": embedding_model_id,
        "collection": collection,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_ingest_receipt(
    *,
    content_fingerprint: str,
    cards_sha256: str | None,
    collection: str,
    chunk_count: int,
    embedding_model_id: str,
    now_iso: str,
) -> dict[str, Any]:
    """An ingest INSTANCE record. `ingested_at` is audit-only — it is deliberately
    NOT part of `content_fingerprint`, so re-ingesting identical cards produces the
    same fingerprint with a different timestamp.
    """
    return {
        "content_fingerprint": content_fingerprint,
        "cards_sha256": cards_sha256,
        "collection": collection,
        "chunk_count": chunk_count,
        "embedding_model_id": embedding_model_id,
        "ingested_at": now_iso,
    }


def write_ingest_receipt(receipt: dict[str, Any], path: str | Path) -> Path:
    """Atomically write the receipt JSON (overwrites — the latest ingest wins)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".ingest_receipt.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(receipt, ensure_ascii=False, indent=2))
        Path(tmp).replace(p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return p


def read_ingest_receipt(path: str | Path) -> dict[str, Any] | None:
    """Load a receipt, or None if it is missing/unreadable."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_collection_fingerprint(
    vectorstore_provider: VectorStoreProvider, collection: str, expected_fp: str
) -> dict[str, Any]:
    """Compare the live collection's stored fingerprint with `expected_fp`.

    Returns status `match` / `mismatch` / `absent` — measure uses this to detect a
    Chroma collection that was mutated since the receipt was written.
    """
    getter = getattr(vectorstore_provider, "get_collection_metadata", None)
    meta = getter(collection) if callable(getter) else {}
    live = (meta or {}).get("content_fingerprint")
    if live is None:
        status = "absent"
    elif live == expected_fp:
        status = "match"
    else:
        status = "mismatch"
    return {"status": status, "live": live, "expected": expected_fp}


def ingest_cards(
    *,
    cards: CapabilityCards,
    workspace_id: str,
    embedding_provider: EmbeddingProvider,
    vectorstore_provider: VectorStoreProvider,
    embedding_model_id: str | None = None,
) -> dict:
    """Embed + REPLACE cards in product_{workspace_id}. Returns a summary dict.

    The collection is reset before upsert so a re-ingest fully replaces the prior
    card set — renamed/removed capabilities don't linger as orphan chunks (review
    round-2 #5). Re-ingesting identical cards is still idempotent.
    """
    model_id = embedding_model_id or getattr(
        embedding_provider, "model_id", _DEFAULT_EMBEDDING_MODEL_ID
    )
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
    new_ids = [c.id for c in chunks]
    content_fingerprint = compute_content_fingerprint(
        chunks, embedding_model_id=model_id, collection=collection
    )

    # ATOMIC replace (review round-3 #1): write the new set FIRST, then delete only
    # the orphans (existing − new). Never delete-then-upsert — an upsert failure
    # there would wipe a healthy product context. Chunk ids are name-derived, so a
    # renamed/removed capability becomes an orphan that we prune here.
    existing_fn = getattr(vectorstore_provider, "existing_ids", None)
    existing = set(existing_fn(collection)) if callable(existing_fn) else set()

    vectorstore_provider.upsert(
        collection=collection,
        ids=new_ids,
        embeddings=embeddings,
        metadatas=[c.metadata for c in chunks],
        documents=texts,
    )

    orphans = existing - set(new_ids)
    orphans_removed = 0
    orphan_cleanup_ok = True
    if orphans:
        deleter = getattr(vectorstore_provider, "delete_ids", None)
        if callable(deleter):
            try:
                deleter(collection, sorted(orphans))
                orphans_removed = len(orphans)
            except Exception:
                # New data is already written and correct; a failed prune leaves
                # stale chunks but is NOT data loss — surface it, don't fail.
                orphan_cleanup_ok = False

    # Record the content fingerprint on the collection itself (CS7) so a later
    # measure can detect drift between the live store and the receipt. Guarded:
    # providers without metadata support (the ABC default) just skip this.
    setter = getattr(vectorstore_provider, "set_collection_metadata", None)
    fingerprint_persisted = False
    if callable(setter):
        try:
            setter(collection, {"content_fingerprint": content_fingerprint})
            fingerprint_persisted = True
        except Exception:
            fingerprint_persisted = False

    return {
        "ok": True,
        "collection": collection,
        "chunks": len(chunks),
        "ids": new_ids,
        "orphans_removed": orphans_removed,
        "orphan_cleanup_ok": orphan_cleanup_ok,
        "product_name": cards.product_name,
        "schema_version": cards.schema_version,
        "content_fingerprint": content_fingerprint,
        "embedding_model_id": model_id,
        "fingerprint_persisted": fingerprint_persisted,
    }
