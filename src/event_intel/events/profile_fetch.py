"""E1 — pre-triage profile-fetch (Tier 1 evidence).

Why: triage scores ``ExhibitorCandidate.source_snippet[:100]`` (triage.py), which
for a CSV roster is just ``"CSV row 12: Acme | https://expo.example/e/acme"`` —
semantically empty. Triage is then name-keyword scoring, and on a 2,885-row
roster the industrial-sounding look-alikes drown the real targets (diagnosed
P@10=0). This stage attaches, for the WHOLE roster, the body text of each
exhibitor's detail page (``candidate.url``) so triage scores what a company
DOES, not how its name reads.

Exhibition-agnostic by design (user constraint 2026-06-13: do NOT overfit to
Hannover Messe). No site-specific parser — a generic byte-capped GET + a
trafilatura body extract, the same primitive that works on any exhibitor
profile page. Where a roster carries no usable detail page, this stage simply
yields no profile and the company stays UNKNOWN, to be picked up by Tier 2
(per-company search) in a later slice.

This is the cheap tier: search API $0 (no search here at all), fetch $0
(HTTP + cache), extract $0 (trafilatura local). The only LLM cost is downstream
— feeding ``profile_text`` into the existing batched triage, NOT a per-company
summarisation pass (that would be one LLM call per exhibitor; we deliberately
avoid it).

Discipline mirrors ``homepage_evidence`` (which mirrors ``news_body``): robots
gate via ``acquisition.robots`` (never ``urllib.robotparser.read()``, playbook
#12), URL-keyed JSON cache with TTL, ONE retry for transient shapes only (R3,
retry-playbook §2), deterministic verdicts cached / transient + robots-deny not
cached, a per-live-fetch throttle for politeness on a single exhibition host,
and an injectable ``fetch_fn`` so tests never touch the network. ``fetch_roster``
and ``fetch_one`` NEVER raise.

All constants PROVISIONAL (offline-only phase — live validation deferred).
Heavy imports (httpx / trafilatura / robots) stay inside method bodies
(cold-import rule).
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from event_intel.runtime.failure_log import FailureLog

if TYPE_CHECKING:
    from event_intel.events.extraction import ExhibitorCandidate

_log = logging.getLogger(__name__)

PROFILE_STAGE = "profile_fetch"


@dataclass
class ProfileFetchConfig:
    enabled: bool = True
    max_bytes_per_page: int = 1_048_576
    timeout_s: float = 10.0
    min_body_chars: int = 80          # profile pages are terse; lower than homepage's 200
    profile_max_chars: int = 600      # what triage will read per company (token budget)
    cache_ttl_days: int | None = 14
    # R3 (retry-playbook §2): ONE retry for transient shapes (429/5xx/transport)
    # only; 4xx refusals are deterministic — never retried.
    max_retries: int = 1
    retry_pause_s: float = 2.0
    # Politeness: pause before each LIVE fetch (cache hits skip it). A full
    # roster hits ONE exhibition host repeatedly, so default to a real throttle.
    throttle_s: float = 0.5

    @classmethod
    def from_dict(cls, d: dict) -> ProfileFetchConfig:
        ttl = d.get("cache_ttl_days", 14)
        return cls(
            enabled=bool(d.get("enabled", True)),
            max_bytes_per_page=int(d.get("max_bytes_per_page", 1_048_576)),
            timeout_s=float(d.get("timeout_s", 10.0)),
            min_body_chars=int(d.get("min_body_chars", 80)),
            profile_max_chars=int(d.get("profile_max_chars", 600)),
            cache_ttl_days=int(ttl) if ttl is not None else None,
            max_retries=int(d.get("max_retries", 1)),
            retry_pause_s=float(d.get("retry_pause_s", 2.0)),
            throttle_s=float(d.get("throttle_s", 0.5)),
        )


@dataclass
class ProfileFetchResult:
    n_total: int = 0          # candidates seen
    n_with_url: int = 0       # candidates carrying a fetchable detail URL
    n_profiled: int = 0       # candidates that got profile_text (outcome ok)
    n_empty: int = 0          # had a URL but unreachable / too thin / refused
    pages_fetched: int = 0    # LIVE fetches (cache hits excluded)
    warnings: list[str] = field(default_factory=list)


class ProfileFetcher:
    """Fetch detail-page body text for a roster of exhibitor candidates.

    ``fetch_fn(url) -> {"status", "text", "final_url", "error"?}`` is injectable
    for tests; the default performs a live byte-capped streaming GET. Neither
    ``fetch_one`` nor ``fetch_roster`` ever raises.
    """

    def __init__(
        self,
        *,
        cfg: ProfileFetchConfig,
        cache_dir: Path,
        now: datetime,
        fetch_fn: Callable[[str], dict] | None = None,
        transport: object | None = None,
        sleep: Callable[[float], None] | None = None,
        failure_log: FailureLog | None = None,
    ) -> None:
        self.cfg = cfg
        self.cache_dir = Path(cache_dir)
        self.now = now
        self._fetch_fn = fetch_fn or self._fetch_live
        self._transport = transport  # httpx transport override (tests)
        self.failure_log = failure_log
        self.pages_fetched = 0
        import time as _time

        self._sleep = sleep or _time.sleep  # injectable (tests)

    # ---------- public API ----------

    def fetch_roster(
        self, candidates: Sequence[ExhibitorCandidate]
    ) -> ProfileFetchResult:
        """Populate ``candidate.profile_text`` in place for every candidate with
        a reachable detail page. Returns coverage stats. NEVER raises.
        """
        result = ProfileFetchResult(n_total=len(candidates))
        self.pages_fetched = 0
        for cand in candidates:
            url = (getattr(cand, "url", None) or "").strip()
            if not url:
                continue
            result.n_with_url += 1
            try:
                text = self.fetch_one(url)
            except Exception as exc:  # belt-and-braces: the stage never raises
                _log.warning("profile fetch failed for %s: %s", url, exc)
                text = None
            if text:
                cand.profile_text = text
                result.n_profiled += 1
            else:
                result.n_empty += 1
        result.pages_fetched = self.pages_fetched
        if result.n_with_url:
            result.warnings.append(
                f"profile_fetch: {result.n_profiled}/{result.n_with_url} exhibitors "
                f"profiled ({result.pages_fetched} live, "
                f"{result.n_with_url - result.pages_fetched} cached); "
                f"{result.n_total - result.n_with_url} had no detail URL"
            )
        return result

    def fetch_one(self, url: str) -> str | None:
        """Detail-page body text for one URL, truncated to ``profile_max_chars``.
        None = no usable profile (unreachable / robots deny / thin / refused).
        Never raises.
        """
        try:
            page = self._get_page(url)
        except Exception as exc:  # noqa: BLE001 — stage never raises
            _log.warning("profile fetch error for %s: %s", url, exc)
            return None
        if page is None or page.get("outcome") != "ok":
            return None
        body = (page.get("body") or "").strip()
        return body[: self.cfg.profile_max_chars] or None

    # ---------- page fetch (cache → robots → throttle → GET → extract) ----------

    def _get_page(self, url: str) -> dict | None:
        """Cached-or-fetched page verdict. None = transient failure / robots
        deny (NOT cached). Dict outcomes: ok / too_short / refused — all
        deterministic, all cached.
        """
        cached = self._cache_get(url)
        if cached is not None:
            return cached

        from event_intel.acquisition import robots as _robots
        from event_intel.acquisition.raw_fetch import get_user_agent

        if not _robots.is_allowed(url, user_agent=get_user_agent()):
            self._log(url, outcome="robots_denied")
            return None

        if self.cfg.throttle_s > 0:
            self._sleep(self.cfg.throttle_s)

        attempts = 0
        while True:
            attempts += 1
            try:
                fetched = self._fetch_fn(url)
            except Exception as exc:  # injected fetch_fn may raise; live one doesn't
                fetched = {"status": None, "text": None, "error": f"{type(exc).__name__}: {exc}"}
            if not fetched.get("error") and fetched.get("text"):
                break
            status = fetched.get("status")
            transient = status is None or status == 429 or (
                isinstance(status, int) and 500 <= status < 600
            )
            if transient and attempts <= self.cfg.max_retries:
                self._sleep(self.cfg.retry_pause_s)
                continue
            self._log(url, outcome="error", status=status,
                      exc=fetched.get("error"), attempts=attempts)
            if not transient:
                # Deterministic 4xx refusal — cache the negative verdict.
                payload = {"url": url, "body": None, "outcome": "refused", "status": status}
                self._cache_put(url, payload)
                return payload
            return None

        self.pages_fetched += 1
        body = self._extract(fetched["text"]) or ""
        payload: dict = {
            "url": url,
            "final_url": fetched.get("final_url"),
            "status": fetched.get("status"),
            "body": body,
            "outcome": "ok" if len(body) >= self.cfg.min_body_chars else "too_short",
        }
        self._cache_put(url, payload)
        self._log(url, outcome=payload["outcome"], status=fetched.get("status"))
        return payload

    # ---------- cache (news_body / homepage_evidence discipline) ----------

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

    # ---------- live fetch (byte cap + charset decode) ----------

    def _fetch_live(self, url: str) -> dict:
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
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) >= self.cfg.max_bytes_per_page:
                        break
                from event_intel.textenc import decode_html

                text = decode_html(
                    bytes(buf[: self.cfg.max_bytes_per_page]),
                    header_charset=resp.charset_encoding,
                )
                return {
                    "status": resp.status_code,
                    "text": text,
                    "final_url": str(resp.url),
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
        exc: str | None = None, attempts: int = 1,
    ) -> None:
        if self.failure_log is None:
            return
        from urllib.parse import urlparse

        self.failure_log.append({
            "ts": self.now.isoformat(),
            "lane": "profile",
            "kind": "detail_page",
            "domain": urlparse(url).netloc,
            "url": url,
            "status": status,
            "outcome": outcome,
            "attempts": attempts,
            "exc_classes": [exc] if exc else [],
        })
