"""Cost lever #16-⑤ — disk cache for per-chunk LLM extraction responses.

Root prescription for the "75-minute build dies at chunk 39" loss shape
(docs/retry-playbook.md §3.6): a re-run after ANY failure (LLM error, crash,
user abort) replays already-extracted chunks from disk at zero LLM cost,
instead of re-spending the whole extraction stage.

Key = sha256("v{LLM_CACHE_VERSION}|{model}|{lang}|{prompt_sha}|{chunk_sha}"):
  - ``prompt_sha`` hashes the INVARIANT prompt parts only — the system prompt
    and the user-message template with its placeholders unfilled. Volatile
    tokens ("chunk 39/64", source_ref) are excluded so a re-chunked or
    re-ordered run over the same content still hits.
  - ``chunk_sha`` hashes the chunk body text.
  - ``model`` / ``lang`` isolate caches across providers and prompt languages.

VERSION RULE: any change to SYSTEM_PROMPT_EN/KO or _USER_TEMPLATE in
events/extraction.py is already invalidated by the content hash, but bump
``LLM_CACHE_VERSION`` whenever PARSING semantics change (e.g. _parse_llm_chunk
starts reading a new field) — the stored raw text would otherwise replay
through a parser it was never validated against.

The cached value is the RAW LLM response text. A hit is re-parsed by the
caller with the CURRENT chunk_index (never a stored one) so chunk attribution
stays correct across re-chunking.

Stdlib-only and import-cold safe. Best-effort: unreadable/corrupted files and
write errors are misses, never failures; an uncreatable cache root disables
the cache instead of failing the build.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)

LLM_CACHE_VERSION = 1


def prompt_fingerprint(*invariant_parts: str) -> str:
    """sha256 over the invariant prompt parts (unit-separator joined)."""
    joined = "\x1f".join(invariant_parts)
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()


def _is_fresh(timestamp_raw: str | None, *, now: datetime, ttl_days: int | None) -> bool:
    """Same TTL contract as enrichment._is_fresh (Phase 18W P2-1):
    None/negative → infinite; 0 → never reuse; unparseable/future → stale.
    """
    if ttl_days is None or ttl_days < 0:
        return True
    if ttl_days == 0:
        return False
    from event_intel.timeutil import normalize_utc, parse_iso_utc

    dt = parse_iso_utc(timestamp_raw)
    if dt is None:
        return False
    age_days = (normalize_utc(now) - dt).total_seconds() / 86400.0
    if age_days < 0:
        return False
    return age_days <= ttl_days


class LlmExtractionCache:
    """One JSON file per (version, model, lang, prompt, chunk) hash, payload
    ``{"cached_at": iso, "response_text": str}``. Mirrors enrichment's
    _SearchCache discipline: version-prefixed keys, TTL wrapper, best-effort
    writes, corrupted file = miss.
    """

    def __init__(self, root: Path, *, ttl_days: int | None = None) -> None:
        self.root = root
        self.ttl_days = ttl_days
        self._disabled = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # A build must never die because the cache dir is uncreatable
            # (read-only home, path collision) — run uncached instead.
            self._disabled = True
            _log.warning("LLM cache disabled — cannot create %s (%s)", root, exc)

    @staticmethod
    def _key(*, model: str, lang: str, prompt_sha: str, chunk_text: str) -> str:
        chunk_sha = hashlib.sha256(
            chunk_text.encode("utf-8", errors="replace")
        ).hexdigest()
        return hashlib.sha256(
            f"v{LLM_CACHE_VERSION}|{model}|{lang}|{prompt_sha}|{chunk_sha}".encode()
        ).hexdigest()

    def get(
        self, *, model: str, lang: str, prompt_sha: str, chunk_text: str,
        now: datetime,
    ) -> str | None:
        if self._disabled:
            return None
        path = self.root / f"{self._key(model=model, lang=lang, prompt_sha=prompt_sha, chunk_text=chunk_text)}.json"
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as f:
                payload = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        if not _is_fresh(payload.get("cached_at"), now=now, ttl_days=self.ttl_days):
            return None
        text = payload.get("response_text")
        return text if isinstance(text, str) else None

    def put(
        self, *, model: str, lang: str, prompt_sha: str, chunk_text: str,
        response_text: str, now: datetime,
    ) -> None:
        if self._disabled:
            return
        from event_intel.timeutil import normalize_utc

        path = self.root / f"{self._key(model=model, lang=lang, prompt_sha=prompt_sha, chunk_text=chunk_text)}.json"
        payload = {
            "cached_at": normalize_utc(now).isoformat(),
            "response_text": response_text,
        }
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except OSError:
            pass  # best-effort; a failed write is just a future miss
