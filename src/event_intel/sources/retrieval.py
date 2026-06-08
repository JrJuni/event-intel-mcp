"""Workspace-source retrieval for capability-card drafting (WSL W3).

Given a populated ``product_sources_{ws}`` collection (W1/W2), run a fixed set of
card-shaped queries, gather diverse + deduped chunks ACROSS documents, and
assemble a single capped text blob that feeds the EXISTING card drafter. The raw
source only GROUNDS the draft (the human still edits it); it never feeds a score.

Cold-import safe: stdlib + the (cold) indexer module; the embedding + vectorstore
providers are injected.
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from event_intel.errors import ErrorCode, MCPError, Stage
from event_intel.sources.indexer import source_collection_name

if TYPE_CHECKING:
    from event_intel.providers.embedding import EmbeddingProvider
    from event_intel.providers.vectorstore import VectorStoreProvider

DEFAULT_MAX_CHARS = 60_000
DEFAULT_PER_QUERY_TOP_K = 6

# Fixed card-shaped queries — one per capability_cards facet. The point is broad
# coverage of the draft's sections, not precision; dedup + per-doc spread below
# turn the union of hits into a balanced context blob.
_QUERIES: dict[str, list[str]] = {
    "en": [
        "what the product is and its one-line summary",
        "key capabilities and features and what it does",
        "ideal customer profile: target industries and company signals",
        "buyer pain points and use cases the product solves",
        "buying triggers and signals that a company is ready to buy",
        "competitors, alternatives, and bad-fit / out-of-scope cases",
    ],
    "ko": [
        "제품이 무엇인지와 한 줄 요약",
        "핵심 기능과 역량, 무엇을 하는 제품인지",
        "이상적 고객 프로필: 타깃 산업과 기업 시그널",
        "제품이 해결하는 고객의 페인 포인트와 활용 사례",
        "구매 트리거와 도입 준비가 된 기업의 신호",
        "경쟁사·대체재, 그리고 부적합/범위 외 케이스",
    ],
}


def _provenance_label(metadata: dict) -> str:
    md = metadata or {}
    sp = md.get("source_path", "?")
    if "page" in md:
        return f"{sp} p{md['page']}"
    if "row_start" in md:
        return f"{sp} rows {md['row_start']}-{md.get('row_end', md['row_start'])}"
    return sp


def gather_workspace_source_text(
    *,
    workspace_id: str,
    embedding_provider: EmbeddingProvider,
    vectorstore_provider: VectorStoreProvider,
    lang: str = "en",
    queries: list[str] | None = None,
    per_query_top_k: int = DEFAULT_PER_QUERY_TOP_K,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> tuple[str, dict]:
    """Assemble a capped, document-balanced text blob from product_sources_{ws}.

    Returns ``(blob, meta)``. Raises ``INVALID_INPUT`` if the collection has no
    chunks (the user must run ``sources sync`` first).
    """
    collection = source_collection_name(workspace_id)
    qtexts = queries or _QUERIES.get(lang, _QUERIES["en"])
    embeddings = embedding_provider.embed(qtexts)
    if len(embeddings) != len(qtexts):
        raise RuntimeError(
            f"embedding count mismatch: {len(embeddings)} for {len(qtexts)} queries"
        )

    batch = vectorstore_provider.query(
        collection=collection,
        query_embeddings=embeddings,
        top_k=per_query_top_k,
    )

    # Dedup hits by chunk id, keeping the best (lowest distance) across queries.
    best: dict[str, dict] = {}
    for hits in batch:
        for h in hits:
            cid = h.get("id")
            if cid is None:
                continue
            dist = h.get("distance")
            dist = 1.0 if dist is None else float(dist)
            prev = best.get(cid)
            if prev is None or dist < prev["distance"]:
                best[cid] = {
                    "document": h.get("document") or "",
                    "metadata": h.get("metadata") or {},
                    "distance": dist,
                    "source_path": (h.get("metadata") or {}).get("source_path", "?"),
                }

    if not best:
        raise MCPError(
            error_code=ErrorCode.INVALID_INPUT,
            stage=Stage.INGEST,
            message=(
                f"no source chunks in {collection} — nothing to draft from. "
                "Place product docs under the workspace sources dir and run "
                "`event-intel sources sync` first."
            ),
            hint={
                "collection": collection,
                "fix": "sync the source library before drafting from workspace",
            },
        )

    # Per-document spread: bucket by source file, round-robin by within-doc rank
    # so a single large document can't crowd out the others.
    groups: dict[str, list[dict]] = defaultdict(list)
    for h in sorted(best.values(), key=lambda x: x["distance"]):
        groups[h["source_path"]].append(h)
    doc_order = sorted(groups, key=lambda p: groups[p][0]["distance"])

    ordered: list[dict] = []
    rank = 0
    while True:
        added = False
        for p in doc_order:
            if rank < len(groups[p]):
                ordered.append(groups[p][rank])
                added = True
        if not added:
            break
        rank += 1

    # Assemble up to max_chars, deduping identical chunk text.
    parts: list[str] = []
    used = 0
    seen_text: set[str] = set()
    files: set[str] = set()
    truncated = False
    sep_len = len("\n\n---\n\n")
    for h in ordered:
        text = (h["document"] or "").strip()
        if not text or text in seen_text:
            continue
        block = f"[source: {_provenance_label(h['metadata'])}]\n{text}"
        if parts and used + len(block) + sep_len > max_chars:
            truncated = True
            break
        parts.append(block)
        used += len(block) + sep_len
        seen_text.add(text)
        files.add(h["source_path"])

    blob = "\n\n---\n\n".join(parts)
    meta = {
        "collection": collection,
        "queries": len(qtexts),
        "chunks_used": len(parts),
        "files": len(files),
        "chars": len(blob),
        "truncated": truncated,
    }
    return blob, meta


def gather_exhibitor_provenance(
    *,
    items: list[tuple[str, str]],
    workspace_id: str,
    embedding_provider: EmbeddingProvider,
    vectorstore_provider: VectorStoreProvider,
    top_k: int = 3,
    snippet_chars: int = 240,
) -> dict[str, list[dict]]:
    """Per-exhibitor raw-source grounding for the tier-list report (WSL W4).

    ``items`` is a list of ``(exhibitor_name, query_text)``. Returns
    ``{name: [{source_path, locator, snippet}]}`` (top ``top_k`` chunks each).

    Graceful by design — this is RATIONALE-ONLY and must never fail a build: an
    empty/missing collection or any error yields ``{}`` (the report then simply
    carries no provenance, falling back to the card-based rationale). It reads
    ``product_sources_{ws}`` and never touches any scoring input.
    """
    if not items:
        return {}
    collection = source_collection_name(workspace_id)
    names = [n for n, _ in items]
    qtexts = [q for _, q in items]
    embeddings = embedding_provider.embed(qtexts)
    if len(embeddings) != len(qtexts):
        return {}
    batch = vectorstore_provider.query(
        collection=collection, query_embeddings=embeddings, top_k=top_k
    )
    out: dict[str, list[dict]] = {}
    for name, hits in zip(names, batch, strict=False):
        prov: list[dict] = []
        for h in hits:
            md = h.get("metadata") or {}
            text = (h.get("document") or "").strip()
            snippet = " ".join(text.split())[:snippet_chars]
            prov.append(
                {
                    "source_path": md.get("source_path", "?"),
                    "locator": _provenance_label(md),
                    "snippet": snippet,
                }
            )
        if prov:
            out[name] = prov
    return out
