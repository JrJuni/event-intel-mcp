"""B1 — news article body fetch lane (zero-config news plan).

Success criterion ① demands the article BODY, not just the search snippet.
Per gated news signal: robots gate (``acquisition.robots`` — never
``urllib.robotparser.read()``, per playbook #12) → streaming GET with a hard
byte cap (C1 pattern) → trafilatura extract → min-chars gate. Failures degrade
that item to snippet-only evidence (never raise) and log an R1-schema event to
``fetch_failures.jsonl``. Successful bodies (and deterministic ``too_short``
verdicts) are cached on disk (URL-keyed, TTL) so re-runs and the B2 gate/RAG
stage read them without re-fetching; transient failures (HTTP/transport/robots
5xx-deny) are NOT cached — the N1 non-stick principle applies to fetches too.

Robots is checked on the ORIGINAL url before the request; redirects are then
followed (``final_url`` recorded — resolves Google News RSS wrapper links, N3).
Heavy imports (httpx / trafilatura / robots) stay inside method bodies.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from event_intel.runtime.failure_log import FailureLog

if TYPE_CHECKING:
    from event_intel.events.enrichment import NewsSignal


@dataclass
class NewsBodyConfig:
    enabled: bool = False
    max_per_company: int = 12
    max_bytes_per_page: int = 1_048_576
    timeout_s: float = 10.0
    min_body_chars: int = 400
    cache_ttl_days: int | None = 14

    @classmethod
    def from_dict(cls, d: dict) -> NewsBodyConfig:
        ttl = d.get("cache_ttl_days", 14)
        return cls(
            enabled=bool(d.get("enabled", False)),
            max_per_company=int(d.get("max_per_company", 12)),
            max_bytes_per_page=int(d.get("max_bytes_per_page", 1_048_576)),
            timeout_s=float(d.get("timeout_s", 10.0)),
            min_body_chars=int(d.get("min_body_chars", 400)),
            cache_ttl_days=int(ttl) if ttl is not None else None,
        )


class NewsBodyFetcher:
    """Fetch + cache article bodies for gated news signals.

    ``fetch_fn`` is injectable for tests; the default performs a live streaming
    GET. ``attach_bodies`` mutates the given signals in place (``body_sha`` /
    ``body_chars``) and is guaranteed never to raise.
    """

    def __init__(
        self,
        *,
        cfg: NewsBodyConfig,
        cache_dir: Path,
        failure_log: FailureLog | None = None,
        now: datetime,
        fetch_fn: Callable[[str], dict] | None = None,
        transport: object | None = None,
    ) -> None:
        self.cfg = cfg
        self.cache_dir = Path(cache_dir)
        self.failure_log = failure_log
        self.now = now
        self._fetch_fn = fetch_fn or self._fetch_live
        self._transport = transport  # httpx transport override (tests)

    # ---------- public API ----------

    def attach_bodies(self, signals: list[NewsSignal]) -> int:
        """Fetch bodies for up to ``max_per_company`` signals; returns how many
        got one. Per-item failures degrade to snippet-only (no raise).
        """
        attached = 0
        for sig in signals[: self.cfg.max_per_company]:
            if not sig.url:
                continue
            try:
                payload = self._get_or_fetch(sig.url)
            except Exception as exc:  # belt-and-braces: the lane never raises
                self._log(sig.url, outcome="error", exc=type(exc).__name__)
                continue
            if payload and payload.get("body"):
                sig.body_sha = payload.get("sha")
                sig.body_chars = len(payload["body"])
                attached += 1
        return attached

    def load_body(self, url: str) -> str | None:
        """Read a cached body (B2 gate/RAG consumers). None when absent/stale."""
        payload = self._cache_get(url)
        return payload.get("body") if payload else None

    # ---------- cache ----------

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha1(url.encode()).hexdigest()}.json"

    def _cache_get(self, url: str) -> dict | None:
        path = self._cache_path(url)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(payload, dict):
            return None
        from event_intel.events.enrichment import _is_fresh  # lazy: avoid cycle

        if not _is_fresh(
            payload.get("fetched_at"), now=self.now, ttl_days=self.cfg.cache_ttl_days
        ):
            return None
        return payload

    def _cache_put(self, url: str, payload: dict) -> None:
        from event_intel.timeutil import normalize_utc

        payload = {"fetched_at": normalize_utc(self.now).isoformat(), **payload}
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_path(url).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass  # cache is best-effort

    # ---------- fetch ----------

    def _get_or_fetch(self, url: str) -> dict | None:
        cached = self._cache_get(url)
        if cached is not None:
            return cached  # includes cached too_short verdicts ({"body": None})

        from event_intel.acquisition import robots as _robots
        from event_intel.acquisition.raw_fetch import get_user_agent

        if not _robots.is_allowed(url, user_agent=get_user_agent()):
            # Not cached: a robots 5xx/transport conservative deny is transient.
            self._log(url, outcome="robots_denied")
            return None

        try:
            result = self._fetch_fn(url)
        except Exception as exc:  # injected fetch_fn may raise; live one doesn't
            result = {"status": None, "text": None, "error": f"{type(exc).__name__}: {exc}"}

        if result.get("error") or not result.get("text"):
            # Transient (HTTP error / transport / empty) → NOT cached, retried
            # on the next run (N1 non-stick).
            self._log(
                url, outcome="error", status=result.get("status"),
                exc=result.get("error"),
            )
            return None

        body = self._extract(result["text"])
        if not body or len(body) < self.cfg.min_body_chars:
            # Deterministic content property → cache the negative verdict so the
            # page isn't re-fetched every run.
            payload = {
                "url": url, "final_url": result.get("final_url"),
                "body": None, "outcome": "too_short",
            }
            self._cache_put(url, payload)
            self._log(url, outcome="too_short", status=result.get("status"))
            return None

        payload = {
            "url": url,
            "final_url": result.get("final_url"),
            "sha": hashlib.sha1(body.encode()).hexdigest(),
            "body": body,
            "truncated": bool(result.get("truncated", False)),
        }
        self._cache_put(url, payload)
        self._log(
            url, outcome="ok", status=result.get("status"),
            truncated=payload["truncated"],
        )
        return payload

    def _fetch_live(self, url: str) -> dict:
        """Streaming GET with a hard byte cap (true bandwidth cap, C1 pattern)."""
        import httpx

        from event_intel.acquisition.raw_fetch import get_user_agent

        kwargs: dict = {
            "timeout": self.cfg.timeout_s,
            "follow_redirects": True,
            "headers": {"User-Agent": get_user_agent()},
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            with httpx.Client(**kwargs) as client, client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return {
                        "status": resp.status_code, "text": None,
                        "error": f"HTTP {resp.status_code}",
                    }
                buf = bytearray()
                truncated = False
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) >= self.cfg.max_bytes_per_page:
                        truncated = True
                        break
                text = bytes(buf[: self.cfg.max_bytes_per_page]).decode(
                    resp.encoding or "utf-8", errors="replace"
                )
                return {
                    "status": resp.status_code,
                    "text": text,
                    "final_url": str(resp.url),
                    "truncated": truncated,
                }
        except Exception as exc:
            return {"status": None, "text": None, "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _extract(html: str) -> str | None:
        import trafilatura

        return trafilatura.extract(html, include_comments=False, include_tables=False)

    # ---------- diagnostics (R1 schema) ----------

    def _log(
        self, url: str, *, outcome: str, status: int | None = None,
        exc: str | None = None, truncated: bool = False,
    ) -> None:
        if self.failure_log is None:
            return
        from urllib.parse import urlparse

        self.failure_log.append({
            "ts": self.now.isoformat(),
            "lane": "news_body",
            "kind": "body",
            "domain": urlparse(url).netloc,
            "url": url,
            "status": status,
            "outcome": outcome,
            "attempts": 1,
            "exc_classes": [exc] if exc else [],
            "truncated": truncated,
        })
