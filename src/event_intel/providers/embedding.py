from __future__ import annotations

import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    @abstractmethod
    def is_ready(self) -> dict: ...

    def warm_up(self) -> dict:
        """Force any lazy model load now. Default: a single throwaway embed.

        Lets callers (e.g. check_runtime warm_up) pay the load cost deliberately
        instead of on the first real request. Subclasses may override to report
        richer status. Cheap for providers with no lazy load.
        """
        import time

        t0 = time.perf_counter()
        self.embed(["warmup"])
        return {"status": "ready", "load_seconds": round(time.perf_counter() - t0, 2)}


class BgeM3Provider(EmbeddingProvider):
    """Default EmbeddingProvider using bge-m3 via sentence_transformers.

    sentence_transformers / torch / transformers are imported lazily on first embed call.
    """

    MODEL_NAME = "BAAI/bge-m3"

    # Process-level model cache, keyed by str(cache_dir). The bge-m3 load is
    # ~1.3 GB and several seconds; without this every tool call (each of which
    # constructs a fresh BgeM3Provider) would reload from scratch. With it, a
    # one-time warm_up persists for the life of the MCP server process so later
    # build_event_tier_list calls reuse the in-memory model. Lazy import stays
    # inside _get_model, so module import remains cold-start safe.
    _MODEL_CACHE: dict[str, object] = {}
    # Guards cache population so a background warm-up thread and a concurrent
    # build() don't both construct the model. See runtime/warmup.py.
    _CACHE_LOCK = threading.Lock()

    def __init__(self, *, cache_dir: str | Path | None = None):
        self.cache_dir = (
            Path(cache_dir)
            if cache_dir
            else Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
        )
        self._model = None

    def _get_model(self):
        if self._model is None:
            key = str(self.cache_dir)
            cached = BgeM3Provider._MODEL_CACHE.get(key)
            if cached is None:
                with BgeM3Provider._CACHE_LOCK:
                    # Double-check inside the lock: another thread may have loaded
                    # it while we waited.
                    cached = BgeM3Provider._MODEL_CACHE.get(key)
                    if cached is None:
                        from sentence_transformers import SentenceTransformer

                        cached = SentenceTransformer(
                            self.MODEL_NAME, cache_folder=str(self.cache_dir)
                        )
                        BgeM3Provider._MODEL_CACHE[key] = cached
            self._model = cached
        return self._model

    def warm_up(self) -> dict:
        """Load bge-m3 into the process cache now and report timing.

        Reports ``already_cached`` so the caller can tell an instant warm-up
        (model already resident) from a cold ~1.3 GB load.
        """
        import time

        key = str(self.cache_dir)
        already = key in BgeM3Provider._MODEL_CACHE
        t0 = time.perf_counter()
        self.embed(["warmup"])  # forces _get_model load + a real encode
        return {
            "status": "ready",
            "already_cached": already,
            "load_seconds": round(time.perf_counter() - t0, 2),
        }

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
