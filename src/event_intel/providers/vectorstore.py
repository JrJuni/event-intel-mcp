from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path


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


class ChromaProvider(VectorStoreProvider):
    """Default VectorStoreProvider using Chroma persistent client.

    chromadb is imported lazily on first call.
    """

    def __init__(self, *, persist_dir: str | Path | None = None):
        self.persist_dir = Path(
            persist_dir
            or os.environ.get(
                "EVENT_INTEL_CHROMA_DIR", Path.home() / ".event-intel" / "chroma"
            )
        )
        self._client = None

    def _get_client(self):
        if self._client is None:
            import chromadb

            self.persist_dir.mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=str(self.persist_dir))
        return self._client

    def _get_collection(self, name: str):
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
