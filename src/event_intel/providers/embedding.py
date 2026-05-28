from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def is_ready(self) -> dict: ...


class BgeM3Provider(EmbeddingProvider):
    """Default EmbeddingProvider using bge-m3 via sentence_transformers.

    sentence_transformers / torch / transformers are imported lazily on first embed call.
    """

    MODEL_NAME = "BAAI/bge-m3"

    def __init__(self, *, cache_dir: str | Path | None = None):
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir
            else Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
        )
        self._model = None

    def _get_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self.MODEL_NAME, cache_folder=str(self.cache_dir)
            )
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self._get_model()
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]

    def is_ready(self) -> dict:
        """Check if bge-m3 weights are cached locally. Lazy — does NOT load the model."""
        from huggingface_hub import try_to_load_from_cache

        cached = try_to_load_from_cache(
            repo_id=self.MODEL_NAME,
            filename="config.json",
            cache_dir=str(self.cache_dir),
        )
        if cached is None:
            return {"status": "missing", "path": str(self.cache_dir)}
        cache_path = Path(cached).parent
        size_mb = sum(p.stat().st_size for p in cache_path.glob("*") if p.is_file()) // (
            1024 * 1024
        )
        return {"status": "ready", "path": str(cache_path), "size_mb": size_mb}
