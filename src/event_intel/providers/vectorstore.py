from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from event_intel.runtime import paths as _paths


class VectorStoreProvider(ABC):
    @abstractmethod
    def upsert(
        self,
        *,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        documents: list[str],
    ) -> None: ...

    @abstractmethod
    def query(
        self,
        *,
        collection: str,
        query_embeddings: list[list[float]],
        top_k: int = 5,
        where: dict | None = None,
    ) -> list[list[dict]]: ...

    @abstractmethod
    def collection_info(self, collection: str) -> dict: ...

    @abstractmethod
    def ensure_writable(self) -> dict: ...

    def existing_ids(self, collection: str) -> set[str]:
        """Current chunk ids in a collection. Used for an ATOMIC re-ingest:
        upsert the new set, then delete only the orphans (existing − new) — never
        empty the collection first (review round-3 #1). Default empty = no cleanup.
        """
        return set()

    def delete_ids(self, collection: str, ids: Iterable[str]) -> None:
        """Delete specific chunk ids. Default no-op; persistent providers override."""
        return None

    def set_collection_metadata(self, collection: str, metadata: dict) -> None:
        """Persist collection-level metadata (e.g. CS7 content_fingerprint).
        Default no-op; persistent providers override.
        """
        return None

    def get_collection_metadata(self, collection: str) -> dict:
        """Read collection-level metadata. Default empty (no drift detection)."""
        return {}


class ChromaProvider(VectorStoreProvider):
    """Default VectorStoreProvider using Chroma persistent client.

    chromadb is imported lazily on first call.
    """

    def __init__(
        self, *, persist_dir: str | Path | None = None, config: dict | None = None
    ) -> None:
        # Resolution order: explicit persist_dir > resolve_paths(config), where the
        # resolver honors EVENT_INTEL_CHROMA_DIR (env), then config.paths.chroma_dir,
        # then the ~/.event-intel/chroma default. This is the bug-(b) fix: previously
        # config.paths.chroma_dir — a key preflight *requires* — was silently ignored.
        if persist_dir is not None:
            self.persist_dir = Path(persist_dir).expanduser()
        else:
            self.persist_dir = _paths.resolve_paths(config).chroma_dir
        self._client = None

    def _get_client(self) -> Any:
        if self._client is None:
            import chromadb

            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        return self._client

    def _get_collection(self, name: str) -> Any:
        client = self._get_client()
        return client.get_or_create_collection(name=name)

    def upsert(
        self,
        *,
        collection: str,
        ids: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        documents: list[str],
    ) -> None:
        col = self._get_collection(collection)
        col.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=documents)

    def query(
        self,
        *,
        collection: str,
        query_embeddings: list[list[float]],
        top_k: int = 5,
        where: dict | None = None,
    ) -> list[list[dict]]:
        col = self._get_collection(collection)
        raw = col.query(
            query_embeddings=query_embeddings,
            n_results=top_k,
            where=where,
        )
        results: list[list[dict]] = []
        for i in range(len(query_embeddings)):
            hits = []
            for j in range(len(raw["ids"][i])):
                hits.append(
                    {
                        "id": raw["ids"][i][j],
                        "document": raw["documents"][i][j],
                        "metadata": raw["metadatas"][i][j],
                        "distance": raw["distances"][i][j],
                    }
                )
            results.append(hits)
        return results

    def existing_ids(self, collection: str) -> set[str]:
        try:
            col = self._get_client().get_collection(name=collection)
            return set(col.get(include=[]).get("ids", []))
        except Exception:
            return set()

    def delete_ids(self, collection: str, ids: Iterable[str]) -> None:
        ids = list(ids)
        if not ids:
            return
        self._get_collection(collection).delete(ids=ids)

    def set_collection_metadata(self, collection: str, metadata: dict) -> None:
        col = self._get_collection(collection)
        merged = {**(col.metadata or {}), **metadata}
        col.modify(metadata=merged)

    def get_collection_metadata(self, collection: str) -> dict:
        try:
            col = self._get_client().get_collection(name=collection)
            return dict(col.metadata or {})
        except Exception:
            return {}

    def collection_info(self, collection: str) -> dict:
        try:
            client = self._get_client()
            col = client.get_collection(name=collection)
            return {"exists": True, "count": col.count()}
        except Exception:
            return {"exists": False, "count": 0}

    def ensure_writable(self) -> dict:
        try:
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            probe = self.persist_dir / ".writable_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return {"status": "writable", "path": str(self.persist_dir)}
        except Exception as e:
            return {"status": "denied", "path": str(self.persist_dir), "error": str(e)}
